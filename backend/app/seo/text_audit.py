"""Auditoría de textos del catálogo (HG1): lee Vendure, aplica las reglas de
`text_rules` y guarda el resultado en `text_audit_run` / `text_audit_item`.

SOLO LECTURA sobre Vendure: usa únicamente `VendureClient.fetch_texts_for_audit`,
que es una query. Lo único que se escribe es la base de Hugo.

Qué recorre: todos los productos (habilitados y deshabilitados) del canal
Argentina (VENDURE_CHANNEL_TOKEN) y del canal por defecto, con todas sus
traducciones. Cada canal se lee aparte: si uno falla, la corrida queda
`degraded` con el motivo y el otro se audita igual.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, delete, distinct, or_
from sqlmodel import Session, func, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import TextAuditItem, TextAuditRun
from app.db.session import engine
from app.seo import lists as seo_lists
from app.seo import text_rules as rules
from app.vendure.client import ProductTexts, TextsRead, VendureClient
from gql.transport.exceptions import TransportError

log = logging.getLogger(__name__)

JOB_ID = "seo_text_audit"
RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_DEGRADED = "degraded"
RUN_FAILED = "failed"

CHANNEL_AR = "ar"
CHANNEL_DEFAULT = "default"

KEEP_RUNS = 8                               # corridas que se conservan
STALE_RUN_AFTER = timedelta(minutes=30)     # una "running" más vieja quedó huérfana
READ_TIMEOUT_S = 270.0                      # por canal; el pedido es terminar en menos de 5 min
EVAL_TIMEOUT_S = 240.0                      # evaluar las reglas de todo el catálogo
MANUAL_COOLDOWN_S = 60
EXPORT_MAX_ROWS = 20_000

text_audit_lock = asyncio.Lock()

Reader = Callable[[str | None], Awaitable[TextsRead]]


async def read_channel(token: str | None) -> TextsRead:
    """Lectura real: el canal `token`, o el canal por defecto si es None."""
    return await VendureClient(channel_token=token).fetch_texts_for_audit()


def channels_to_read() -> list[tuple[str, str | None]]:
    """[(etiqueta, token)]: el canal Argentina (si hay VENDURE_CHANNEL_TOKEN) y el
    canal por defecto."""
    ar_token = (get_settings().vendure_channel_token or "").strip()
    out: list[tuple[str, str | None]] = []
    if ar_token:
        out.append((CHANNEL_AR, ar_token))
    out.append((CHANNEL_DEFAULT, None))
    return out


# ─── Evaluación (pura: no toca Vendure ni la base) ─────────────────

@dataclass(slots=True)
class MergedProduct:
    product: ProductTexts
    in_ar: bool | None
    in_default: bool | None


def merge_channels(reads: dict[str, TextsRead]) -> list[MergedProduct]:
    """Une los productos de los canales leídos por id. `in_<canal>` es None si ese
    canal no se pudo leer (o no está configurado)."""
    ids: dict[str, set[str] | None] = {
        label: ({p.id for p in reads[label].products} if label in reads else None)
        for label in (CHANNEL_AR, CHANNEL_DEFAULT)
    }
    by_id: dict[str, ProductTexts] = {}
    for label in (CHANNEL_AR, CHANNEL_DEFAULT):  # el texto del canal AR gana
        for p in (reads[label].products if label in reads else []):
            by_id.setdefault(p.id, p)
    ordered = sorted(by_id, key=rules.id_sort_key)
    return [
        MergedProduct(
            product=by_id[pid],
            in_ar=None if ids[CHANNEL_AR] is None else pid in ids[CHANNEL_AR],
            in_default=None if ids[CHANNEL_DEFAULT] is None else pid in ids[CHANNEL_DEFAULT],
        )
        for pid in ordered
    ]


def _row(m: MergedProduct, language: str, name: str, slug: str, description: str) -> TextAuditItem:
    # Topes: nombres y slugs de miles de caracteres no se guardan enteros, y los códigos y el
    # idioma entran en columnas de ancho fijo (Postgres rechaza lo que no cabe).
    return TextAuditItem(
        run_id=0,
        product_id=m.product.id[:64],
        language_code=language[:16],
        name=name[:rules.MAX_NAME_CHARS],
        slug=slug[:rules.MAX_SLUG_CHARS],
        enabled=m.product.enabled,
        product_code=m.product.product_code[:64] if m.product.product_code else None,
        in_ar=m.in_ar,
        in_default=m.in_default,
        name_len=len(name.strip()),
        desc_chars=len(rules.plain_text(description)),
    )


def evaluate(
    merged: Iterable[MergedProduct],
    lists: seo_lists.TextLists,
    deadline: float | None = None,
    problems: list[str] | None = None,
) -> list[TextAuditItem]:
    """Una fila por producto e idioma, con las reglas que saltaron.

    Un producto que no se puede evaluar (dato raro, bug de una regla) se saltea y queda en
    `problems` (solo su id): no tira la corrida. Una traducción cuyo idioma ya apareció (es_AR y
    es-ar) se ignora. `deadline` (monotonic) corta con `rules.AuditTimeout`."""
    problems = problems if problems is not None else []
    technical = frozenset(t.upper() for t in lists.technical)
    rows: list[TextAuditItem] = []
    found: dict[int, dict[str, str]] = {}   # id(fila) -> {regla: detalle}
    for step, m in enumerate(merged):
        if deadline is not None and step % 64 == 0 and time.monotonic() > deadline:
            raise rules.AuditTimeout("la evaluación de los productos pasó su plazo")
        p = m.product
        try:
            product_rows, product_found = _evaluate_product(m, lists, technical)
        except rules.AuditTimeout:
            raise
        except Exception as exc:  # noqa: BLE001  (un producto raro no puede tirar los demás)
            log.warning("seo_text_audit: no se pudo evaluar el producto %s: %s", p.id, _short(exc))
            problems.append(f"producto {p.id[:40]}: no se pudo evaluar ({type(exc).__name__})")
            continue
        if product_found["repeated"]:
            problems.append(f"producto {p.id[:40]}: {product_found['repeated']} traducción(es) con el idioma repetido (se ignoró)")
        for row in product_rows:
            found[id(row)] = product_found["rules"][id(row)]
        rows.extend(product_rows)

    # Duplicados: entre productos habilitados, dentro de cada idioma.
    by_lang: dict[str, list[tuple[str, str, str]]] = {}
    row_by_key: dict[str, TextAuditItem] = {}
    for row in rows:
        if not row.enabled or not row.name.strip():
            continue
        key = f"{row.product_id}|{row.language_code}"
        row_by_key[key] = row
        by_lang.setdefault(row.language_code, []).append((key, row.product_id, row.name))
    for entries in by_lang.values():
        for key, dups in rules.find_duplicates(entries, deadline=deadline).items():
            found[id(row_by_key[key])].update(dups)

    for row in rows:
        ordered = rules.sort_rules(found[id(row)])
        row.n_issues = len(ordered)
        row.issues = f",{','.join(ordered)}," if ordered else ""
        row.details = json.dumps({r: found[id(row)][r] for r in ordered}, ensure_ascii=False) if ordered else None
    return rows


def _evaluate_product(
    m: MergedProduct, lists: seo_lists.TextLists, technical: frozenset[str],
) -> tuple[list[TextAuditItem], dict[str, Any]]:
    """Las filas de un producto (una por idioma distinto) y las reglas que saltaron en cada una."""
    p = m.product
    matcher = rules.SupplierMatcher(
        rules.SupplierRefs(p.supplier_business, p.supplier_size_model, p.supplier_link), technical,
    )
    seen_langs: set[str] = set()
    translations = []
    repeated = 0
    for t in p.translations or []:
        lang = rules.normalize_lang(t.language)
        if lang in seen_langs:
            repeated += 1
            continue
        seen_langs.add(lang)
        translations.append((t, lang))
    product_rows: list[TextAuditItem] = []
    per_row: dict[int, dict[str, str]] = {}
    if not translations:
        row = _row(m, "", "", "", "")
        product_rows.append(row)
        per_row[id(row)] = rules.audit_translation(
            "", "", "", product_code=p.product_code, lists=lists, matcher=matcher)
    for t, lang in translations:
        row = _row(m, lang, t.name, t.slug, t.description)
        product_rows.append(row)
        per_row[id(row)] = rules.audit_translation(
            t.name, t.slug, t.description, product_code=p.product_code, lists=lists, matcher=matcher,
        )
    if "es_AR" not in seen_langs:
        missing = rules.missing_es_ar_detail(seen_langs)
        for row in product_rows:
            per_row[id(row)]["SIN_ES_AR"] = missing
    return product_rows, {"rules": per_row, "repeated": repeated}


def rule_counts(rows: Iterable[TextAuditItem]) -> dict[str, int]:
    """{regla: productos distintos} (un producto con la misma regla en dos idiomas cuenta una vez)."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        for rule in row.issues.strip(",").split(","):
            if rule:
                seen.setdefault(rule, set()).add(row.product_id)
    return {rule: len(seen[rule]) for rule in rules.sort_rules(seen)}


# ─── Corrida ───────────────────────────────────────────────────────

_SECRET_RES = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(authorization|token|api[-_ ]?key|password|secret)\b\s*[:=]\s*\S+"),
)


def _short(exc: BaseException) -> str:
    """El motivo de un fallo para mostrar en el dashboard: corto y sin credenciales."""
    msg = " ".join(str(exc).split())
    msg = _SECRET_RES[0].sub("Bearer …", msg)
    msg = _SECRET_RES[1].sub(r"\1 …", msg)
    return f"{type(exc).__name__}: {msg}"[:300] if msg else type(exc).__name__


class AuditFailed(Exception):
    """Falla de la auditoría con un mensaje ya pensado para mostrarse (sin datos del servidor)."""


def _failure_reason(exc: BaseException, channel: str | None = None) -> str:
    """El motivo de un fallo que se guarda y se muestra en el dashboard: el tipo de la excepción
    y una frase fija. El texto de la excepción (que puede traer una respuesta del servidor, una
    URL o un id) queda solo en el log del servidor."""
    if isinstance(exc, AuditFailed):
        return str(exc)[:300]
    name = type(exc).__name__
    where = f" (canal {channel})" if channel else ""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        if isinstance(exc, rules.AuditTimeout):
            return f"{name}: la evaluación de las reglas pasó el plazo de {EVAL_TIMEOUT_S:g} s"
        return f"{name}: Vendure no respondió en {READ_TIMEOUT_S:g} s{where}"
    if isinstance(exc, (TransportError, OSError)) or type(exc).__module__.startswith(("httpx", "httpcore", "gql")):
        return f"{name}: no se pudo leer Vendure{where}"
    return f"{name}: error inesperado{where}; el detalle está en el log del servidor"


def _fail_orphans(session: Session) -> None:
    cutoff = utcnow() - STALE_RUN_AFTER
    for run in session.exec(select(TextAuditRun).where(
            TextAuditRun.status == RUN_RUNNING, TextAuditRun.started_at < cutoff)):
        run.status = RUN_FAILED
        run.finished_at = utcnow()
        run.error = "interrumpida (el proceso se reinició o tardó demasiado)"
        session.add(run)
    session.commit()


def _prune(session: Session) -> None:
    keep = session.exec(
        select(TextAuditRun.id).order_by(TextAuditRun.id.desc()).limit(KEEP_RUNS)  # type: ignore[union-attr]
    ).all()
    if len(keep) < KEEP_RUNS:
        return
    oldest_kept = min(keep)
    session.execute(delete(TextAuditItem).where(TextAuditItem.run_id < oldest_kept))
    session.execute(delete(TextAuditRun).where(TextAuditRun.id < oldest_kept))
    session.commit()


async def _read_all(reader: Reader) -> tuple[dict[str, TextsRead], dict[str, str]]:
    """Lee los canales en paralelo, cada uno con su tope de tiempo y aislado."""
    channels = channels_to_read()

    async def one(token: str | None) -> TextsRead:
        return await asyncio.wait_for(reader(token), READ_TIMEOUT_S)

    results = await asyncio.gather(*(one(tok) for _, tok in channels), return_exceptions=True)
    ok: dict[str, TextsRead] = {}
    failed: dict[str, str] = {}
    for (label, _), res in zip(channels, results):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            failed[label] = _failure_reason(res, label)
            log.error("Auditoría de textos: no pude leer el canal %s: %s", label, _short(res))
        else:
            ok[label] = res
    return ok, failed


def _utc_iso(dt: datetime | None) -> str | None:
    """Las fechas de Hugo se guardan en UTC sin zona: el navegador necesita la «Z»."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _insert_items(session: Session, items: list[TextAuditItem]) -> int:
    """Inserta todas las filas en una transacción; si el lote falla (un dato que la base no
    acepta) reintenta fila por fila y se saltea solo las que no entran. Devuelve cuántas salteó."""
    try:
        session.add_all(items)
        session.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        log.warning("seo_text_audit: el lote de filas falló (%s); se reintenta una por una", _short(exc))
        session.rollback()
    skipped = 0
    for item in items:
        item.id = None
        try:
            with session.begin_nested():
                session.add(item)
                session.flush()
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            log.warning("seo_text_audit: se salteó la fila del producto %s (%s): %s",
                        item.product_id, item.language_code, _short(exc))
            if item in session:
                session.expunge(item)
            item.id = None
    return skipped


def _save_results(
    run_id: int,
    merged: list[MergedProduct],
    items: list[TextAuditItem],
    reads: dict[str, TextsRead],
    failed: dict[str, str],
    notes: list[str],
    elapsed_s: float,
) -> dict[str, Any]:
    """Guarda las filas y cierra la corrida. Devuelve el resumen."""
    counts = rule_counts(items)
    with Session(engine) as session:
        run = session.get(TextAuditRun, run_id)
        assert run is not None
        for item in items:
            item.run_id = run_id
        skipped = _insert_items(session, items)
        if skipped:
            notes = [*notes, f"{skipped} fila(s) no se pudieron guardar (ver el log del servidor)."]
            items = [i for i in items if i.id is not None]
            counts = rule_counts(items)
        run.products_total = len(merged)
        run.products_enabled = sum(1 for m in merged if m.product.enabled)
        run.rows_total = len(items)
        run.products_with_issues = len({i.product_id for i in items if i.n_issues})
        run.channels_ok = ",".join(reads)
        run.channels_failed = json.dumps(failed, ensure_ascii=False) if failed else None
        run.counts = json.dumps(counts, ensure_ascii=False)
        run.notes = " ".join(notes) or None
        run.status = RUN_DEGRADED if failed else RUN_OK
        run.finished_at = utcnow()
        run.duration_s = round(elapsed_s, 1)
        session.add(run)
        session.commit()
        _prune(session)
        session.refresh(run)
        return run_to_dict(run)


def run_to_dict(run: TextAuditRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "started_at": _utc_iso(run.started_at),
        "finished_at": _utc_iso(run.finished_at),
        "status": run.status,
        "trigger": run.trigger,
        "products_total": run.products_total,
        "products_enabled": run.products_enabled,
        "rows_total": run.rows_total,
        "products_with_issues": run.products_with_issues,
        "channels_ok": [c for c in (run.channels_ok or "").split(",") if c],
        "channels_failed": json.loads(run.channels_failed) if run.channels_failed else {},
        "duration_s": run.duration_s,
        "counts": json.loads(run.counts) if run.counts else {},
        "notes": run.notes,
        "error": run.error,
    }


async def run_text_audit(trigger: str = "cron", reader: Reader | None = None) -> dict[str, Any] | None:
    """Corre la auditoría completa. Devuelve el resumen de la corrida, o None si ya
    había una en curso. Nunca escribe en Vendure."""
    if text_audit_lock.locked():
        log.warning("seo_text_audit ya está corriendo, ignoro la nueva invocación")
        return None
    async with text_audit_lock:
        t0 = time.monotonic()
        with Session(engine) as session:
            _fail_orphans(session)
            run = TextAuditRun(trigger=trigger, status=RUN_RUNNING)
            session.add(run)
            session.commit()
            session.refresh(run)
            run_id = int(run.id)  # type: ignore[arg-type]
            lists = seo_lists.load(session)
        log.info("Iniciando seo_text_audit (corrida %s, %s)", run_id, trigger)

        notes: list[str] = []
        try:
            reads, failed = await _read_all(reader or read_channel)
            if not reads:
                raise AuditFailed("no se pudo leer ningún canal de Vendure: "
                                  + "; ".join(f"{k}: {v}" for k, v in failed.items()))
            if not any(c == CHANNEL_AR for c, _ in channels_to_read()):
                notes.append("VENDURE_CHANNEL_TOKEN vacío: solo se leyó el canal por defecto "
                             "y «canal AR» queda sin dato.")
            if any(not r.supplier_fields for r in reads.values()):
                notes.append("Vendure no expone supplierBusiness/supplierSizeModel: "
                             "la regla FAB solo compara el link del proveedor.")
            merged = merge_channels(reads)
            # La evaluación y el guardado son CPU y base: en un hilo, para no frenar el
            # event loop (dashboard, scheduler) mientras se procesan miles de filas.
            # El plazo va por dentro (la evaluación se corta sola); el wait_for es el respaldo.
            problems: list[str] = []
            deadline = time.monotonic() + EVAL_TIMEOUT_S
            items = await asyncio.wait_for(
                asyncio.to_thread(evaluate, merged, lists, deadline, problems), EVAL_TIMEOUT_S + 30)
            if problems:
                shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3})" if len(problems) > 3 else "")
                notes.append(f"{len(problems)} aviso(s) al evaluar: {shown}.")
            result = await asyncio.to_thread(
                _save_results, run_id, merged, items, reads, failed, notes, time.monotonic() - t0,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("seo_text_audit falló")
            reason = _failure_reason(exc)
            with Session(engine) as session:
                run = session.get(TextAuditRun, run_id)
                if run is not None:
                    run.status = RUN_FAILED
                    run.error = reason
                    run.finished_at = utcnow()
                    run.duration_s = round(time.monotonic() - t0, 1)
                    session.add(run)
                    session.commit()
                    result = run_to_dict(run)
                else:  # pragma: no cover - la fila la creamos arriba
                    result = {"id": run_id, "status": RUN_FAILED, "error": reason}
            return result
        log.info("seo_text_audit terminado: %d productos, %d filas, %d con problemas, %.1f s",
                 result["products_total"], result["rows_total"],
                 result["products_with_issues"], result["duration_s"])
        return result


# ─── Lectura para el dashboard ─────────────────────────────────────

def latest_run(session: Session) -> TextAuditRun | None:
    """La última corrida con resultados (ok o degraded); si no hay, la última a secas."""
    done = session.exec(
        select(TextAuditRun).where(TextAuditRun.status.in_([RUN_OK, RUN_DEGRADED]))  # type: ignore[attr-defined]
        .order_by(TextAuditRun.id.desc()).limit(1)  # type: ignore[union-attr]
    ).first()
    if done is not None:
        return done
    return session.exec(select(TextAuditRun).order_by(TextAuditRun.id.desc()).limit(1)).first()  # type: ignore[union-attr]


def is_running(session: Session) -> bool:
    if text_audit_lock.locked():
        return True
    cutoff = utcnow() - STALE_RUN_AFTER
    return session.exec(select(TextAuditRun.id).where(
        TextAuditRun.status == RUN_RUNNING, TextAuditRun.started_at >= cutoff).limit(1)).first() is not None


def seconds_since_last_start(session: Session) -> float | None:
    last = session.exec(select(TextAuditRun.started_at).order_by(TextAuditRun.id.desc()).limit(1)).first()  # type: ignore[union-attr]
    return None if last is None else (utcnow() - last).total_seconds()


ENABLED_FILTERS = ("all", "enabled", "disabled")
CHANNEL_FILTERS = ("all", "ar", "solo_default")
NO_LANGUAGE = "-"   # valor de ?lang= para los productos sin ninguna traducción


@dataclass(frozen=True, slots=True)
class Filters:
    rule: str | None = None
    enabled: str = "all"
    lang: str | None = None
    channel: str = "all"
    q: str | None = None
    only_issues: bool = True


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_query(q: str | None) -> str:
    """El texto de búsqueda sin caracteres de control: un NUL no entra en un parámetro de
    texto de Postgres (daba un 500) y en una búsqueda no significa nada."""
    return _CONTROL_RE.sub("", q or "").strip()


def _filtered(stmt, run_id: int, f: Filters, *, with_rule: bool):
    stmt = stmt.where(TextAuditItem.run_id == run_id)
    if f.only_issues:
        stmt = stmt.where(TextAuditItem.n_issues > 0)
    if with_rule and f.rule:
        stmt = stmt.where(TextAuditItem.issues.contains(f",{f.rule},", autoescape=True))  # type: ignore[attr-defined]
    if f.enabled == "enabled":
        stmt = stmt.where(TextAuditItem.enabled == True)  # noqa: E712
    elif f.enabled == "disabled":
        stmt = stmt.where(TextAuditItem.enabled == False)  # noqa: E712
    if f.lang:
        stmt = stmt.where(TextAuditItem.language_code == ("" if f.lang == NO_LANGUAGE else _CONTROL_RE.sub("", f.lang)))
    if f.channel == "ar":
        stmt = stmt.where(TextAuditItem.in_ar == True)  # noqa: E712
    elif f.channel == "solo_default":
        stmt = stmt.where(TextAuditItem.in_default == True, TextAuditItem.in_ar == False)  # noqa: E712
    q = clean_query(f.q)
    if q:
        needle = q.lower()
        stmt = stmt.where(or_(
            func.lower(TextAuditItem.name).contains(needle, autoescape=True),
            func.lower(TextAuditItem.slug).contains(needle, autoescape=True),
            func.lower(TextAuditItem.product_code).contains(needle, autoescape=True),
            TextAuditItem.product_id == q,
        ))
    return stmt


def item_to_dict(item: TextAuditItem) -> dict[str, Any]:
    details = json.loads(item.details) if item.details else {}
    return {
        "product_id": item.product_id,
        "product_code": item.product_code,
        "language": item.language_code,
        "name": item.name,
        "slug": item.slug,
        "enabled": item.enabled,
        "in_ar": item.in_ar,
        "in_default": item.in_default,
        "name_len": item.name_len,
        "desc_chars": item.desc_chars,
        "n_issues": item.n_issues,
        "issues": [
            {"rule": r, "detail": details.get(r, "")} for r in item.issues.strip(",").split(",") if r
        ],
    }


def facet_counts(session: Session, run_id: int, f: Filters) -> tuple[dict[str, int], int]:
    """({regla: productos distintos}, productos distintos) bajo los filtros menos el de regla,
    para los chips. Una sola consulta que agrega en SQL: no trae las filas a memoria."""
    cols = [func.count(distinct(TextAuditItem.product_id))]
    cols += [
        func.count(distinct(case(
            (TextAuditItem.issues.contains(f",{rule},", autoescape=True), TextAuditItem.product_id),  # type: ignore[attr-defined]
        )))
        for rule in rules.RULE_IDS
    ]
    row = session.execute(_filtered(select(*cols), run_id, f, with_rule=False)).one()
    counts = {rule: int(n) for rule, n in zip(rules.RULE_IDS, row[1:]) if n}
    return counts, int(row[0])


def query_items(
    session: Session, run_id: int, f: Filters, page: int, page_size: int,
) -> dict[str, Any]:
    total = session.exec(_filtered(select(func.count()).select_from(TextAuditItem), run_id, f, with_rule=True)).one()
    rows = session.exec(
        _filtered(select(TextAuditItem), run_id, f, with_rule=True)
        .order_by(TextAuditItem.n_issues.desc(), TextAuditItem.id)  # type: ignore[attr-defined]
        .offset(page * page_size).limit(page_size)
    ).all()
    counts, facet_products = facet_counts(session, run_id, f)
    languages = [
        (code or NO_LANGUAGE) for code in session.exec(
            select(TextAuditItem.language_code).where(TextAuditItem.run_id == run_id).distinct()
            .order_by(TextAuditItem.language_code)
        ).all()
    ]
    return {
        "total": int(total),
        "items": [item_to_dict(r) for r in rows],
        "counts": counts,
        "facet_products": facet_products,
        "languages": languages,
    }


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS = (
    "producto_id", "codigo", "idioma", "habilitado", "canal_ar", "canal_default", "nombre",
    "largo_nombre", "slug", "cantidad_problemas", "problemas", "detalle",
)
# Excel en es-AR separa las columnas con «;» (la coma es el separador decimal): un CSV con comas
# se abre en una sola columna. Para otras herramientas, ?sep=, o ?sep=tab.
CSV_SEPARATORS = {";": ";", ",": ",", "tab": "\t"}
DEFAULT_CSV_SEP = ";"


def csv_safe(value: object) -> str:
    """Texto de una celda sin que una planilla lo tome por fórmula (también si antes del «=»
    hay espacios o caracteres de control, que Excel ignora)."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES) or _CONTROL_RE.sub("", text).lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _yes_no(v: bool | None) -> str:
    return "" if v is None else ("si" if v else "no")


@dataclass(frozen=True, slots=True)
class CsvExport:
    text: str
    total: int          # filas que cumplen los filtros
    truncated: bool     # hay más que EXPORT_MAX_ROWS


def export_csv(session: Session, run_id: int, f: Filters, sep: str = DEFAULT_CSV_SEP) -> CsvExport:
    """CSV (UTF-8 con BOM para Excel, separador `sep`) de lo que cumple los filtros, hasta
    EXPORT_MAX_ROWS filas; dice cuántas había en total y si se cortó."""
    delimiter = CSV_SEPARATORS.get(sep, DEFAULT_CSV_SEP)
    total = int(session.exec(
        _filtered(select(func.count()).select_from(TextAuditItem), run_id, f, with_rule=True)).one())
    items = session.exec(
        _filtered(select(TextAuditItem), run_id, f, with_rule=True)
        .order_by(TextAuditItem.n_issues.desc(), TextAuditItem.id)  # type: ignore[attr-defined]
        .limit(EXPORT_MAX_ROWS)
    ).all()
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for item in items:
        d = item_to_dict(item)
        writer.writerow([
            csv_safe(d["product_id"]), csv_safe(d["product_code"]), csv_safe(d["language"]),
            _yes_no(d["enabled"]), _yes_no(d["in_ar"]), _yes_no(d["in_default"]),
            csv_safe(d["name"]), d["name_len"], csv_safe(d["slug"]), d["n_issues"],
            " ".join(i["rule"] for i in d["issues"]),
            csv_safe(" | ".join(f"{i['rule']}: {i['detail']}" for i in d["issues"])),
        ])
    return CsvExport(buf.getvalue(), total, total > EXPORT_MAX_ROWS)
