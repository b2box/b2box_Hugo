"""Estimado de precio para lo que no tenemos: foto → 1688 → Vendure → rango.

Flujo (diseño 1.2, con las decisiones del 20-oct):

    t0  límites (visitante, global; fail-closed)     ── sin cupo: sin estimado
    1   la foto: link de ML o alibaba.com (fotos del link), foto subida, o
        link de 1688 leído por id en OTAPI (precio real, sin búsqueda por foto)
    2   apenas hay foto y CLIP no ve un match claro: Apify en paralelo con el
        resto del lookup (`start`)
    3   el lookup decide: lo tenemos (`skip_found`) o no (`finish`)
    4   CLIP de las miniaturas + 1 llamada a Qwen-VL → ≥ 2 candidatos buenos
    5   yuanes: mediana de los buenos × k (P25/P75 para los extremos)
    6   vecinos CLIP de nuestro catálogo → Vendure `estimateImportPrice`
    7   tabla por tramos: mín/máx por cantidad, ± m, redondeo

Deadline global (`estimate_deadline_s`, 50 s) contado desde que entra el
request: lo que no llegó, no entra. Cada paso tiene además su propio techo.

Al navegador va solo `public()` (pesos, cantidades, id del estimado). Los
candidatos, yuanes, offerIds y tiempos viven en `internal` (dashboard y
`lookup_estimate`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app import runtime, security
from app.config import get_settings
from app.dedup import catalog_index
from app.estimate import identity, images, judge, limits, measures, otapi_item, pricing, store
from app.estimate import vendure_estimate as vendure
from app.ingest import apify_1688

log = logging.getLogger(__name__)

OK = "ok"
SKIPPED_FOUND = "skipped_found"
NO_CANDIDATES = "no_candidates"
LOW_CONFIDENCE = "low_confidence"
OVER_BUDGET = "over_budget"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
ERROR = "error"
DISABLED = "disabled"

# Lo que queda reservado después de Apify para miniaturas, Qwen y Vendure.
TAIL_RESERVE_S = 30.0
_PHOTO_TIMEOUT_S = 5.0
_OTAPI_TIMEOUT_S = 4.0
_THUMBS_TIMEOUT_S = 4.0
# Medido el 30-oct por OpenRouter: 8 s no alcanzaba para la foto + 8 miniaturas.
_JUDGE_TIMEOUT_S = 20.0
_VENDURE_TIMEOUT_S = 5.0
# Menos que esto de ventana no alcanza para una corrida (la más rápida: 10 s).
_MIN_APIFY_WINDOW_S = 8.0
_SHORTLIST = judge.MAX_CANDIDATES
# Sin juez: coseno CLIP crudo mínimo entre la foto y la miniatura (a calibrar
# en el backtest; el mismo producto con otra foto da ~0,85-0,95).
CLIP_ONLY_FLOOR = 0.82
# El `similarityScore` del actor es una DISTANCIA (más bajo = más parecido; su
# tabla dice "distance (lower = closer)"). Medido el 28-oct con un spinner: las
# ofertas idénticas a la foto dan 0,056-0,075, las de otro modelo 0,14-0,28.
# Sin juez ni CLIP, una oferta hasta acá cuenta como buena.
ACTOR_DISTANCE_GOOD = 0.10
_NEIGHBORS = 10
# Ofertas buenas en las que se buscan medidas de vendedor antes de la IA.
_MEASURE_OFFERS = 5
_MEASURES_AI_TIMEOUT_S = 12.0
# NCM de ropa y textiles para vestir (capítulos 61 y 62): categoría excluida.
_EXCLUDED_NCM_PREFIXES = ("61", "62")
_APIFY_STATUS_MAP = {
    "no_candidates": NO_CANDIDATES, "over_budget": OVER_BUDGET, "busy": OVER_BUDGET,
    "disabled": DISABLED, "timeout": TIMEOUT, "error": ERROR,
}

# Tareas que registran un estimado después de responder (referencia fuerte).
_background: set[asyncio.Task] = set()


@dataclass(slots=True)
class EstimateResult:
    status: str
    estimate_id: str | None = None
    currency: str | None = None
    tiers: list[pricing.Tier] = field(default_factory=list)
    valid_until: datetime | None = None
    message: str = ""
    internal: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK and bool(self.tiers)

    def public(self) -> dict[str, Any] | None:
        """Lo único que puede ver el navegador: pesos con IVA y cantidades."""
        if not self.ok:
            return None
        return {
            "currency": self.currency,
            "vat_included": True,
            "tiers": [{"quantity": t.quantity, "low_cents": t.low_cents,
                       "high_cents": t.high_cents} for t in self.tiers],
            "valid_until": self.valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")
            if self.valid_until else None,
            "label": "estimado",
            "estimate_id": self.estimate_id,
        }


def enabled() -> bool:
    return int(runtime.get("estimate_enabled") or 0) == 1


def tier_quantities() -> list[int]:
    qs = {int(runtime.get(f"estimate_tier_qty_{i}") or 0) for i in (1, 2, 3)}
    return sorted(q for q in qs if q > 0)


def trusted_ip_clients() -> set[str]:
    """A quién se le cree `visitor.ip`. "legacy" nunca: HUGO_API_KEY es la key
    compartida (la misma regla que los clientes admin de /api/debug-config)."""
    raw = get_settings().estimate_trusted_ip_clients or ""
    return {c.strip() for c in raw.split(",") if c.strip()} - {security.LEGACY_CLIENT}


def visitor_ip_hash(*, api_client: str | None, declared_ip: object,
                    socket_ip: str | None) -> str | None:
    """Hash de la IP real del visitante, o None si no se puede estimar.

    Cliente de confianza (el storefront): solo con una `visitor.ip` de internet;
    sin ella NO hay estimado (None → rate_limited): el bucket compartido del
    socket es para buscar en el catálogo, no para gastar Apify. Cualquier otro
    cliente: la IP del socket (rotar `visitor.ip` no le da cupo). Lanza
    SaltMissing."""
    if api_client and api_client in trusted_ip_clients() and security.has_own_key(api_client):
        return identity.ip_hash(declared_ip)
    return identity.ip_hash(socket_ip, kind="sock")


class EstimateSession:
    """Un estimado, de punta a punta. Lo crea `open_session`."""

    def __init__(self, *, source: str, started_at: float, input_kind: str,
                 ip_hash: str | None = None, preset: str | None = None,
                 reason: str | None = None, taken: Sequence[str] = ()) -> None:
        s = get_settings()
        self.source = source
        self.input_kind = input_kind
        self.ip_hash = ip_hash
        self.started_at = started_at
        self.deadline_at = started_at + float(s.estimate_deadline_s)
        self.preset = preset
        self.reason = reason
        self.taken = tuple(taken)
        self.marketplace = "other"
        self.title = ""
        self.image_url: str | None = None
        self.source_url: str | None = None
        self.timings: dict[str, int] = {}
        self.offer: otapi_item.Offer1688 | None = None
        self.otapi_calls = 0
        self.photo: images.QueryPhoto | None = None
        self.search: apify_1688.Search1688Result | None = None
        self.search_error: str | None = None
        self.apify_launched = False
        self._task: asyncio.Task | None = None
        self._done = False
        # skip_found / abandon: ya no hay que lanzar Apify (lo mira _prepare
        # justo antes de lanzar, sin await de por medio: no hay carrera).
        self._stop = False

    # ── tiempo ────────────────────────────────────────────────────

    def remaining(self) -> float:
        return self.deadline_at - time.monotonic()

    def _mark(self, step: str, since: float) -> None:
        self.timings[step] = int((time.monotonic() - since) * 1000)

    @property
    def blocked(self) -> bool:
        return self.preset is not None

    @property
    def done(self) -> bool:
        return self._done

    @property
    def searching(self) -> bool:
        """Hay una búsqueda (foto o Apify) corriendo en segundo plano."""
        return self._task is not None and not self._task.done()

    @property
    def spent(self) -> bool:
        """¿Se pagó algo? (Apify lanzado o una lectura de OTAPI). Si no, el
        cupo del visitante se devuelve cuando no hubo estimado."""
        return self.apify_launched or self.otapi_calls > 0

    # ── paso 1: link de 1688 ──────────────────────────────────────

    async def read_1688_link(self, url: str) -> otapi_item.Offer1688 | None:
        """Lee la oferta por id. Las fotos que trae sirven para el catálogo y
        para los vecinos; el precio es la escalera real (sin búsqueda por foto)."""
        if self.blocked:
            return None
        offer_id = otapi_item.offer_id_from_url(url)
        if not offer_id:
            return None
        t = time.monotonic()
        window = max(0.5, min(_OTAPI_TIMEOUT_S, self.remaining()))
        try:
            # El timeout de httpx es por operación: una respuesta que gotea (o
            # un DNS colgado) lo pasa. Este es el corte duro.
            read = await asyncio.wait_for(otapi_item.read_offer(offer_id, timeout_s=window),
                                          timeout=window + 0.5)
        except asyncio.TimeoutError:
            # El cupo ya se reservó y RapidAPI cobra el intento: cuenta como llamada.
            read = otapi_item.OfferRead("timeout", calls=1)
        self._mark("otapi", t)
        self.otapi_calls += read.calls
        self.input_kind = "link_1688"
        if read.offer is None:
            self.search_error = f"otapi_{read.status}"
            return None
        self.offer = read.offer
        return read.offer

    # ── paso 2: lanzar la búsqueda ────────────────────────────────

    def start(self, image_urls: Sequence[str], *, title: str = "", marketplace: str = "",
              source_url: str | None = None) -> None:
        """Arranca (una sola vez) la foto + Apify en segundo plano."""
        if self.blocked or self._task is not None:
            return
        self.title = title or self.title
        self.marketplace = marketplace or self.marketplace
        self.source_url = source_url or self.source_url
        urls = list(image_urls)
        if self.offer is not None:
            urls = urls + [u for u in self.offer.pictures if u not in urls]
        self.image_url = urls[0] if urls else None
        self._task = asyncio.create_task(self._prepare(urls))
        self._task.add_done_callback(_log_task_error)

    async def _prepare(self, image_urls: list[str]) -> None:
        target = images.pick_search_image(image_urls)
        if not target:
            self.search_error = "foto_no_permitida"
            return
        t = time.monotonic()
        self.photo = await images.load_query_photo(
            target, timeout_s=min(_PHOTO_TIMEOUT_S, max(0.1, self.remaining())))
        self._mark("photo", t)
        if self.offer is not None:
            return            # link de 1688: el precio ya está, no se busca
        window = min(float(get_settings().estimate_apify_deadline_s),
                     self.remaining() - TAIL_RESERVE_S)
        if window < _MIN_APIFY_WINDOW_S:
            self.search = apify_1688.Search1688Result(status="timeout")
            return
        if self._stop:
            return            # el lookup ya resolvió sin estimado: no se paga
        t = time.monotonic()
        self.apify_launched = True
        self.search = await apify_1688.search_by_image(
            target, trusted_bytes_sha256=self.photo.sha256, deadline_s=window)
        self._mark("apify", t)

    # ── paso 3a: lo tenemos ───────────────────────────────────────

    async def skip_found(self) -> EstimateResult:
        """El lookup encontró el producto: no hay estimado. Si Apify ya estaba
        corriendo, su registro (con el gasto) se completa en segundo plano."""
        result = EstimateResult(SKIPPED_FOUND, internal={"reason": "el producto está en el catálogo"})
        if self._done:
            return result
        self._done = True
        self._stop = True
        if self.blocked:
            result.internal["reason"] += f" (además: {self.preset}, {self.reason})"
            await self._persist(result)
            return result
        if self._task is None or self._task.done():
            if not self.spent:
                await limits.refund_all_async(self.taken)
            await self._persist(result, ended_found=self.apify_launched)
            return result
        task = asyncio.create_task(self._persist_after_search(result))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return result

    async def _settle_task(self) -> bool:
        """Espera a que la búsqueda en segundo plano termine para saber si se
        gastó. Con `_stop` puesto ya no lanza nada nuevo, y lo que lanzó tiene
        deadline propio. True = terminó; False = no terminó a tiempo (se asume
        que gastó: nunca se devuelve cupo a ciegas)."""
        if self._task is None or self._task.done():
            return True
        limit = max(self.remaining(), 0.0) + apify_1688.DEFAULT_DEADLINE_S + 10.0
        done, _ = await asyncio.wait({self._task}, timeout=limit)
        return bool(done)

    async def _refund_if_unspent(self) -> None:
        settled = await self._settle_task()
        if settled and not self.blocked and not self.spent:
            await limits.refund_all_async(self.taken)

    async def _persist_after_search(self, result: EstimateResult) -> None:
        await self._refund_if_unspent()
        await self._persist(result, ended_found=self.apify_launched)

    async def abandon(self, reason: str) -> EstimateResult:
        """El lookup terminó antes de poder estimar (sin foto, índice armándose).
        Se devuelve el cupo del visitante si no se gastó nada."""
        if self._done:
            return EstimateResult(ERROR, internal={"reason": reason})
        self._done = True
        self._stop = True
        status = self.preset or ERROR
        result = EstimateResult(status, internal={"reason": self.reason or reason})
        # La búsqueda puede estar a mitad de camino (bajando la foto): se espera
        # a que termine antes de decidir el reembolso. Con _stop ya no lanza.
        await self._refund_if_unspent()
        await self._persist(result)
        return result

    # ── paso 3b: no lo tenemos ────────────────────────────────────

    async def finish(self, *, title: str = "") -> EstimateResult:
        """Corre lo que falta y devuelve el estimado. Nunca lanza."""
        self.title = title or self.title
        if self._done:
            return EstimateResult(ERROR, internal={"reason": "estimado ya resuelto"})
        self._done = True
        if self.blocked:
            result = EstimateResult(self.preset or ERROR, internal={"reason": self.reason})
            await self._persist(result)
            return result
        try:
            result = await self._finish()
        except Exception as exc:  # noqa: BLE001
            log.exception("estimado: error inesperado")
            result = EstimateResult(ERROR, internal={"reason": f"error {type(exc).__name__}"})
        await self._persist(result)
        return result

    async def _finish(self) -> EstimateResult:
        if self._task is None:
            return EstimateResult(ERROR, internal={"reason": "sin foto"})
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=max(self.remaining(), 0.0))
        except asyncio.TimeoutError:
            return self._fail(TIMEOUT, "deadline global antes de terminar la búsqueda",
                              deadline="global")
        if self.offer is not None:
            return await self._from_offer()
        if self.search is None:
            return self._fail(ERROR, self.search_error or "sin búsqueda")
        if self.search.status != "ok":
            status = _APIFY_STATUS_MAP.get(self.search.status, ERROR)
            return self._fail(status, f"apify {self.search.status}",
                              deadline="apify" if status == TIMEOUT else None)
        return await self._from_candidates(self.search.candidates)

    # ── link de 1688 ──────────────────────────────────────────────

    async def _from_offer(self) -> EstimateResult:
        offer = self.offer
        assert offer is not None
        quantities = tier_quantities()
        scenarios = pricing.scenarios_from_ladder(offer.price_ladder(), quantities)
        detail = {"candidates": [{
            "offer_id": offer.offer_id, "price_cny": offer.price_cny, "ladder": offer.ladder,
            "moq": offer.moq, "used": True, "source": "otapi",
        }], "k": None}
        unit = offer.unit or offer.attr_unit
        if unit is not None:
            detail["measures"] = unit.as_dict()
        else:
            unit = await self._unit_from_ai(offer.pictures, offer.title, offer.attributes_text,
                                            detail) or offer.box
        return await self._price(scenarios, quantities, judged=True, detail=detail,
                                 price_from_ladder=True, unit=unit)

    # ── candidatos de Apify ───────────────────────────────────────

    async def _from_candidates(self, candidates: list[apify_1688.Candidate1688]) -> EstimateResult:
        photo = self.photo
        t = time.monotonic()
        thumbs = await images.embed_thumbnails(
            [c.image_url for c in candidates],
            timeout_s=min(_THUMBS_TIMEOUT_S, self.remaining() - 9.0))
        self._mark("thumbs", t)
        query_vec = photo.vector if photo else None
        ranked = []
        for c in candidates:
            clip = images.cosine(query_vec, thumbs.get(c.image_url))
            # Distancia del actor → parecido: los idénticos primero en la lista corta.
            sim = max(0.0, 1.0 - c.similarity) if c.similarity is not None else 0.0
            combined = 0.5 * (clip if clip is not None else 0.0) + 0.5 * sim
            ranked.append((combined, clip, c))
        ranked.sort(key=lambda r: r[0], reverse=True)
        shortlist = ranked[:_SHORTLIST]

        verdicts: judge.JudgeOutcome | None = None
        if photo is not None and judge.available() and self.remaining() > 5.0:
            t = time.monotonic()
            try:
                verdicts = await asyncio.wait_for(
                    judge.judge(photo.url, [(c.image_url, c.title) for _, _, c in shortlist],
                                max_calls=int(runtime.get("estimate_daily_budget") or 0)),
                    timeout=min(_JUDGE_TIMEOUT_S, self.remaining() - 4.0))
            except asyncio.TimeoutError:
                log.info("estimado: el juez no contestó a tiempo, sigo solo con CLIP")
                self.timings["judge_timeout"] = 1
            self._mark("judge", t)
        judged = verdicts is not None and verdicts.answered

        log_rows = []
        good = []
        for i, (combined, clip, c) in enumerate(shortlist):
            verdict = verdicts.verdicts.get(i) if judged and verdicts else None
            # Con juez, una oferta sin veredicto válido (índice que el modelo no
            # devolvió o devolvió mal) no cuenta.
            if judged:
                ok = verdict is not None and verdict.good
            elif clip is not None:
                ok = clip >= CLIP_ONLY_FLOOR
            else:
                ok = c.similarity is not None and c.similarity <= ACTOR_DISTANCE_GOOD
            if ok:
                good.append(c)
            log_rows.append({
                "offer_id": c.offer_id, "title": c.title[:120], "price_cny": c.price_cny,
                "similarity": c.similarity, "clip": None if clip is None else round(clip, 4),
                "score": round(combined, 4),
                "score_visual": verdict.score_visual if verdict else None,
                "lote": verdict.lote if verdict else None,
                "accesorio": verdict.accesorio if verdict else None,
                "image_url": c.image_url, "detail_url": c.detail_url, "good": ok,
            })
        k = float(runtime.get("estimate_price_factor_k") or get_settings().estimate_price_factor_k)
        detail: dict[str, Any] = {
            "candidates": log_rows, "k": k,
            "judge": {"used": judged, "model": verdicts.model if verdicts else None,
                      "cost_usd": verdicts.cost_usd if verdicts else 0.0,
                      "excluded": bool(verdicts and verdicts.excluded)},
        }
        if judged and verdicts and verdicts.excluded:
            return self._fail(LOW_CONFIDENCE, "categoría excluida (ropa, marca o réplica)", detail)
        if len(good) < 2:
            return self._fail(LOW_CONFIDENCE if shortlist else NO_CANDIDATES,
                              f"{len(good)} candidatos buenos (hacen falta 2)", detail)
        prices = pricing.remove_bait([c.price_cny for c in good])
        kept = set(prices)
        for row in log_rows:
            row["used"] = bool(row["good"] and row["price_cny"] in kept)
        if len(prices) < 2:
            return self._fail(LOW_CONFIDENCE, "quedan menos de 2 precios sin carnada", detail)
        spread = pricing.dispersion(prices)
        detail["dispersion"] = round(spread, 3)
        if spread > pricing.MAX_PRICE_DISPERSION:
            return self._fail(LOW_CONFIDENCE, f"dispersión {spread:.2f} > 2,5", detail)
        detail["median_cny"] = statistics.median(prices)
        scenarios = pricing.scenarios_from_prices(prices, k)
        used = [c for c in good if c.price_cny in kept]
        unit = await self._unit_from_offers(used, detail)
        return await self._price(scenarios, tier_quantities(), judged=judged, detail=detail,
                                 unit=unit)

    # ── medidas de la unidad ──────────────────────────────────────

    async def _unit_from_offers(self, offers: list[apify_1688.Candidate1688],
                                detail: dict[str, Any]) -> measures.UnitMeasures | None:
        """Como M1 de Paco, sin el navegador: las fichas de las ofertas más parecidas
        (OTAPI, en paralelo) y, si ninguna trae medidas, la IA. Nunca las cajas de un
        parecido del catálogo."""
        picked = offers[:_MEASURE_OFFERS]
        window = min(_OTAPI_TIMEOUT_S, self.remaining() - 6.0)
        reads: list[otapi_item.OfferRead] = []
        if picked and window > 0.5:
            t = time.monotonic()
            reads = list(await asyncio.gather(
                *(otapi_item.read_offer(c.offer_id, timeout_s=window) for c in picked)))
            self._mark("otapi_measures", t)
            self.otapi_calls += sum(r.calls for r in reads)
        found = [r.offer for r in reads if r.offer]
        tries = [{"offer_id": c.offer_id, "status": r.status,
                  "unit": r.offer.unit.as_dict() if r.offer and r.offer.unit else None,
                  "attr_unit": r.offer.attr_unit.as_dict() if r.offer and r.offer.attr_unit else None,
                  "box": r.offer.box.as_dict() if r.offer and r.offer.box else None}
                 for c, r in zip(picked, reads)]
        for field_name in ("unit", "attr_unit"):
            unit = next((getattr(o, field_name) for o in found if getattr(o, field_name)), None)
            if unit:
                detail["measures"] = {"tries": tries, **unit.as_dict()}
                return unit
        best = found[0] if found else None
        best_candidate = offers[0] if offers else None
        unit = await self._unit_from_ai(
            best.pictures if best else ([best_candidate.image_url] if best_candidate else []),
            best.title if best else (best_candidate.title if best_candidate else ""),
            best.attributes_text if best else "", detail)
        if unit is None:
            unit = next((o.box for o in found if o.box), None)
            if unit:
                detail["measures"] = unit.as_dict()
        detail.setdefault("measures", {})["tries"] = tries
        return unit

    async def _unit_from_ai(self, pictures: list[str], title: str, attributes: str,
                            detail: dict[str, Any]) -> measures.UnitMeasures | None:
        # La foto del cliente primero (es la que el modelo ve bien); después, las de la oferta.
        photo_url = self.photo.url if self.photo else (pictures[0] if pictures else "")
        window = min(_MEASURES_AI_TIMEOUT_S, self.remaining() - 4.0)
        if window <= 0.5 or not photo_url:
            return None
        t = time.monotonic()
        unit, cost = None, 0.0
        try:
            if measures.gemini_available():
                unit, cost = await asyncio.wait_for(
                    measures.estimate_with_gemini(photo_url, pictures, title, attributes),
                    timeout=window)
            if unit is None:
                more, extra = await asyncio.wait_for(measures.estimate_with_ai(photo_url, title),
                                                     timeout=max(0.5, window - (time.monotonic() - t)))
                unit, cost = more, cost + extra
        except asyncio.TimeoutError:
            pass
        self._mark("ai_measures", t)
        detail["measures"] = {**(unit.as_dict() if unit else {"source": None}),
                              "ai_cost_usd": cost}
        return unit

    # ── Vendure y rango ───────────────────────────────────────────

    def _neighbors(self) -> list[str]:
        vec = self.photo.vector if self.photo else None
        if vec is None or not catalog_index.is_ready():
            return []
        return [p.id for p, _score, _img in catalog_index.search(vec, top_k=_NEIGHBORS)]

    async def _price(self, scenarios: pricing.Scenarios, quantities: list[int], *, judged: bool,
                     detail: dict[str, Any], price_from_ladder: bool = False,
                     unit: measures.UnitMeasures | None = None) -> EstimateResult:
        detail["scenarios_cny"] = scenarios.as_dict()
        if unit is None:
            if self.remaining() < _MEASURES_AI_TIMEOUT_S:
                return self._fail(TIMEOUT, "sin tiempo para las medidas de la unidad", detail,
                                  deadline="measures")
            return self._fail(LOW_CONFIDENCE, "sin medidas de la unidad (ni del vendedor ni de la IA)",
                              detail)
        refs = self._neighbors()
        detail["reference_product_ids"] = refs
        if not quantities:
            return self._fail(ERROR, "sin cantidades de tramos", detail)
        window = min(_VENDURE_TIMEOUT_S, self.remaining())
        if window <= 0.2:
            return self._fail(TIMEOUT, "sin tiempo para Vendure", detail, deadline="global")
        t = time.monotonic()
        try:
            est = await asyncio.wait_for(vendure.estimate(quantities, scenarios, refs, unit),
                                         timeout=window)
        except asyncio.TimeoutError:
            self._mark("vendure", t)
            return self._fail(TIMEOUT, "Vendure no contestó a tiempo", detail, deadline="vendure")
        except Exception as exc:  # noqa: BLE001
            self._mark("vendure", t)
            # El mensaje de GraphQL dice el motivo (permiso, canal, validación).
            log.warning("estimado: Vendure falló (%s: %s)", type(exc).__name__, str(exc)[:400])
            return self._fail(ERROR, f"Vendure {type(exc).__name__}", detail)
        self._mark("vendure", t)
        detail["vendure"] = {
            "status": est.status, "currency": est.currency, "box_source": est.box_source,
            "box_sample_size": est.box_sample_size, "reference_product_count":
            est.reference_product_count, "tax_classification_code": est.tax_classification_code,
            "tax_classification_share": est.tax_classification_share,
            "is_imo_cargo": est.is_imo_cargo, "sales_vat_pct": est.sales_vat_pct,
            "profile_version": est.profile_version, "computed_at": est.computed_at,
            "scenarios": [{"label": sc.label, "purchase_price_cny": sc.purchase_price_cny,
                           "box_quantile": sc.box_quantile, "cbm_per_unit": sc.cbm_per_unit,
                           "kg_per_unit": sc.kg_per_unit,
                           "prices": {str(q): {"net": p[0], "invoice": p[1]}
                                      for q, p in sorted(sc.prices.items())}}
                          for sc in est.scenarios],
        }
        if est.status == vendure.STATUS_NO_BOX:
            return self._fail(LOW_CONFIDENCE, "Vendure: sin datos de caja", detail)
        if est.status != vendure.STATUS_OK:
            return self._fail(ERROR, f"Vendure: {est.status}", detail)
        code = est.tax_classification_code or ""
        if code.replace(".", "").startswith(_EXCLUDED_NCM_PREFIXES):
            return self._fail(LOW_CONFIDENCE, "categoría excluida (textil)", detail)

        # Un solo margen, el del panel (± 20 % por defecto, Nico 28-oct): sin recargos aparte.
        raw_margin = runtime.get("estimate_margin_pct")
        margin = float(get_settings().estimate_margin_pct if raw_margin is None else raw_margin)
        detail["margin_pct"] = margin
        max_ratio = float(get_settings().estimate_max_range_ratio)
        tiers: list[pricing.Tier] = []
        for q in quantities:
            rng = pricing.tier_range(est.invoice_by_quantity(q), margin, max_ratio)
            if rng is None:
                return self._fail(LOW_CONFIDENCE, f"rango demasiado ancho o vacío para {q} u.",
                                  detail)
            tiers.append(pricing.Tier(q, rng[0], rng[1]))
        valid_hours = int(get_settings().estimate_valid_hours)
        return EstimateResult(
            OK, estimate_id="e_" + secrets.token_urlsafe(12), currency=est.currency, tiers=tiers,
            valid_until=datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(hours=valid_hours),
            message=pricing.estimate_message(tiers), internal=detail,
        )

    # ── cierre ────────────────────────────────────────────────────

    def _fail(self, status: str, reason: str, detail: dict[str, Any] | None = None, *,
              deadline: str | None = None) -> EstimateResult:
        internal = dict(detail or {})
        internal["reason"] = reason
        if deadline:
            internal["deadline_hit"] = deadline
        log.info("estimado: %s (%s)", status, reason)
        return EstimateResult(status, internal=internal)

    def cost_usd(self, result: EstimateResult) -> float:
        apify_usd = self.search.usd_imputado if self.search else 0.0
        judge_usd = float((result.internal.get("judge") or {}).get("cost_usd") or 0.0)
        return round(apify_usd + judge_usd + self.otapi_calls * otapi_item.USD_PER_CALL, 6)

    def _finalize_internal(self, result: EstimateResult) -> None:
        self.timings["total"] = int((time.monotonic() - self.started_at) * 1000)
        search = self.search
        result.internal.update({
            "status": result.status,
            "timings_ms": dict(self.timings),
            "apify": None if search is None else {
                "status": search.status, "run_ids": list(search.run_ids),
                "results": len(search.candidates), "usd": search.usd_imputado,
                "hedged": search.hedged, "cached": search.cached, "shared": search.shared,
                "elapsed_s": search.elapsed_s,
            },
            "apify_launched": self.apify_launched,
            "image_sha256": self.photo.sha256 if self.photo else None,
            "cost_usd": self.cost_usd(result),
        })

    async def _persist(self, result: EstimateResult, *, ended_found: bool = False) -> None:
        self._finalize_internal(result)
        info = result.internal
        vend = info.get("vendure") or {}
        row = {
            "estimate_id": result.estimate_id, "source": self.source,
            "input_kind": self.input_kind, "marketplace": self.marketplace,
            "image_sha256": info.get("image_sha256"), "ip_hash": self.ip_hash,
            "estimate_status": result.status, "reason": info.get("reason"),
            "deadline_hit": info.get("deadline_hit"), "total_ms": self.timings.get("total", 0),
            "timings": info.get("timings_ms"), "apify_launched": self.apify_launched,
            "apify_status": self.search.status if self.search else None,
            "apify_run_ids": self.search.run_ids if self.search else None,
            "apify_results": len(self.search.candidates) if self.search else 0,
            "apify_usd": self.search.usd_imputado if self.search else 0.0,
            "ended_found": ended_found, "candidates": info.get("candidates"),
            "qwen_used": bool((info.get("judge") or {}).get("used")),
            "price_factor_k": info.get("k"), "margin_pct": info.get("margin_pct"),
            "box_source": vend.get("box_source"), "box_sample_size": vend.get("box_sample_size"),
            "tax_classification_code": vend.get("tax_classification_code"),
            "tax_classification_share": vend.get("tax_classification_share"),
            "profile_version": vend.get("profile_version"),
            "vendure_scenarios": {"sent": info.get("scenarios_cny"),
                                  "received": vend.get("scenarios")} if vend else None,
            "tiers": [{"quantity": t.quantity, "low_cents": t.low_cents,
                       "high_cents": t.high_cents} for t in result.tiers] or None,
            "currency": result.currency, "cost_usd": info.get("cost_usd", 0.0),
        }
        audit = None
        if result.ok:
            audit = {
                "detail": f"Estimado para '{(self.title or self.source_url or 'foto')[:80]}': "
                          + result.message,
                "product_name": self.title, "image_url": self.image_url,
                "source_url": self.source_url,
            }
        await asyncio.to_thread(store.save, row, audit)


def abandon_later(session: EstimateSession, reason: str) -> None:
    """`abandon` en segundo plano (desde un except, incluso con cancelación)."""
    if session.done:
        return
    task = asyncio.create_task(session.abandon(reason))
    _background.add(task)
    task.add_done_callback(_background.discard)


def _log_task_error(task: asyncio.Task) -> None:
    """Si nadie esperó la búsqueda (deadline), su error igual queda en el log."""
    if not task.cancelled() and task.exception() is not None:
        log.warning("estimado: la búsqueda terminó con %s", type(task.exception()).__name__)


async def open_session(*, source: str, started_at: float | None = None, input_kind: str = "link",
                       api_client: str | None = None, declared_ip: object = None,
                       socket_ip: str | None = None, count_visitor: bool = True) -> EstimateSession:
    """Arma la sesión y reserva el cupo (t0). Nunca lanza: si no corresponde
    estimar, la sesión nace bloqueada con el estado que va a devolver.

    `count_visitor=False` (endpoint de prueba del dashboard): cuenta solo para
    el cupo global, no para el de un visitante."""
    started_at = time.monotonic() if started_at is None else started_at
    t = time.monotonic()

    def blocked(status: str, reason: str, ip_hash: str | None = None) -> EstimateSession:
        session = EstimateSession(source=source, started_at=started_at, input_kind=input_kind,
                                  ip_hash=ip_hash, preset=status, reason=reason)
        session._mark("limits", t)
        return session

    if not enabled():
        return blocked(DISABLED, "estimate_enabled = 0")
    try:
        ip_hash = (visitor_ip_hash(api_client=api_client, declared_ip=declared_ip,
                                   socket_ip=socket_ip) if count_visitor else None)
    except identity.SaltMissing:
        log.error("estimado: falta ESTIMATE_HASH_SALT en producción, no corre")
        return blocked(DISABLED, "sin ESTIMATE_HASH_SALT")
    if count_visitor and ip_hash is None:
        return blocked(RATE_LIMITED, "sin IP de visitante")
    s = get_settings()
    reservation = await limits.reserve_estimate_async(
        ip_hash,
        global_limit=int(runtime.get("estimate_daily_budget") or 0),
        visitor_limit=int(s.estimate_visitor_daily_limit),
        per_minute=int(s.estimate_visitor_per_minute),
    )
    if not reservation.granted:
        status = OVER_BUDGET if reservation.hit == limits.LIMIT_GLOBAL else RATE_LIMITED
        return blocked(status, f"límite {reservation.hit}", ip_hash)
    session = EstimateSession(source=source, started_at=started_at, input_kind=input_kind,
                              ip_hash=ip_hash, taken=reservation.taken)
    session._mark("limits", t)
    return session


async def wait_background() -> None:
    """Espera los registros pendientes (tests y apagado ordenado)."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
