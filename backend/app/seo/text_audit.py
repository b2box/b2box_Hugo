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
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_
from sqlmodel import Session, func, select

from app.clock import utcnow
from app.config import get_settings
from app.db.models import TextAuditItem, TextAuditRun
from app.db.session import engine
from app.seo import lists as seo_lists
from app.seo import text_rules as rules
from app.vendure.client import ProductTexts, TextsRead, VendureClient

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
    return TextAuditItem(
        run_id=0,
        product_id=m.product.id,
        language_code=language,
        name=name,
        slug=slug,
        enabled=m.product.enabled,
        product_code=m.product.product_code,
        in_ar=m.in_ar,
        in_default=m.in_default,
        name_len=len(name.strip()),
        desc_chars=len(rules.plain_text(description)),
    )


def evaluate(merged: Iterable[MergedProduct], lists: seo_lists.TextLists) -> list[TextAuditItem]:
    """Una fila por producto e idioma, con las reglas que saltaron."""
    technical = frozenset(t.upper() for t in lists.technical)
    rows: list[TextAuditItem] = []
    found: dict[int, dict[str, str]] = {}   # id(fila) -> {regla: detalle}
    for m in merged:
        p = m.product
        matcher = rules.SupplierMatcher(
            rules.SupplierRefs(p.supplier_business, p.supplier_size_model, p.supplier_link), technical,
        )
        translations = p.translations or []
        langs = [rules.normalize_lang(t.language) for t in translations]
        missing = None if "es_AR" in langs else rules.missing_es_ar_detail(langs)
        product_rows: list[TextAuditItem] = []
        if not translations:
            product_rows.append(_row(m, "", "", "", ""))
            found[id(product_rows[0])] = rules.audit_translation(
                "", "", "", product_code=p.product_code, lists=lists, matcher=matcher)
        for t, lang in zip(translations, langs):
            row = _row(m, lang, t.name, t.slug, t.description)
            product_rows.append(row)
            found[id(row)] = rules.audit_translation(
                t.name, t.slug, t.description,
                product_code=p.product_code, lists=lists, matcher=matcher,
            )
        if missing:
            for row in product_rows:
                found[id(row)]["SIN_ES_AR"] = missing
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
        for key, dups in rules.find_duplicates(entries).items():
            found[id(row_by_key[key])].update(dups)

    for row in rows:
        ordered = rules.sort_rules(found[id(row)])
        row.n_issues = len(ordered)
        row.issues = f",{','.join(ordered)}," if ordered else ""
        row.details = json.dumps({r: found[id(row)][r] for r in ordered}, ensure_ascii=False) if ordered else None
    return rows


def rule_counts(rows: Iterable[TextAuditItem]) -> dict[str, int]:
    """{regla: productos distintos} (un producto con la misma regla en dos idiomas cuenta una vez)."""
    seen: dict[str, set[str]] = {}
    for row in rows:
        for rule in row.issues.strip(",").split(","):
            if rule:
                seen.setdefault(rule, set()).add(row.product_id)
    return {rule: len(seen[rule]) for rule in rules.sort_rules(seen)}


# ─── Corrida ───────────────────────────────────────────────────────

def _short(exc: BaseException) -> str:
    msg = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {msg}"[:300] if msg else type(exc).__name__


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
            failed[label] = _short(res)
            log.error("Auditoría de textos: no pude leer el canal %s: %s", label, failed[label])
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
                raise RuntimeError("no se pudo leer ningún canal de Vendure: "
                                   + "; ".join(f"{k}: {v}" for k, v in failed.items()))
            if not any(c == CHANNEL_AR for c, _ in channels_to_read()):
                notes.append("VENDURE_CHANNEL_TOKEN vacío: solo se leyó el canal por defecto "
                             "y «canal AR» queda sin dato.")
            if any(not r.supplier_fields for r in reads.values()):
                notes.append("Vendure no expone supplierBusiness/supplierSizeModel: "
                             "la regla FAB solo compara el link del proveedor.")
            merged = merge_channels(reads)
            items = evaluate(merged, lists)
            counts = rule_counts(items)
            with Session(engine) as session:
                run = session.get(TextAuditRun, run_id)
                assert run is not None
                for item in items:
                    item.run_id = run_id
                session.add_all(items)
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
                run.duration_s = round(time.monotonic() - t0, 1)
                session.add(run)
                session.commit()
                _prune(session)
                session.refresh(run)
                result = run_to_dict(run)
        except Exception as exc:  # noqa: BLE001
            log.exception("seo_text_audit falló")
            with Session(engine) as session:
                run = session.get(TextAuditRun, run_id)
                if run is not None:
                    run.status = RUN_FAILED
                    run.error = _short(exc)
                    run.finished_at = utcnow()
                    run.duration_s = round(time.monotonic() - t0, 1)
                    session.add(run)
                    session.commit()
                    result = run_to_dict(run)
                else:  # pragma: no cover - la fila la creamos arriba
                    result = {"id": run_id, "status": RUN_FAILED, "error": _short(exc)}
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
        stmt = stmt.where(TextAuditItem.language_code == ("" if f.lang == NO_LANGUAGE else f.lang))
    if f.channel == "ar":
        stmt = stmt.where(TextAuditItem.in_ar == True)  # noqa: E712
    elif f.channel == "solo_default":
        stmt = stmt.where(TextAuditItem.in_default == True, TextAuditItem.in_ar == False)  # noqa: E712
    if f.q and f.q.strip():
        needle = f.q.strip().lower()
        stmt = stmt.where(or_(
            func.lower(TextAuditItem.name).contains(needle, autoescape=True),
            func.lower(TextAuditItem.slug).contains(needle, autoescape=True),
            func.lower(TextAuditItem.product_code).contains(needle, autoescape=True),
            TextAuditItem.product_id == f.q.strip(),
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


def query_items(
    session: Session, run_id: int, f: Filters, page: int, page_size: int,
) -> dict[str, Any]:
    total = session.exec(_filtered(select(func.count()).select_from(TextAuditItem), run_id, f, with_rule=True)).one()
    rows = session.exec(
        _filtered(select(TextAuditItem), run_id, f, with_rule=True)
        .order_by(TextAuditItem.n_issues.desc(), TextAuditItem.id)  # type: ignore[attr-defined]
        .offset(page * page_size).limit(page_size)
    ).all()
    # Conteos por regla bajo los demás filtros (sin el de regla), para los chips.
    facet = session.exec(_filtered(select(TextAuditItem), run_id, f, with_rule=False)).all()
    languages = [
        (code or NO_LANGUAGE) for code in session.exec(
            select(TextAuditItem.language_code).where(TextAuditItem.run_id == run_id).distinct()
            .order_by(TextAuditItem.language_code)
        ).all()
    ]
    return {
        "total": int(total),
        "items": [item_to_dict(r) for r in rows],
        "counts": rule_counts(facet),
        "facet_products": len({r.product_id for r in facet}),
        "languages": languages,
    }


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS = (
    "producto_id", "codigo", "idioma", "habilitado", "canal_ar", "canal_default", "nombre",
    "largo_nombre", "slug", "cantidad_problemas", "problemas", "detalle",
)


def csv_safe(value: object) -> str:
    """Texto de una celda sin que una planilla lo tome por fórmula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _yes_no(v: bool | None) -> str:
    return "" if v is None else ("si" if v else "no")


def export_csv(session: Session, run_id: int, f: Filters) -> str:
    """CSV (coma, UTF-8 con BOM para Excel) de todo lo que cumple los filtros."""
    items = session.exec(
        _filtered(select(TextAuditItem), run_id, f, with_rule=True)
        .order_by(TextAuditItem.n_issues.desc(), TextAuditItem.id)  # type: ignore[attr-defined]
        .limit(EXPORT_MAX_ROWS)
    ).all()
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf, lineterminator="\r\n")
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
    return buf.getvalue()
