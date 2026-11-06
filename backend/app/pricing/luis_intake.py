"""Lo que manda Luis, de punta a punta: Google Lens al recibirlo y semáforo al subirlo a Vendure.

Flujo (pestaña "Subidos a Vendure" del dashboard):

  1. Luis pide `POST /verify` con `dedup_only` y Hugo dice que NO está en el catálogo: `record()` guarda una fila por
     oferta de 1688 (`LuisIntake`, clave = offer id del link) y `lens_soon()` pasa la foto por Google Lens en segundo
     plano (actor `s-r/google-lens`, ~USD 0,005 por foto con matches). De Lens se guardan las publicaciones de Mercado
     Libre Argentina con su precio en ARS: título, precio, link y dominio.
  2. Paco lo sube a Vendure y Vendure le avisa a Hugo (`POST /api/vendure/events`): `compare_products()` lee el producto,
     lo reconoce por el `supplierLink` (mismo offer id de 1688) y compara NUESTRO precio contra la mediana de esas
     publicaciones con las reglas del semáforo (semaforo.estimated_margin_pct / semaforo.color): verde, amarillo, rojo,
     o sin_dato si Lens no trajo ninguna publicación de ML con precio.

No busca en ML (solo lo que trajo Lens: cero tráfico a ML) y no toca los snapshots del semáforo de la noche.
Gasto acotado: interruptor `luis_lens_enabled` y cupo diario atómico `luis_lens_daily_max` (fotos por día UTC).
Seguridad: lo que vuelve del actor es de un tercero; se guardan solo links https a dominios de ML de la lista y el
título pasa por clean_line. El token de Apify va solo en el header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.config import get_settings
from app.db.models import LuisIntake
from app.db.session import engine
from app.estimate.otapi_item import offer_id_from_url
from app.ingest.apify_1688 import _json, _request_bounded, _Run, allowed_image_url
from app.pricing import apify_ml_search as ams
from app.pricing import daily_budget, ml_lens, price_monitor, price_review, semaforo
from app.pricing.market_ml_web import clean_line

log = logging.getLogger(__name__)

COUNTER_KEY = "_meta:luis_lens_today"
# Estados de la fila.
LENS_PENDING, LENS_OK, LENS_EMPTY, LENS_ERROR, LENS_OFF = "pendiente", "ok", "sin_ml", "error", "apagado"
WAITING = "esperando_subida"
COMPARED = "comparado"
NO_PRICE = "sin_precio"
MAX_MATCHES_KEPT = 10
_RUN_TIMEOUT_S = 240.0
# "$ 12.999" · "ARS 12.999,50" · "$12,999.00": dígitos con separadores; el último separador con 1-2 dígitos es decimal.
_PRICE_RE = re.compile(r"\d[\d.,\s]*")
_ML_AR_HOSTS = ("mercadolibre.com.ar",)


def is_enabled() -> bool:
    return int(runtime.get("luis_lens_enabled")) == 1


def daily_max() -> int:
    return max(0, int(runtime.get("luis_lens_daily_max")))


# ─── 1. Al recibirlo de Luis ───────────────────────────────────────


def record(source_url: str | None, name: str | None, image_urls: list[str] | None) -> int | None:
    """Guarda (o refresca) la fila de esta oferta de 1688. None si el link no es de una oferta de 1688 o no hay foto
    usable. Idempotente por offer id: Luis puede pedir /verify varias veces por el mismo producto."""
    offer_id = offer_id_from_url(source_url or "")
    image = next((u for u in (image_urls or []) if allowed_image_url(u)), None)
    if not offer_id or not image:
        return None
    with Session(engine, expire_on_commit=False) as s:
        row = s.exec(select(LuisIntake).where(LuisIntake.offer_id == offer_id)).first()
        if row is None:
            row = LuisIntake(offer_id=offer_id, source_url=(source_url or "")[:2048], image_url=image[:2048],
                             name=clean_line(name or "", 300), lens_status=LENS_PENDING, status=WAITING)
            s.add(row)
        else:
            row.name = clean_line(name or "", 300) or row.name
        s.commit()
        return row.id


async def lens_soon(intake_id: int) -> None:
    """Lens para una fila, en segundo plano. Nunca lanza."""
    try:
        await lens_one(intake_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Luis/Lens: la fila %s falló (%s)", intake_id, type(exc).__name__)


async def lens_one(intake_id: int) -> str:
    """Corre Lens para la fila si hace falta (pendiente o error). Devuelve el estado final."""
    with Session(engine) as s:
        row = s.get(LuisIntake, intake_id)
        if row is None:
            return "no_existe"
        if row.lens_status in (LENS_OK, LENS_EMPTY):
            return row.lens_status
        image = row.image_url
    if not is_enabled():
        _set_lens(intake_id, LENS_OFF, [], 0.0)
        return LENS_OFF
    token = get_settings().apify_token.strip()
    if not token:
        _set_lens(intake_id, LENS_OFF, [], 0.0)
        return LENS_OFF
    if await asyncio.to_thread(daily_budget.reserve_up_to, COUNTER_KEY, daily_max(), 1) < 1:
        return LENS_PENDING                                   # sin cupo hoy: queda pendiente para mañana
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with ams._new_client(_RUN_TIMEOUT_S + 30.0) as http:
        level = await ams._actor_permission(http, ml_lens._ACTOR, headers)
        if level != ams._REQUIRED_PERMISSION:
            log.warning("Luis/Lens: el actor no corre con permisos limitados (%s)", level or "desconocido")
            _set_lens(intake_id, LENS_ERROR, [], 0.0)
            return LENS_ERROR
        run = _Run(n=1)
        step = _HttpOnly(http, headers)
        try:
            items = await asyncio.wait_for(ml_lens._drive(step, run, [image]), timeout=_RUN_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001  (timeout o red: la corrida se aborta y la fila queda con error)
            log.warning("Luis/Lens: la corrida falló (%s)", type(exc).__name__)
            if run.run_id:
                await ams._abort(http, run, headers)
            items = None
    usd = max(run.usage_usd, ml_lens._USD_PER_PHOTO)
    if items is None:
        _set_lens(intake_id, LENS_ERROR, [], usd)
        return LENS_ERROR
    item = ml_lens._by_input_index(items, 1).get(0)
    matches = ml_matches(ml_lens._matches(item))
    status = LENS_OK if matches else LENS_EMPTY
    _set_lens(intake_id, status, matches, usd)
    return status


class _HttpOnly:
    """Lo único que ml_lens._drive lee de su `_Step`."""

    def __init__(self, http: Any, headers: dict) -> None:
        self.http, self.headers = http, headers


def parse_price_cents(price: object, currency: object) -> int | None:
    """Precio de un match de Lens en centavos de ARS, o None (otra moneda, vacío o raro)."""
    cur = currency.strip().upper() if isinstance(currency, str) else ""
    if cur not in ("", "ARS", "$"):
        return None
    if not isinstance(price, str) or len(price) > 40:
        return None
    m = _PRICE_RE.search(price)
    if not m:
        return None
    raw = m.group(0).replace(" ", "").strip(".,")
    last = max(raw.rfind(","), raw.rfind("."))
    if last != -1 and 1 <= len(raw) - last - 1 <= 2:
        whole, dec = raw[:last], raw[last + 1:]
    else:
        whole, dec = raw, ""
    whole = whole.replace(".", "").replace(",", "")
    if not whole.isdigit():
        return None
    value = int(whole) * 100 + (int(dec.ljust(2, "0")) if dec else 0)
    return value if 0 < value < 10**12 else None


def ml_matches(matches: list) -> list[dict[str, Any]]:
    """Las publicaciones de Mercado Libre Argentina con precio en ARS, saneadas (lo demás se descarta)."""
    out: list[dict[str, Any]] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        link = m.get("url") if not m.get("link_is_redirect") else None
        host = _ml_host(link)
        domain = m.get("source_domain") if isinstance(m.get("source_domain"), str) else ""
        if host is None and not domain.lower().startswith("mercado"):
            continue
        cur = m.get("currency").strip().upper() if isinstance(m.get("currency"), str) else ""
        # Sin moneda explícita solo vale si el link es de ML Argentina ("$" también es el peso mexicano).
        if cur != "ARS" and host is None:
            continue
        cents = parse_price_cents(m.get("price"), m.get("currency"))
        if cents is None:
            continue
        out.append({"title": clean_line(str(m.get("title") or ""), 200), "price_cents": cents,
                    "url": link if host else None})
        if len(out) >= MAX_MATCHES_KEPT:
            break
    return out


def _ml_host(url: object) -> str | None:
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048:
        return None
    host = url[8:].split("/", 1)[0].lower()
    if "@" in host or ":" in host:
        return None
    return host if any(host == h or host.endswith("." + h) for h in _ML_AR_HOSTS) else None


def _set_lens(intake_id: int, status: str, matches: list[dict[str, Any]], usd: float) -> None:
    prices = [m["price_cents"] for m in matches]
    with Session(engine) as s:
        row = s.get(LuisIntake, intake_id)
        if row is None:
            return
        row.lens_status, row.lens_ran_at, row.lens_usd = status, utcnow(), round(usd, 6)
        row.ml_matches = json.dumps(matches, ensure_ascii=False)
        row.ml_median_cents = semaforo.median_cents(prices) if prices else None
        s.add(row)
        s.commit()


# ─── 2. Al subirlo a Vendure ───────────────────────────────────────


async def compare_products(product_ids: list[str], client: Any = None) -> dict[str, int]:
    """Para cada producto avisado por Vendure: si es uno que mandó Luis (mismo offer id de 1688), compara nuestro precio
    con lo que trajo Lens y guarda el color. Nunca lanza; devuelve contadores para el log."""
    counts: dict[str, int] = {}
    if not product_ids:
        return counts
    try:
        pending = await asyncio.to_thread(_offers_waiting)
        if not pending:
            return counts
        if client is None:
            from app.vendure.client import VendureClient
            client = VendureClient()
        for pid in product_ids[:200]:
            try:
                outcome = await _compare_one(client, pid, pending)
            except Exception as exc:  # noqa: BLE001
                log.warning("Luis/Vendure: el producto %s falló (%s)", pid, type(exc).__name__)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Luis/Vendure: la comparación se cortó (%s)", type(exc).__name__)
    if counts:
        log.info("Luis/Vendure: %s", counts)
    return counts


def _offers_waiting() -> set[str]:
    """Offer ids de 1688 que mandó Luis (todos: un producto ya comparado se recompara si cambia su precio)."""
    with Session(engine) as s:
        return set(s.exec(select(LuisIntake.offer_id)).all())


async def _compare_one(client: Any, pid: str, offers: set[str]) -> str:
    product = await client.get_product_priced(pid)
    if product is None:
        return "no_existe"
    offer_id = offer_id_from_url(product.source_url or "")
    if not offer_id or offer_id not in offers:
        return "no_es_de_luis"
    our = semaforo.pick_our_price(product.priced_variants or [], int(runtime.get("pm_tier_policy")))
    return await asyncio.to_thread(_save_comparison, offer_id, pid, product, our)


def _save_comparison(offer_id: str, pid: str, product: Any, our: semaforo.OurPrice | None) -> str:
    params = price_review.ReviewParams.from_runtime()
    commission = float(runtime.get("pm_ml_commission_pct"))
    shipping = int(runtime.get("pm_ml_shipping_cents"))
    with Session(engine) as s:
        row = s.exec(select(LuisIntake).where(LuisIntake.offer_id == offer_id)).first()
        if row is None:
            return "no_es_de_luis"
        row.vendure_product_id = pid
        row.vendure_name = clean_line(product.name or "", 300)
        row.vendure_image_url = (product.featured_image_url or "")[:2048] or None
        row.uploaded_at = row.uploaded_at or utcnow()
        row.compared_at = utcnow()
        if our is None or (our.currency and our.currency.upper() != price_monitor.CURRENCY):
            row.our_price_cents, row.margin_pct, row.color, row.status = None, None, semaforo.SIN_DATO, NO_PRICE
        else:
            row.our_price_cents = our.price_cents
            median = row.ml_median_cents
            margin = (semaforo.estimated_margin_pct(median, our.price_cents, commission, shipping)
                      if median else None)
            row.margin_pct = None if margin is None or not math.isfinite(margin) else round(margin, 2)
            row.color = (semaforo.color(margin, our.price_cents, median, params.green_min, params.yellow_min)
                         if median else semaforo.SIN_DATO)
            row.status = COMPARED
        s.add(row)
        s.commit()
        return row.status


# ─── 3. Para la pestaña ────────────────────────────────────────────


def list_rows(limit: int = 200, only_uploaded: bool = False) -> list[dict[str, Any]]:
    with Session(engine) as s:
        stmt = select(LuisIntake)
        if only_uploaded:
            stmt = stmt.where(LuisIntake.vendure_product_id.is_not(None))  # type: ignore[union-attr]
        rows = s.exec(stmt.order_by(LuisIntake.id.desc()).limit(limit)).all()  # type: ignore[union-attr]
    return [_out(r) for r in rows]


def _iso(moment: datetime | None) -> str | None:
    return moment.replace(microsecond=0).isoformat() + "Z" if moment else None


def _out(r: LuisIntake) -> dict[str, Any]:
    try:
        matches = json.loads(r.ml_matches or "[]")
    except ValueError:
        matches = []
    return {
        "id": r.id, "offer_id": r.offer_id, "source_url": r.source_url, "name": r.vendure_name or r.name,
        "image_url": r.vendure_image_url or r.image_url, "received_at": _iso(r.created_at),
        "lens_status": r.lens_status, "ml_matches": matches if isinstance(matches, list) else [],
        "ml_median_cents": r.ml_median_cents, "vendure_product_id": r.vendure_product_id,
        "uploaded_at": _iso(r.uploaded_at), "our_price_cents": r.our_price_cents, "margin_pct": r.margin_pct,
        "color": r.color, "status": r.status, "compared_at": _iso(r.compared_at),
    }
