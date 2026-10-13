"""Bandeja de revisión de Pao: qué rojos entran, con qué diagnóstico y cómo se cierran solos.

Cada noche, DESPUÉS de cerrar la corrida del semáforo, `process_run(run_id)`:

    0. si la corrida ya tiene `review_at` no hace nada (idempotente, también si el proceso se reinició a mitad);
    1. marca cada snapshot rojo como `firme` o `dudoso:<motivo>` (`review_flag`);
    2. con `pm_mode >= 1`: abre la ficha (estado `propuesto`) de cada rojo firme que lleva `pm_review_confirm_nights`
       noches con dato seguidas, con su diagnóstico a-e; actualiza las fichas abiertas y cierra solas (`cerrado_auto`)
       las que dejaron de ser rojo;
    3. guarda el resumen en la corrida y escribe `review_at`.

**En esta etapa Hugo NO escribe en Vendure** (ni `enabled` ni nada) y NO manda nada a Paco PRO: las fichas son propuestas
que Pao ve en el dashboard. `pm_mode = 2` (automático) todavía no existe: se comporta como 1 y lo loguea. Lo único que
se le pide a Vendure son lecturas (ver `diagnosis_source.ReadSource`), con tope duro de 150 consultas por noche.

Rojo firme (sección 3.3 del diseño), TODAS tienen que cumplirse:

    R1  ML con dato (`ml_status = ok`), color real rojo (nunca el estimado), producto habilitado
    R2  el producto no tiene ya una ficha abierta
    R3  recalculado SOLO con los idénticos confirmados por foto + nombre o por una persona (`clip`, `clip+nombre`,
        `manual`) sigue siendo rojo: un idéntico que solo confirmó el juez IA, o que quedó sin confirmar, no cuenta
    R4  el rojo se sostiene solo con ML, sin los precios de las tiendas
    R5  el dato tiene menos de `pm_review_data_max_age_h` horas (la Mac de la oficina puede traer datos de días)
    R6  al menos `pm_review_min_listings` publicaciones firmes con precio y `pm_review_min_sellers` vendedores distintos
    R7  nuestro precio / mediana de ML <= `pm_review_max_price_ratio` (más es casi siempre un match equivocado)
    R8  rojo firme en `pm_review_confirm_nights` corridas CON DATO seguidas (una noche `failed` o `skipped` no suma ni resta)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.db.models import MarketPriceSnapshot, PriceMonitorRun, PriceReviewEvent, PriceReviewItem
from app.db.session import engine
from app.dedup import catalog_index
from app.pricing import daily_budget
from app.pricing import diagnosis_source as ds
from app.pricing import price_monitor, semaforo
from app.pricing.safe_error import error_code
from app.pricing.price_diagnosis import DiagThresholds
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

ACTOR_AUTO = "hugo:auto"

# Estados de una ficha.
PROPUESTO = "propuesto"
EN_REVISION = "en_revision"
CORRIGIENDO = "corrigiendo"
BUSCANDO_PROVEEDOR = "buscando_proveedor"
MANTENIDO = "mantenido"
DESPUBLICADO = "despublicado"
CORREGIDO = "corregido"
CERRADO_AUTO = "cerrado_auto"
OPEN_STATES = (PROPUESTO, EN_REVISION, CORRIGIENDO, BUSCANDO_PROVEEDOR)
CLOSED_STATES = (DESPUBLICADO, CORREGIDO, CERRADO_AUTO)

# Veredicto de un snapshot.
NO_ROJO = "no_rojo"
FIRME = "firme"
DUDOSO = "dudoso"

# R3: de dónde puede venir un idéntico para sostener un rojo que lleve a una ficha.
FIRM_SOURCES = frozenset({"clip", "clip+nombre", "manual"})

# Cuántas corridas hacia atrás se miran para la racha, y cuántas veces se reintenta un diagnóstico sin desglose.
STREAK_LOOKBACK_RUNS = 30
DIAG_MAX_ATTEMPTS = 5
# Tiempo máximo que la pasada le dedica a leer Vendure (el resto de la pasada es solo base de datos).
PASS_DEADLINE_S = 900.0
JSON_LIMIT = 8_000
EVENT_JSON_LIMIT = 2_000

# Por qué un rojo es «dudoso». El texto lleva los números configurados.
REASONS = {
    "solo_tiendas": "El rojo sale solo de las tiendas: Mercado Libre no tiene idénticos con precio",
    "deshabilitado": "El producto ya está deshabilitado",
    "tiendas": "Sin los precios de las tiendas ya no es rojo",
    "recalculo": "Recalculado con los precios de Mercado Libre ya no es rojo",
    "fuentes": "Los idénticos los confirmó solo el juez IA, o quedaron sin confirmar",
    "dato_viejo": "El dato de los idénticos es viejo",
    "pocas_publicaciones": "Pocas publicaciones idénticas firmes",
    "pocos_vendedores": "Pocos vendedores distintos",
    "precio_extremo": "Nuestro precio es muchas veces el de Mercado Libre (casi seguro el match está mal)",
}


def mode_label(mode: int) -> str:
    return {0: "sombra", 1: "propuesta", 2: "automático (todavía como propuesta)"}.get(int(mode), str(mode))


# ─── Parámetros ───────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ReviewParams:
    green_min: float = 30.0
    yellow_min: float = 10.0
    data_max_age_h: float = 48.0
    min_listings: int = 3
    min_sellers: int = 2
    max_price_ratio: float = 3.0
    confirm_nights: int = 2

    @classmethod
    def from_runtime(cls) -> "ReviewParams":
        g = runtime.get
        return cls(
            green_min=float(g("pm_green_min_pct")), yellow_min=float(g("pm_yellow_min_pct")),
            data_max_age_h=float(g("pm_review_data_max_age_h")), min_listings=int(g("pm_review_min_listings")),
            min_sellers=int(g("pm_review_min_sellers")), max_price_ratio=float(g("pm_review_max_price_ratio")),
            confirm_nights=int(g("pm_review_confirm_nights")),
        )

    def reason_text(self, reason: str) -> str:
        base = REASONS.get(reason, reason)
        extra = {
            "dato_viejo": f" (más de {self.data_max_age_h:g} h)",
            "pocas_publicaciones": f" (menos de {self.min_listings})",
            "pocos_vendedores": f" (menos de {self.min_sellers})",
            "precio_extremo": f" (más de {self.max_price_ratio:g} veces)",
        }.get(reason, "")
        return base + extra


def diag_thresholds() -> DiagThresholds:
    g = runtime.get
    return DiagThresholds(
        cert_share_pct=float(g("pm_diag_cert_share_pct")), logistics_ratio=float(g("pm_diag_logistics_ratio")),
        box_z=float(g("pm_diag_box_z")), min_neighbors=int(g("pm_diag_min_neighbors")),
    )


# ─── Rojo firme: la puerta de entrada (funciones puras sobre un snapshot) ─


@dataclass(slots=True)
class FirmVerdict:
    status: str                       # no_rojo | dudoso | firme
    reason: str | None = None         # el primer motivo por el que es dudoso
    firm_prices: list[int] = field(default_factory=list)
    firm_sellers: int = 0
    firm_median: int | None = None
    price_ratio: float | None = None  # nuestro precio / mediana de los idénticos firmes
    reff_ratio: float | None = None   # nuestro precio / lo que le queda neto al revendedor
    data_at: datetime | None = None
    age_h: float | None = None

    @property
    def firm(self) -> bool:
        return self.status == FIRME

    @property
    def flag(self) -> str | None:
        if self.status == FIRME:
            return "firme"
        if self.status == DUDOSO:
            return f"dudoso:{self.reason}"
        return None

    @property
    def firm_listings(self) -> int:
        return len(self.firm_prices)


def _color_with(prices: list[int], snap: MarketPriceSnapshot, p: ReviewParams) -> tuple[str, float | None]:
    median = semaforo.median_cents(prices)
    margin = semaforo.estimated_margin_pct(median, snap.our_price_cents, snap.commission_pct or 0.0,
                                           snap.shipping_cents or 0)
    return semaforo.color(margin, snap.our_price_cents, median, p.green_min, p.yellow_min), margin


def _data_time(snap: MarketPriceSnapshot) -> datetime | None:
    """De cuándo es el dato. Una fila sin `data_at` (anterior a la bandeja) vale la hora de la corrida, salvo
    que venga de la oficina: de esa no se sabe la fecha y no se puede afirmar que sea fresca."""
    if snap.data_at is not None:
        return snap.data_at
    if snap.match_origin == "oficina" or snap.web_via == "oficina":
        return None
    return snap.captured_at


def _seller_key(seller: Any) -> str:
    """Un vendedor sin distinguir mayúsculas, espacios ni tildes de más: «TiendaX», «tiendax» y « TIENDAX » son el mismo, y
    cuentan UNA vez para los 2 vendedores que pide R6."""
    text = unicodedata.normalize("NFKC", str(seller or ""))
    return " ".join(text.split()).casefold()


def evaluate_firm(snap: MarketPriceSnapshot, p: ReviewParams, now: datetime | None = None) -> FirmVerdict:
    """R1 y R3-R7 sobre un snapshot. (R2 y R8 dependen de otras fichas y de otras noches: las mira `process_run`.)

    No rojo → `no_rojo`. Rojo al que le falla algo → `dudoso` con el primer motivo en el orden del diseño.
    Todo cumplido → `firme`."""
    now = now or utcnow()
    v = FirmVerdict(NO_ROJO)
    if snap.color != semaforo.ROJO:
        return v                                      # el color ESTIMADO nunca cuenta: solo `color`

    def doubt(reason: str) -> FirmVerdict:
        v.status, v.reason = DUDOSO, reason
        return v

    if snap.ml_status != price_monitor.OK:
        return doubt("solo_tiendas")                  # rojo sin dato de ML: sale de las tiendas
    if snap.product_enabled is False:
        return doubt("deshabilitado")
    if not snap.our_price_cents or snap.our_price_cents <= 0:
        return doubt("recalculo")

    entries = price_monitor._json_list(snap.matched_listings)
    all_prices = [x for m in entries for x in price_monitor._entry_prices(m)]
    # R4: solo con ML (todas las fuentes de ML) tiene que seguir en rojo
    if _color_with(all_prices, snap, p)[0] != semaforo.ROJO:
        return doubt("tiendas" if (snap.price_basis or "ml") != "ml" else "recalculo")
    # R3: solo con los idénticos confirmados por foto + nombre o por una persona
    firm_entries = [m for m in entries if m.get("source") in FIRM_SOURCES]
    v.firm_prices = [x for m in firm_entries for x in price_monitor._entry_prices(m)]
    v.firm_sellers = len({name for m in firm_entries for s in (m.get("sellers") or []) if (name := _seller_key(s))})
    if not v.firm_prices or _color_with(v.firm_prices, snap, p)[0] != semaforo.ROJO:
        return doubt("fuentes")
    v.firm_median = semaforo.median_cents(v.firm_prices)
    # R5: dato fresco
    v.data_at = _data_time(snap)
    if v.data_at is None:
        return doubt("dato_viejo")
    v.age_h = (now - v.data_at).total_seconds() / 3600.0
    if v.age_h >= p.data_max_age_h:
        return doubt("dato_viejo")
    # R6: evidencia mínima
    if v.firm_listings < p.min_listings:
        return doubt("pocas_publicaciones")
    if v.firm_sellers < p.min_sellers:
        return doubt("pocos_vendedores")
    # R7: precio creíble
    if v.firm_median and v.firm_median > 0:
        v.price_ratio = snap.our_price_cents / v.firm_median
        resale = v.firm_median * (1 - (snap.commission_pct or 0.0) / 100.0) - (snap.shipping_cents or 0)
        v.reff_ratio = snap.our_price_cents / resale if resale > 0 else None
        if v.price_ratio > p.max_price_ratio:
            return doubt("precio_extremo")
    v.status = FIRME
    return v


# ─── JSON acotado ─────────────────────────────────────────────────


def _cap_json(obj: Any, limit: int = JSON_LIMIT) -> str:
    """JSON compacto que cabe en `limit` caracteres: si no entra, se le sacan las evidencias de las reglas antes que
    cualquier otra cosa, y como último recurso se guarda solo la causa."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    if isinstance(obj, dict) and isinstance(obj.get("rules"), list):
        slim = dict(obj, rules=[{k: v for k, v in r.items() if k != "evidence"} for r in obj["rules"]])
        text = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= limit:
            return text
    return json.dumps({"truncated": True, "probable_cause": (obj or {}).get("probable_cause") if isinstance(obj, dict) else None},
                      ensure_ascii=False)


def _loads(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ─── Eventos y transiciones ───────────────────────────────────────


def add_event(session: Session, item: PriceReviewItem, kind: str, *, actor: str = ACTOR_AUTO,
              from_state: str | None = None, to_state: str | None = None, motive_code: str | None = None,
              motive_text: str | None = None, run_id: int | None = None, snapshot_id: int | None = None,
              payload: dict[str, Any] | None = None, now: datetime | None = None) -> PriceReviewEvent:
    """Agrega un evento al registro (no hace commit). Todo evento lleva quién lo hizo: un email o `hugo:auto`."""
    ev = PriceReviewEvent(
        item_id=int(item.id), product_id=item.product_id, at=now or utcnow(), actor=(actor or ACTOR_AUTO)[:120],
        kind=kind[:24], from_state=from_state, to_state=to_state,
        motive_code=(motive_code or None) and motive_code[:24], motive_text=(motive_text or None) and motive_text[:500],
        run_id=run_id, snapshot_id=snapshot_id,
        payload=_cap_json(payload, EVENT_JSON_LIMIT) if payload else None,
    )
    session.add(ev)
    return ev


def close_item(session: Session, item: PriceReviewItem, *, note: str, actor: str = ACTOR_AUTO,
               code: str = "ya_no_es_rojo", run_id: int | None = None, snapshot_id: int | None = None,
               now: datetime | None = None) -> bool:
    """Cierra una ficha abierta como `cerrado_auto` con un cambio de estado atómico
    (UPDATE … WHERE id AND state AND version). True si lo hizo ESTA llamada; False si otra ya la había movido.
    No hace commit. No toca Vendure."""
    now = now or utcnow()
    old_state, old_version = item.state, item.version
    result = session.execute(
        update(PriceReviewItem)
        .where(PriceReviewItem.id == item.id, PriceReviewItem.state == old_state,  # type: ignore[arg-type]
               PriceReviewItem.version == old_version)
        .values(state=CERRADO_AUTO, state_since=now, closed_at=now, open_key=None, version=old_version + 1)
    )
    if (result.rowcount or 0) != 1:
        return False
    for attr, value in (("state", CERRADO_AUTO), ("state_since", now), ("closed_at", now), ("open_key", None),
                        ("version", old_version + 1)):
        set_committed_value(item, attr, value)       # el UPDATE de arriba ya lo escribió: que no lo repita
    add_event(session, item, "cerrado_auto", actor=actor, from_state=old_state, to_state=CERRADO_AUTO,
              motive_code=code, motive_text=note, run_id=run_id, snapshot_id=snapshot_id, now=now)
    return True


def open_item_for(session: Session, product_id: str) -> PriceReviewItem | None:
    return session.exec(select(PriceReviewItem).where(PriceReviewItem.open_key == product_id)).first()


# ─── Racha (R8) ───────────────────────────────────────────────────


MIN_HOURS_BETWEEN_NIGHTS = 12


def _art_day(moment: datetime):
    """El día calendario argentino (UTC-3) de un datetime naive en UTC."""
    return moment.replace(tzinfo=timezone.utc).astimezone(daily_budget.ART).date()


def _same_night(a: datetime, b: datetime) -> bool:
    """¿Dos corridas son la misma noche? Mismo día ART, o menos de 12 h de diferencia (aunque crucen la medianoche)."""
    return _art_day(a) == _art_day(b) or abs((a - b).total_seconds()) < MIN_HOURS_BETWEEN_NIGHTS * 3600


def streaks(session: Session, run_id: int, product_ids: list[str]) -> dict[str, int]:
    """Noches seguidas con dato en rojo firme, contando la de `run_id` (que ya es firme). Mira las corridas
    anteriores de más nueva a más vieja: `failed` y `skipped` no suman ni restan; la primera noche que no fue
    rojo firme (verde, amarillo, sin dato, rojo dudoso) corta la cuenta.

    Se cuentan NOCHES, no corridas. Una noche es un día calendario de ARGENTINA (no de UTC: la medianoche UTC cae a las 21:00
    ART) y dos corridas a menos de `MIN_HOURS_BETWEEN_NIGHTS` horas una de otra son la misma noche aunque crucen la
    medianoche. De cada noche vale la última corrida con dato. Así dos disparos seguidos (el cron y un «Correr ahora», o dos
    manuales) no alcanzan para pasar la racha."""
    out = {pid: 1 for pid in product_ids}
    if not product_ids:
        return out
    today = session.exec(select(PriceMonitorRun.started_at).where(PriceMonitorRun.id == run_id)).first()
    low = max(1, run_id - STREAK_LOOKBACK_RUNS)
    for i in range(0, len(product_ids), 400):
        chunk = product_ids[i:i + 400]
        rows = session.exec(
            select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.ml_status, MarketPriceSnapshot.review_flag,
                   PriceMonitorRun.started_at)
            .join(PriceMonitorRun, PriceMonitorRun.id == MarketPriceSnapshot.run_id)  # type: ignore[arg-type]
            .where(MarketPriceSnapshot.product_id.in_(chunk),  # type: ignore[attr-defined]
                   MarketPriceSnapshot.run_id < run_id, MarketPriceSnapshot.run_id >= low)
            .order_by(MarketPriceSnapshot.product_id, MarketPriceSnapshot.run_id.desc())  # type: ignore[union-attr]
        ).all()
        stopped: set[str] = set()
        counted: dict[str, list[datetime]] = {}
        for pid, status, flag, started in rows:
            if pid in stopped:
                continue
            if status in (price_monitor.FAILED, price_monitor.SKIPPED):
                continue
            nights = counted.setdefault(pid, [today] if today else [])
            if started is not None and any(_same_night(started, other) for other in nights):
                continue                          # otra corrida de una noche que ya se contó (o la de hoy)
            if started is not None:
                nights.append(started)
            if flag == "firme":
                out[pid] += 1
            else:
                stopped.add(pid)
    return out


# ─── La pasada de la noche ────────────────────────────────────────


@dataclass(slots=True)
class _Candidate:
    snap: MarketPriceSnapshot
    verdict: FirmVerdict
    streak: int


@dataclass(slots=True)
class _Plan:
    run_id: int
    requested_mode: int
    mode: int
    params: ReviewParams
    now: datetime
    summary: dict[str, Any]
    n_flipped: int = 0
    to_create: list[_Candidate] = field(default_factory=list)
    pending_diag: list[int] = field(default_factory=list)       # ids de fichas abiertas sin desglose
    specs_by_product: dict[str, dict[str, float]] = field(default_factory=dict)
    done: bool = False


def _plan(run_id: int, now: datetime | None) -> _Plan | None:
    """Todo lo que se resuelve con la base: marcar rojos, actualizar y cerrar fichas, decidir qué fichas abrir.
    None si la corrida no existe o ya fue procesada."""
    now = now or utcnow()
    requested = int(runtime.get("pm_mode") or 0)
    mode = min(requested, 1)
    params = ReviewParams.from_runtime()
    with Session(engine, expire_on_commit=False) as s:
        run = s.get(PriceMonitorRun, run_id)
        if run is None or run.review_at is not None:
            return None
        plan = _Plan(run_id=run_id, requested_mode=requested, mode=mode, params=params, now=now,
                     summary={"mode": requested, "mode_effective": mode, "nuevas": 0, "diag_reintentados": 0,
                              "consultas_vendure": 0})
        if requested >= 2:
            log.warning("price_review #%s: pm_mode=2 (automático) todavía no está implementado: esta pasada se "
                        "comporta como pm_mode=1 (propuesta) y no toca Vendure", run_id)
            plan.summary["note"] = "pm_mode=2 todavía no implementado: se comportó como 1"

        plan.n_flipped = int(s.exec(
            select(func.count(MarketPriceSnapshot.id))  # type: ignore[arg-type]
            .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.prev_color.is_not(None),  # type: ignore[union-attr]
                   MarketPriceSnapshot.prev_color != MarketPriceSnapshot.color)
        ).one() or 0)

        reds = list(s.exec(select(MarketPriceSnapshot).where(
            MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.color == semaforo.ROJO)).all())
        verdicts: dict[str, FirmVerdict] = {}
        dudosos: dict[str, int] = {}
        for snap in reds:
            v = evaluate_firm(snap, params, now)
            verdicts[snap.product_id] = v
            snap.review_flag = v.flag
            s.add(snap)
            if v.status == DUDOSO and v.reason:
                dudosos[v.reason] = dudosos.get(v.reason, 0) + 1
        s.commit()
        firm = [sn for sn in reds if verdicts[sn.product_id].firm]
        plan.summary.update({"rojos": len(reds), "firmes": len(firm), "dudosos": dudosos})

        if mode == 0:
            plan.done = True
            return plan

        # Fichas abiertas: se actualizan o se cierran solas.
        by_pid = {sn.product_id: sn for sn in reds}
        open_items = list(s.exec(select(PriceReviewItem).where(PriceReviewItem.open_key.is_not(None))).all())  # type: ignore[union-attr]
        open_pids = {it.product_id for it in open_items}
        extra_pids = [it.product_id for it in open_items if it.product_id not in by_pid]
        snaps_open = dict(by_pid)
        if extra_pids:
            for sn in s.exec(select(MarketPriceSnapshot).where(
                    MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.product_id.in_(extra_pids))).all():  # type: ignore[attr-defined]
                snaps_open[sn.product_id] = sn
        streak_map = streaks(s, run_id, [pid for pid, v in verdicts.items() if v.firm])
        touched = closed = 0
        for item in open_items:
            sn = snaps_open.get(item.product_id)
            if sn is None:
                continue
            outcome = _refresh_open_item(s, item, sn, verdicts.get(sn.product_id), streak_map.get(sn.product_id, 0),
                                         params, run_id, now)
            touched += outcome == "updated"
            closed += outcome == "closed"
            if outcome != "closed" and item.diag_status == "pendiente":
                plan.pending_diag.append(int(item.id))   # type: ignore[arg-type]
        s.commit()

        # Fichas nuevas: rojo firme, sin ficha abierta, con la racha cumplida.
        waiting = 0
        for sn in firm:
            if sn.product_id in open_pids:
                continue
            n = streak_map.get(sn.product_id, 1)
            if n >= params.confirm_nights:
                plan.to_create.append(_Candidate(sn, verdicts[sn.product_id], n))
            else:
                waiting += 1
        plan.to_create.sort(key=lambda c: (-c.streak, -c.verdict.firm_listings, c.snap.est_margin_pct or 0.0, c.snap.product_id))
        plan.summary.update({"esperando_racha": waiting, "actualizadas": touched, "cerradas_solas": closed})
        if plan.to_create or plan.pending_diag:
            plan.specs_by_product = {
                pid: ds.parse_specs(raw) for pid, raw in s.exec(
                    select(MarketPriceSnapshot.product_id, MarketPriceSnapshot.our_specs)
                    .where(MarketPriceSnapshot.run_id == run_id, MarketPriceSnapshot.our_specs.is_not(None))  # type: ignore[union-attr]
                ).all()}
    return plan


def _flags_for(snap: MarketPriceSnapshot, verdict: FirmVerdict | None, params: ReviewParams,
               diag_status: str | None = None) -> list[str]:
    out: list[str] = []
    if snap.match_origin == "oficina" or snap.web_via == "oficina":
        out.append("oficina")
    if verdict is not None and verdict.status == DUDOSO and verdict.reason:
        out.append(f"dudoso:{verdict.reason}")
    if snap.ml_status != price_monitor.OK:
        out.append("sin_dato_hoy")
    if diag_status in ("pendiente", "parcial"):
        out.append("diag_pendiente" if diag_status == "pendiente" else "diag_parcial")
    return out


def _refresh_open_item(s: Session, item: PriceReviewItem, snap: MarketPriceSnapshot, verdict: FirmVerdict | None,
                       streak: int, params: ReviewParams, run_id: int, now: datetime) -> str:
    """Pone al día una ficha abierta con el snapshot de esta corrida, o la cierra sola si dejó de ser rojo.
    Devuelve «updated», «closed» o «unchanged»."""
    if item.state != PROPUESTO:                          # las demás etapas llegan después
        return "unchanged"
    item.last_run_id, item.last_snapshot_id = run_id, snap.id
    item.last_color, item.last_margin_pct = snap.color, snap.est_margin_pct
    if snap.product_enabled is False:
        close_item(s, item, note="El producto ya estaba inactivo por otra razón.", code="ya_inactivo",
                   run_id=run_id, snapshot_id=snap.id, now=now)
        return "closed"
    if snap.ml_status == price_monitor.OK and snap.color != semaforo.ROJO:
        close_item(s, item, note=f"Dejó de ser rojo: hoy está en {snap.color}.", code="ya_no_es_rojo",
                   run_id=run_id, snapshot_id=snap.id, now=now)
        return "closed"
    if verdict is not None and verdict.firm:
        item.streak_nights = streak
    item.flags = json.dumps(_flags_for(snap, verdict, params, item.diag_status))
    s.add(item)
    return "updated"


def _variant_of(catalog: Mapping[str, VendureProduct] | None, snap: MarketPriceSnapshot):
    product = (catalog or {}).get(snap.product_id)
    for v in (product.priced_variants if product else None) or []:
        if v.id == snap.variant_id:
            return v
    return None


@dataclass(slots=True)
class _Diag:
    """Lo que dio diagnosticar un producto. `blocked`: no se pudo pedir (se acabó el tope de requests del día o el tiempo de la
    pasada); eso NO cuenta como intento: la ficha se reintenta mañana con todos sus intentos."""
    pdiag: ds.ProductDiagnosis | None
    blocked: bool = False


class _Gate:
    """El tope de requests del día y el tiempo de la pasada, mirados antes de pedirle algo a Vendure."""

    def __init__(self, source: ds.BudgetedSource, deadline_at: float) -> None:
        self.source = source
        self.deadline_at = deadline_at
        self.deadline_hit = False

    def remaining(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    @property
    def closed(self) -> bool:
        if self.remaining() <= 0:
            self.deadline_hit = True
        return self.deadline_hit or self.source.refused


async def _diagnose(snap: MarketPriceSnapshot, gate: _Gate, channel, plan: _Plan,
                    catalog: Mapping[str, VendureProduct] | None, median: int | None) -> _Diag:
    if gate.closed:
        return _Diag(None, blocked=True)
    neighbors = ds.neighbor_boxes(snap.product_id, plan.specs_by_product, catalog_index.neighbors_of)
    try:
        pdiag = await asyncio.wait_for(ds.diagnose_product(
            snap, gate.source, yellow_min_pct=plan.params.yellow_min, channel=channel,
            variant=_variant_of(catalog, snap), neighbor_boxes_=neighbors, thresholds=diag_thresholds(),
            ml_median_cents=median), timeout=gate.remaining())
    except (asyncio.TimeoutError, TimeoutError):
        gate.deadline_hit = True
        log.warning("price_review: se acabó el tiempo de la pasada diagnosticando %s", snap.product_id)
        return _Diag(None, blocked=True)
    except Exception:  # noqa: BLE001  (un producto no puede tirar la pasada)
        log.exception("price_review: el diagnóstico de %s reventó", snap.product_id)
        return _Diag(None)
    return _Diag(pdiag, blocked=bool(pdiag and pdiag.blocked))


def _diag_fields(pdiag: ds.ProductDiagnosis | None, attempts: int) -> dict[str, Any]:
    """Las columnas de diagnóstico de la ficha a partir del resultado. «Completo» = se pudo LEER el desglose (aunque no traiga
    dólares: un abastecimiento local, o un tramo sin precio que la regla (b) ya explica)."""
    pending = "parcial" if attempts >= DIAG_MAX_ATTEMPTS else "pendiente"
    if pdiag is None:
        return {"diag_status": pending, "diag": _cap_json(
            {"attempts": attempts, "errors": ["el diagnóstico no pudo armarse"]}), "breakdown": None,
            "probable_cause": None, "fixable": None}
    d = pdiag.diagnosis.to_dict()
    d["errors"], d["attempts"] = pdiag.errors, attempts
    d["tiers"] = pdiag.tiers_breakdown
    return {
        "diag_status": "completo" if pdiag.breakdown_read else pending, "diag": _cap_json(d),
        "breakdown": _cap_json({"tier": pdiag.breakdown, "tiers": pdiag.tiers_breakdown}) if pdiag.breakdown else None,
        "probable_cause": pdiag.diagnosis.probable_cause, "fixable": pdiag.diagnosis.fixable,
    }


def _create_item(plan: _Plan, cand: _Candidate, diag: _Diag) -> bool:
    """Abre la ficha y su evento `propuesto` en UNA transacción. False si ya había una abierta (otro
    proceso, o esta misma pasada antes de un reinicio): no se duplica nada."""
    snap, v = cand.snap, cand.verdict
    fields = _diag_fields(diag.pdiag, attempts=0 if diag.blocked else 1)
    item = PriceReviewItem(
        product_id=snap.product_id, product_name=snap.product_name, product_code=snap.product_code,
        product_slug=snap.product_slug, image_url=snap.product_image_url, open_key=snap.product_id,
        state=PROPUESTO, state_since=plan.now, opened_at=plan.now, mode_at_open=plan.mode,
        opened_run_id=plan.run_id, opened_snapshot_id=snap.id, last_run_id=plan.run_id, last_snapshot_id=snap.id,
        last_color=snap.color, last_margin_pct=snap.est_margin_pct, streak_nights=cand.streak,
        our_price_cents=snap.our_price_cents, ml_median_cents=v.firm_median,
        firm_listing_count=v.firm_listings, firm_seller_count=v.firm_sellers,
        price_ratio=None if v.price_ratio is None else round(v.price_ratio, 3),
        reff_ratio=None if v.reff_ratio is None else round(v.reff_ratio, 3),
        flags=json.dumps(_flags_for(snap, v, plan.params, fields["diag_status"])), **fields,
    )
    with Session(engine, expire_on_commit=False) as s:
        s.add(item)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return False
        add_event(s, item, "propuesto", to_state=PROPUESTO, run_id=plan.run_id, snapshot_id=snap.id, now=plan.now,
                  payload={"racha": cand.streak, "causa": fields["probable_cause"], "margen_pct": snap.est_margin_pct,
                           "modo": plan.requested_mode, "modo_efectivo": plan.mode})
        s.commit()
    return True


def _retry_diag(plan: _Plan, item_id: int, diag: _Diag, verdict: FirmVerdict | None) -> None:
    """Completa el diagnóstico de una ficha abierta. Si no se pudo pedir nada (tope del día, tiempo de la pasada) la ficha queda
    como estaba y NO gasta un intento. Si lo de hoy sigue siendo un rojo firme, las cifras de la tarjeta se refrescan con las
    mismas con las que se calculó el diagnóstico (una sola mediana, un solo precio)."""
    if diag.blocked and diag.pdiag is None:
        return
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is None or item.state not in OPEN_STATES:
            return
        attempts = int((_loads(item.diag) or {}).get("attempts", 1)) + (0 if diag.blocked else 1)
        for k, val in _diag_fields(diag.pdiag, attempts).items():
            setattr(item, k, val)
        if verdict is not None and verdict.firm:
            snap = s.get(MarketPriceSnapshot, item.last_snapshot_id) if item.last_snapshot_id else None
            if snap is not None:
                item.our_price_cents, item.ml_median_cents = snap.our_price_cents, verdict.firm_median
                item.firm_listing_count, item.firm_seller_count = verdict.firm_listings, verdict.firm_sellers
                item.price_ratio = None if verdict.price_ratio is None else round(verdict.price_ratio, 3)
                item.reff_ratio = None if verdict.reff_ratio is None else round(verdict.reff_ratio, 3)
        flags = [f for f in (_loads(item.flags) or []) if not str(f).startswith("diag_")]
        if item.diag_status != "completo":
            flags.append("diag_pendiente" if item.diag_status == "pendiente" else "diag_parcial")
        item.flags = json.dumps(flags)
        s.add(item)
        s.commit()


def _load_pending(item_id: int) -> tuple[MarketPriceSnapshot | None, int]:
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is None or item.last_snapshot_id is None:
            return None, 0
        return s.get(MarketPriceSnapshot, item.last_snapshot_id), item_id


def _finish(plan: _Plan) -> bool:
    """Guarda el resumen y escribe `review_at` UNA sola vez (UPDATE … WHERE review_at IS NULL)."""
    with Session(engine) as s:
        result = s.execute(
            update(PriceMonitorRun)
            .where(PriceMonitorRun.id == plan.run_id, PriceMonitorRun.review_at.is_(None))  # type: ignore[arg-type,union-attr]
            .values(review_at=utcnow(), review_summary=json.dumps(plan.summary, ensure_ascii=False),
                    n_flipped=plan.n_flipped)
        )
        s.commit()
    return (result.rowcount or 0) == 1


async def process_run(run_id: int, *, catalog: Mapping[str, VendureProduct] | None = None,
                      source: ds.ReadSource | None = None, now: datetime | None = None,
                      max_calls: int = ds.MAX_VENDURE_CALLS, deadline_s: float = PASS_DEADLINE_S) -> dict[str, Any] | None:
    """La pasada de la bandeja sobre una corrida del semáforo. Devuelve el resumen, o None si la corrida no
    existe o ya fue procesada (`review_at`). NO escribe en Vendure.

    `catalog`: los productos que acaba de leer la corrida (para no volver a pedir los tramos). `source`: de dónde leer
    Vendure (por defecto el cliente real). Las lecturas tienen DOS topes: `max_calls` REQUESTS HTTP reales por día (ART), contados
    en la base (sobreviven a reinicios y a una segunda pasada del mismo día) y `deadline_s` segundos para leer. Lo que no entra
    queda «diagnóstico pendiente» y no gasta un intento: se completa en las próximas noches."""
    t0 = time.monotonic()
    plan = await asyncio.to_thread(_plan, run_id, now)
    if plan is None:
        return None
    if not plan.done and (plan.to_create or plan.pending_diag):
        budgeted = ds.BudgetedSource(source or VendureClient(), max_calls,           # type: ignore[arg-type]
                                     day=daily_budget.today_art(plan.now))
        gate = _Gate(budgeted, time.monotonic() + deadline_s)
        if await asyncio.to_thread(budgeted.budget.used) >= budgeted.max_calls:      # el día ya gastó todo (otra pasada)
            budgeted.refused = True
        channel = None
        if not gate.closed:
            try:
                channel = await asyncio.wait_for(budgeted.get_channel_pricing(), timeout=gate.remaining())
            except ds.ReadBudgetExhausted:
                pass                                       # ya quedó anotado en `budgeted.refused`
            except (asyncio.TimeoutError, TimeoutError):
                gate.deadline_hit = True
            except Exception as exc:  # noqa: BLE001
                plan.summary.setdefault("errores", []).append(f"canal: {error_code(exc)}")
                log.warning("price_review: no se pudieron leer los datos del canal: %s: %s", type(exc).__name__, str(exc)[:200])
        retried = 0
        for item_id in plan.pending_diag:           # primero las que quedaron sin desglose (las más viejas)
            if gate.closed:
                break
            snap, _ = await asyncio.to_thread(_load_pending, item_id)
            if snap is None:
                continue
            verdict = evaluate_firm(snap, plan.params, plan.now)
            median = verdict.firm_median if verdict.firm else None
            diag = await _diagnose(snap, gate, channel, plan, catalog, median)
            await asyncio.to_thread(_retry_diag, plan, item_id, diag, verdict)
            retried += 1
        created = 0
        for cand in plan.to_create:
            if int(runtime.get("pm_mode") or 0) == 0:        # interruptor de emergencia: bajar el modo corta la pasada
                plan.summary["cortada_por_modo"] = True
                log.warning("price_review #%s: pm_mode bajó a 0 durante la pasada; no se abren más fichas", run_id)
                break
            diag = await _diagnose(cand.snap, gate, channel, plan, catalog, cand.verdict.firm_median)
            created += bool(await asyncio.to_thread(_create_item, plan, cand, diag))
        used_today = await asyncio.to_thread(budgeted.budget.used)
        plan.summary.update({"nuevas": created, "diag_reintentados": retried, "consultas_vendure": budgeted.calls,
                             "consultas_hoy": used_today, "tope_consultas": budgeted.max_calls})
        if budgeted.refused or used_today >= budgeted.max_calls:
            plan.summary["tope_alcanzado"] = True
        if gate.deadline_hit:
            plan.summary["tiempo_agotado"] = True
    plan.summary["duracion_s"] = round(time.monotonic() - t0, 2)
    wrote = await asyncio.to_thread(_finish, plan)
    log.info("price_review #%s (modo %s): %s%s", run_id, mode_label(plan.requested_mode), plan.summary,
             "" if wrote else " (otro proceso ya había cerrado la pasada)")
    return plan.summary


# ─── "No es el mismo": la ficha se cierra sola en la misma respuesta ─


def after_correction(session: Session, snap: MarketPriceSnapshot, actor: str | None) -> dict[str, Any] | None:
    """Gancho de «No es el mismo» / «Es el mismo» (api/price_monitor_routes._commit_correction). Corre dentro de la
    transacción de la corrección, sin commit:

      * vuelve a marcar el snapshot (`review_flag`) con los datos corregidos;
      * si el producto tiene una ficha abierta y ya no es rojo con dato, la cierra como `cerrado_auto`
        (el actor del evento es la persona que hizo la corrección).

    Devuelve `{"item_id", "state", "closed"}` si había ficha, o None. No toca Vendure."""
    p = ReviewParams.from_runtime()
    verdict = evaluate_firm(snap, p)
    snap.review_flag = verdict.flag
    item = open_item_for(session, snap.product_id)
    if item is None or item.state != PROPUESTO or item.last_run_id != snap.run_id:
        return None
    item.last_snapshot_id, item.last_color, item.last_margin_pct = snap.id, snap.color, snap.est_margin_pct
    closed = False
    if snap.color != semaforo.ROJO or snap.ml_status != price_monitor.OK:
        closed = close_item(
            session, item, actor=actor or "desconocido", code="no_es_el_mismo",
            note="Una persona corrigió las publicaciones de Mercado Libre y el producto dejó de ser rojo.",
            run_id=snap.run_id, snapshot_id=snap.id)
    else:
        item.flags = json.dumps(_flags_for(snap, verdict, p, item.diag_status))
        session.add(item)
    return {"item_id": item.id, "state": item.state, "closed": closed}
