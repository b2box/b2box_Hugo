"""Prechequeo de Luis: ¿este candidato compite en Mercado Libre? ANTES de pagar la búsqueda de Paco en 1688.

    POST /radar/precheck   {items: [{ref, titulo_es, titulo_zh, imagen_url, precio_cny_moq, moq, peso_kg, medidas_cm,
                                     categoria}]}  (hasta 50)         → {items: [{ref, estado}]}
    GET  /radar/precheck?refs=a,b                                    → el veredicto ESTIMADO de cada uno

Misma auth que `/radar/ml-supply` (X-API-Key de `HUGO_API_KEYS`, cliente `luis`). Apagado por default
(`radar_precheck_enabled = 0`): responde `estado: "error"` con motivo «apagado».

Cómo se mide (sin caminos nuevos hacia ML):

  1. Cada candidato queda en `radar_precheck` como `pendiente` (tope de candidatos nuevos por día
     `radar_precheck_daily_max`). La búsqueda en ML va por el camino VIGENTE, Apify (`pricing/apify_ml_search.py`, actor
     `devcake/mercadolibre-scraper`, `search_one`): `search_pending` reserva cada id `pc-<id>` con `oficina_ml.claim` (así
     nadie más lo busca a la vez), gasta su PROPIO cupo diario atómico (`radar_precheck_apify_daily_max`, aparte del de la
     noche), guarda el resultado por `oficina_ml.ingest` (origen `apify`, con los «vendidos» que trae el actor) y anota el
     gasto de cada corrida en `apify_spend` (etiqueta `radar_precheck`). Corre al recibir el pedido y cada 15 min. Sin
     cupo, queda pendiente para el día siguiente. Respaldo: la Mac de la oficina recibe los pendientes en su cola
     (`radar_precheck_queue_share_pct`), si está prendida; el job nocturno de Apify NO los toma (no gasta su cupo).
  2. Cuando hay resultado (`ml_web_result` con ese id), `process_pending` filtra «mismo producto» con las reglas del
     semáforo (CLIP + nombre, `market_match.score_candidates`; la foto del candidato se embebe al vuelo, como la de un
     producto deshabilitado). Solo los IGUAL con precio en pesos cuentan: mediana, cantidad, publicaciones y VENTAS
     (`sold_quantity` de la polycard de ML).
  3. Precio de la app ESTIMADO = precio de fábrica × factor de los desgloses reales de Vendure (`price_factor`); P_max =
     la cuenta de `price_diagnosis.price_max_cents`; color y ganancia con `semaforo` (los mismos cortes y costos de ML).

Todo lo que vuelve dice `estimado: true`: no es el semáforo firme (no hay precio real de Vendure).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import MlWebResult, RadarPrecheck
from app.db.session import engine
from app.pricing import daily_budget, market_match, price_factor, semaforo
from app.pricing.market_ml import MlCandidate
from app.pricing.market_ml_web import clean_line
from app.pricing.price_diagnosis import price_max_cents

log = logging.getLogger(__name__)

PENDIENTE, LISTO, ERROR = "pendiente", "listo", "error"
PREFIX = "pc-"
MAX_ITEMS = 50
COUNTER_KEY = "radar_precheck_new"
REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$", re.ASCII)
MAX_ATTEMPTS = 3
MATCHES_KEPT = 10


def is_enabled() -> bool:
    return int(runtime.get("radar_precheck_enabled") or 0) == 1


def queue_id(row_id: int) -> str:
    return f"{PREFIX}{int(row_id)}"


def row_id_of(product_id: str) -> int | None:
    if not isinstance(product_id, str) or not product_id.startswith(PREFIX):
        return None
    tail = product_id[len(PREFIX):]
    return int(tail) if tail.isdigit() and len(tail) <= 12 else None


def build_query(titulo_es: str) -> str:
    return market_match.search_query(clean_line(titulo_es, 200))


def _ttl() -> timedelta:
    return timedelta(days=max(1, int(runtime.get("radar_precheck_ttl_days") or 15)))


# ─── 1. Recibir ───────────────────────────────────────────────────


def submit(items: Sequence[dict[str, Any]], client: str | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Encola los candidatos (ya validados por la ruta). Devuelve `{ref, estado}` por cada uno, en el mismo orden."""
    now = now or utcnow()
    if not is_enabled():
        return [{"ref": it["ref"], "estado": ERROR, "motivo": "apagado"} for it in items]
    from app.ingest.apify_1688 import allowed_image_url

    cap = int(runtime.get("radar_precheck_daily_max") or 0)
    out: list[dict[str, Any]] = []
    for it in items:
        ref = it["ref"]
        image = allowed_image_url(it.get("imagen_url"))
        query = build_query(it.get("titulo_es") or "")
        if not image or not query:
            out.append({"ref": ref, "estado": ERROR, "motivo": "falta la foto (https pública) o el título"})
            continue
        with Session(engine, expire_on_commit=False) as s:
            row = s.exec(select(RadarPrecheck).where(RadarPrecheck.ref == ref)).first()
            same = row is not None and row.query == query and row.imagen_url == image[:2048] \
                and math.isclose(row.precio_cny_moq, float(it["precio_cny_moq"]))
            if row is not None and same and (row.estado == PENDIENTE or (
                    row.estado == LISTO and row.medido_at is not None and now - row.medido_at < _ttl())):
                out.append({"ref": ref, "estado": row.estado})
                continue
        if daily_budget.reserve(COUNTER_KEY, cap, None, None, daily_budget.today_art(now)) is None:
            out.append({"ref": ref, "estado": ERROR, "motivo": "tope diario"})
            continue
        medidas = it.get("medidas_cm")
        with Session(engine, expire_on_commit=False) as s:
            row = s.exec(select(RadarPrecheck).where(RadarPrecheck.ref == ref)).first() or RadarPrecheck(ref=ref,
                                                                                                         created_at=now)
            row.client = (client or "")[:40] or None
            row.titulo_es, row.titulo_zh = clean_line(it.get("titulo_es"), 300), clean_line(it.get("titulo_zh"), 300)
            row.imagen_url, row.query = image[:2048], query
            row.precio_cny_moq, row.moq = float(it["precio_cny_moq"]), int(it.get("moq") or 1)
            row.peso_kg = it.get("peso_kg")
            row.medidas_cm = json.dumps(list(medidas)) if medidas else None
            row.categoria = clean_line(it.get("categoria"), 80) or None
            # A los segundos: `fetched_at` de la búsqueda llega sin microsegundos y se compara contra esto.
            row.requested_at, row.estado, row.attempts = now.replace(microsecond=0), PENDIENTE, 0
            row.color = row.motivo = row.factor = row.matches = None
            row.ml_median_cents = row.precio_app_cents = row.precio_max_cents = row.vendidos_total = None
            row.margen_pct, row.n_identicos, row.publicaciones, row.medido_at = None, 0, 0, None
            s.add(row)
            s.commit()
        out.append({"ref": ref, "estado": PENDIENTE})
    return out


# ─── 2. La cola de búsqueda (oficina / Apify) ─────────────────────


def queue_entries(limit: int, *, now: datetime | None = None, taken: set[str] | None = None) -> list[dict[str, Any]]:
    """Los prechequeos pendientes que todavía no tienen búsqueda después de pedirse (el más viejo primero), con la forma
    de la cola de la oficina: `{product_id: "pc-<id>", queries: [consulta]}`. Vacío si está apagado."""
    if limit <= 0 or not is_enabled():
        return []
    taken = taken or set()
    with Session(engine) as s:
        rows = s.exec(select(RadarPrecheck).where(RadarPrecheck.estado == PENDIENTE)
                      .order_by(RadarPrecheck.requested_at, RadarPrecheck.id).limit(limit * 4)).all()  # type: ignore[arg-type]
        ids = [queue_id(int(r.id)) for r in rows]  # type: ignore[arg-type]
        tried = {pid: at for pid, at in s.exec(select(MlWebResult.product_id, MlWebResult.fetched_at)
                                               .where(MlWebResult.product_id.in_(ids))).all()} if ids else {}  # type: ignore[attr-defined]
    out: list[dict[str, Any]] = []
    for r in rows:
        pid = queue_id(int(r.id))  # type: ignore[arg-type]
        if pid in taken or (pid in tried and tried[pid] >= r.requested_at):
            continue
        out.append({"product_id": pid, "queries": [r.query]})
        if len(out) >= limit:
            break
    return out


def known_ids(product_ids: Sequence[str]) -> set[str]:
    """De estos ids de la cola, los que son prechequeos que existen (para que `oficina_ml.ingest` los acepte)."""
    wanted = {pid: rid for pid in product_ids if (rid := row_id_of(pid)) is not None}
    if not wanted:
        return set()
    with Session(engine) as s:
        found = set(s.exec(select(RadarPrecheck.id).where(RadarPrecheck.id.in_(list(wanted.values())))).all())  # type: ignore[union-attr]
    return {pid for pid, rid in wanted.items() if rid in found}


# ─── 2b. Buscar con Apify (camino vigente) ────────────────────────

APIFY_COUNTER_KEY = "radar_precheck_apify"
SPEND_LABEL = "radar_precheck"
CLAIM_PREFIX = "apify-pc-"


def apify_reason() -> str | None:
    """Por qué el prechequeo no puede buscar con Apify, o None. Usa el token, el actor y el build de la búsqueda de la
    noche, pero NO su interruptor ni su cupo: tiene los suyos."""
    from app.config import get_settings
    from app.pricing import apify_ml_search as ams

    if not is_enabled():
        return "apagado"
    if not get_settings().apify_token.strip():
        return "falta APIFY_TOKEN"
    if not ams._actor():
        return "el actor configurado no está en la lista blanca"
    if ams._build() is None:
        return "el build configurado no es válido"
    return None


def _record_spend(outcomes: Sequence[Any]) -> None:
    from app.db.models import ApifySpend

    try:
        with Session(engine) as s:
            for o in outcomes:
                if not o.run_id and not o.usd_imputado:
                    continue
                usd = o.usage_usd if o.usage_usd else o.usd_imputado
                s.add(ApifySpend(label=SPEND_LABEL, run_id=(o.run_id or "")[:40], terms=1, usd=round(float(usd), 6),
                                 estimated=not o.usage_usd))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("precheck: no se pudo registrar el gasto de Apify (%s)", type(exc).__name__)


async def search_pending(*, limit: int = 20, http: Any = None, now: datetime | None = None) -> dict[str, int]:
    """Busca en ML con Apify los prechequeos pendientes sin búsqueda. Nunca lanza; devuelve contadores."""
    import asyncio
    import secrets

    from app.config import get_settings
    from app.pricing import apify_ml_search as ams
    from app.pricing import oficina_ml

    counts: dict[str, int] = {}
    reason = apify_reason()
    if reason:
        return {"apagado": 1} if reason == "apagado" else {"sin_apify": 1}
    now = now or utcnow()
    entries = await asyncio.to_thread(queue_entries, limit, now=now)
    if not entries:
        return counts
    s = get_settings()
    actor, build = ams._actor(), ams._build() or ""
    timeout_s, batch = int(s.apify_meli_search_timeout_s), max(1, int(s.apify_meli_search_concurrency))
    cap = int(runtime.get("radar_precheck_apify_daily_max") or 0)
    claimer = f"{CLAIM_PREFIX}{secrets.token_hex(3)}"
    headers = {"Authorization": f"Bearer {s.apify_token.strip()}", "Accept": "application/json"}
    own = http is None
    http = http or ams._new_client(ams._POLL_WAIT_S + 15.0)
    try:
        level = await ams._actor_permission(http, actor, headers)
        if level != ams._REQUIRED_PERMISSION:
            log.warning("precheck: el actor de Apify no corre con permisos limitados (%s)", level or "desconocido")
            return {"actor_sin_permisos_limitados": 1}
        queries = {e["product_id"]: e["queries"][0] for e in entries}
        pids = list(queries)
        for at in range(0, len(pids), batch):
            chunk = pids[at:at + batch]
            claimed = await asyncio.to_thread(oficina_ml.claim, chunk, claimer, ams._CLAIM_LEASE)
            if not claimed:
                continue
            granted = await asyncio.to_thread(daily_budget.reserve_up_to, APIFY_COUNTER_KEY, cap, len(claimed))
            todo, spare = claimed[:granted], claimed[granted:]
            if spare:
                await asyncio.to_thread(oficina_ml.release, claimer, spare)
                counts["tope"] = counts.get("tope", 0) + len(spare)
            if todo:
                outcomes = await asyncio.gather(*(
                    ams.search_one(http, pid, queries[pid], actor=actor, build=build, headers=headers,
                                   timeout_s=timeout_s, limit=HARD_MAX) for pid in todo))
                await asyncio.to_thread(oficina_ml.ingest, [o.as_result() for o in outcomes], origin=ams.ORIGIN_APIFY)
                await asyncio.to_thread(_record_spend, outcomes)
                await asyncio.to_thread(oficina_ml.release, claimer, todo)
                for o in outcomes:
                    counts[o.status] = counts.get(o.status, 0) + 1
                    if o.status == "error":
                        await asyncio.to_thread(_fail, int(row_id_of(o.product_id) or 0), o.reason or "Apify falló")
                if any(o.fatal for o in outcomes):
                    counts["fatal"] = 1
                    break
            if granted < len(claimed):
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("precheck: la búsqueda con Apify se cortó (%s)", type(exc).__name__)
        counts["error"] = counts.get("error", 0) + 1
    finally:
        await asyncio.to_thread(oficina_ml.release, claimer)
        if own:
            await http.aclose()
    if counts:
        log.info("precheck/Apify: %s", counts)
    return counts


HARD_MAX = 24        # = oficina_ml.HARD_MAX_CANDIDATES; ingest recorta a `pm_ml_web_max_results`

_cycle_lock: Any = None


async def run_cycle() -> dict[str, Any]:
    """Buscar lo pendiente con Apify y medir lo que ya tiene búsqueda. Una vuelta a la vez por proceso; nunca lanza."""
    import asyncio

    global _cycle_lock
    if _cycle_lock is None:
        _cycle_lock = asyncio.Lock()
    if not is_enabled() or _cycle_lock.locked():
        return {}
    async with _cycle_lock:
        searched = await search_pending()
        measured = await process_pending(limit=100)
    return {"busqueda": searched, "medicion": measured}


# ─── 3. Medir (puro) ──────────────────────────────────────────────


def verdict(*, matches: Sequence[MlCandidate], publicaciones: int, precio_app_cents: int | None,
            commission_pct: float, shipping_cents: int, green_min: float, yellow_min: float,
            min_listings: int) -> dict[str, Any]:
    """El veredicto ESTIMADO con los IGUAL ya filtrados. PURA."""
    prices = [int(c.price_cents) for c in matches if c.price_cents and (c.currency or "ARS").upper() == "ARS"]
    sold = [int(c.sold_quantity) for c in matches if c.sold_quantity is not None]
    median = semaforo.median_cents(prices)
    margin = semaforo.estimated_margin_pct(median, precio_app_cents, commission_pct, shipping_cents)
    color = semaforo.color(margin, precio_app_cents, median, green_min, yellow_min)
    pmax = price_max_cents(median, commission_pct, shipping_cents, yellow_min) if median else None
    if median is None:
        motivo = "Sin idénticos con precio en Mercado Libre" if publicaciones else "Mercado Libre no devolvió publicaciones"
    elif precio_app_cents is None:
        motivo = "Hay idénticos, pero no se pudo estimar nuestro precio (faltan desgloses de Vendure)"
        color = semaforo.SIN_DATO
    else:
        motivo = (f"{len(prices)} idéntico(s); mediana ML vs nuestro precio estimado"
                  + (f" (pocos: el semáforo firme pide {min_listings})" if len(prices) < min_listings else ""))
    return {
        "color": color, "ml_median_cents": median, "n_identicos": len(prices),
        "vendidos_total": sum(sold) if sold else None, "publicaciones": int(publicaciones),
        "precio_max_cents": int(round(pmax)) if pmax is not None and pmax > 0 else None,
        "margen_pct": None if margin is None or not math.isfinite(margin) else round(margin, 1), "motivo": motivo,
    }


# ─── 4. Procesar lo que ya tiene búsqueda ─────────────────────────


def _our_product(row: RadarPrecheck):
    from app.vendure.client import VendureProduct

    return VendureProduct(id=queue_id(int(row.id)), name=row.titulo_es, slug="", description="", enabled=False,  # type: ignore[arg-type]
                          source_url=None, image_urls=[row.imagen_url], product_code=None,
                          featured_image_url=row.imagen_url, first_variant_price_cents=None, variant_count=0)


def _due(refs: Sequence[str] | None, limit: int) -> list[tuple[RadarPrecheck, MlWebResult]]:
    from app.pricing import oficina_ml

    with Session(engine, expire_on_commit=False) as s:
        stmt = select(RadarPrecheck).where(RadarPrecheck.estado == PENDIENTE)
        if refs:
            stmt = stmt.where(RadarPrecheck.ref.in_(list(refs)))  # type: ignore[attr-defined]
        rows = s.exec(stmt.order_by(RadarPrecheck.requested_at).limit(limit)).all()  # type: ignore[arg-type]
        out: list[tuple[RadarPrecheck, MlWebResult]] = []
        for r in rows:
            res = s.exec(select(MlWebResult).where(
                MlWebResult.product_id == queue_id(int(r.id)),  # type: ignore[arg-type]
                MlWebResult.status.in_(oficina_ml.RESULT_STATUSES),  # type: ignore[attr-defined]
                MlWebResult.fetched_at >= r.requested_at).order_by(MlWebResult.fetched_at.desc()).limit(1)).first()  # type: ignore[union-attr]
            if res is not None:
                out.append((r, res))
    return out


async def process_pending(refs: Sequence[str] | None = None, *, limit: int = 50, scorer: Any = None,
                          now: datetime | None = None) -> dict[str, int]:
    """Mide los pendientes que ya tienen búsqueda en ML. Nunca lanza. Sin CLIP listo quedan pendientes (no se inventa)."""
    import asyncio

    from app.dedup import catalog_index, image_embed
    from app.pricing import oficina_ml

    counts: dict[str, int] = {}
    now = now or utcnow()
    try:
        due = await asyncio.to_thread(_due, refs, limit)
        if not due:
            return counts
        if scorer is None and not (image_embed.available() and catalog_index.is_ready()):
            counts["sin_clip"] = len(due)
            return counts
        samples = await asyncio.to_thread(price_factor.load_samples)
        thr = market_match.Thresholds.from_runtime()
        g = runtime.get
        for row, res in due:
            try:
                raw = json.loads(res.candidates or "[]")
            except ValueError:
                raw = []
            cands = oficina_ml.sanitize_candidates(raw, oficina_ml.HARD_MAX_CANDIDATES)
            kwargs = {"scorer": scorer} if scorer is not None else {}
            try:
                decisions = await market_match.score_candidates(_our_product(row), cands, thr, **kwargs)
            except Exception as exc:  # noqa: BLE001
                log.info("precheck %s: el filtro falló (%s)", row.ref, type(exc).__name__)
                await asyncio.to_thread(_fail, int(row.id), "no se pudo comparar la foto")  # type: ignore[arg-type]
                counts["error"] = counts.get("error", 0) + 1
                continue
            matches = [d.candidate for d in decisions if d.verdict == market_match.MATCH]
            medidas = json.loads(row.medidas_cm) if row.medidas_cm else None
            factor = price_factor.compute(samples, cny_unit=row.precio_cny_moq, category=row.categoria,
                                          medidas_cm=medidas)
            app_cents = price_factor.estimate_cents(factor, row.precio_cny_moq)
            v = verdict(matches=matches, publicaciones=len(cands), precio_app_cents=app_cents,
                        commission_pct=float(g("pm_ml_commission_pct")), shipping_cents=int(g("pm_ml_shipping_cents")),
                        green_min=float(g("pm_green_min_pct")), yellow_min=float(g("pm_yellow_min_pct")),
                        min_listings=int(g("pm_review_min_listings")))
            await asyncio.to_thread(_save, int(row.id), v, app_cents, factor.how, matches, now)  # type: ignore[arg-type]
            counts["listo"] = counts.get("listo", 0) + 1
    except Exception as exc:  # noqa: BLE001
        log.warning("precheck: se cortó (%s)", type(exc).__name__)
    return counts


def _save(row_id: int, v: dict[str, Any], app_cents: int | None, how: dict[str, Any], matches: Sequence[MlCandidate],
          now: datetime) -> None:
    with Session(engine) as s:
        row = s.get(RadarPrecheck, row_id)
        if row is None:
            return
        row.estado, row.color, row.ml_median_cents = LISTO, v["color"], v["ml_median_cents"]
        row.n_identicos, row.vendidos_total, row.publicaciones = v["n_identicos"], v["vendidos_total"], v["publicaciones"]
        row.precio_app_cents, row.precio_max_cents, row.margen_pct = app_cents, v["precio_max_cents"], v["margen_pct"]
        row.motivo = v["motivo"][:300]
        row.factor = json.dumps(how, ensure_ascii=False)[:1000]
        row.matches = json.dumps([{"titulo": c.name, "precio_cents": c.price_cents, "vendidos": c.sold_quantity,
                                   "link": c.permalink} for c in list(matches)[:MATCHES_KEPT]], ensure_ascii=False)
        row.medido_at = now
        s.add(row)
        s.commit()


def _fail(row_id: int, why: str) -> None:
    with Session(engine) as s:
        row = s.get(RadarPrecheck, row_id)
        if row is None:
            return
        row.attempts += 1
        if row.attempts >= MAX_ATTEMPTS:
            row.estado, row.motivo = ERROR, why[:300]
        s.add(row)
        s.commit()


# ─── 5. Responder ─────────────────────────────────────────────────


def _iso(moment: datetime | None) -> str | None:
    return moment.replace(microsecond=0).isoformat() + "Z" if moment else None


def _pesos(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100.0, 2)


def to_wire(row: RadarPrecheck | None, ref: str) -> dict[str, Any]:
    if row is None:
        return {"ref": ref, "estado": ERROR, "motivo": "no existe: mandalo primero con POST /radar/precheck",
                "estimado": True}
    try:
        how = json.loads(row.factor) if row.factor else None
    except ValueError:
        how = None
    return {
        "ref": row.ref, "estado": row.estado, "color": row.color if row.estado == LISTO else None,
        "ml": {"mediana_ars": _pesos(row.ml_median_cents), "n_identicos": row.n_identicos,
               "vendidos_total": row.vendidos_total, "publicaciones": row.publicaciones},
        "precio_app_estimado_ars": _pesos(row.precio_app_cents), "precio_max_ars": _pesos(row.precio_max_cents),
        "margen_revendedor_pct": row.margen_pct, "motivo": row.motivo, "estimado": True,
        "medido_at": _iso(row.medido_at), "factor": how,
    }


def lookup(refs: Sequence[str]) -> list[dict[str, Any]]:
    with Session(engine) as s:
        rows = {r.ref: r for r in s.exec(select(RadarPrecheck).where(RadarPrecheck.ref.in_(list(refs)))).all()}  # type: ignore[attr-defined]
    return [to_wire(rows.get(ref), ref) for ref in refs]
