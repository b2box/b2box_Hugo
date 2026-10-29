"""Decisiones sobre las fichas de la bandeja de revisión y la ÚNICA escritura de `enabled` en Vendure desde la bandeja.

Las expone api/price_review_routes.py y solo las puede usar una persona de PRICE_REVIEW_DECIDERS:

    inactivar    «Inactivar y revisar»: propuesto → en_revision (apaga el producto)
    corregir     → corrigiendo (apaga si estaba propuesto)
    mantener     → mantenido (reactiva si lo había apagado Hugo); guarda la línea base de la regla del 10 %
    despublicar  → despublicado (apaga si estaba propuesto)
    reactivar    «Ya lo corregí: reactivar»: corrigiendo → corregido (reactiva)
    deshacer     vuelve al estado y al `enabled` de antes de la última acción humana (hasta 30 días)

Qué se puede desde cada estado lo manda `TRANSITIONS` (tabla 3.1 de los criterios); la pantalla muestra los botones que
devuelve `available_actions`.

Escribir `enabled` (protocolo 3.7):

    1. en UNA transacción: cambio de estado atómico (UPDATE … WHERE id AND state AND version), el evento de la acción
       y un evento `intent` con lo que se va a escribir;
    2. se lee el `enabled` real (`get_enabled_status`; para reactivar, `get_review_guard`);
    3. se escribe con `set_enabled` (reactivar: UN solo intento);
    4. se verifica que Vendure devolvió el valor pedido;
    5. en otra transacción: `hugo_disabled` y sus fechas, el evento `inactivado` / `reactivado` y una fila de AuditLog;
    6. `vendure_catalog.invalidate()`.

Si Vendure falla o devuelve otra cosa, la ficha vuelve atómicamente al estado anterior con un evento `vendure_error`
(solo el código de `safe_error.error_code`, nunca el mensaje) y la respuesta es 502. Si el proceso muere a mitad,
`reconcile()` completa o descarta la intención contra el `enabled` real: lo llaman la pasada de la noche, un job al
arrancar y la próxima acción sobre la misma ficha.

Reglas fijas:
  * apagar lo que ya estaba apagado por otra razón: no se escribe y la ficha se cierra (`cerrado_auto`);
  * reactivar solo lo que apagó Hugo, con el producto todavía apagado, sin `approvalStatus` pending/rejected y sin un
    `duplicate_disabled` / `bx_no_image_disabled` posterior; si no, 409 «reactivar a mano»;
  * las escrituras usan un `VendureClient` propio: no gastan ni las frena el tope de lecturas de la pasada;
  * `pm_mode = 0` frena las acciones; Deshacer sigue permitido.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from gql.transport.exceptions import TransportQueryError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app import runtime
from app.clock import utcnow
from app.db.models import AuditLog, PriceReviewEvent, PriceReviewItem
from app.db.session import engine
from app.pricing import price_review as pr
from app.pricing.price_review_digest import clean
from app.pricing.safe_error import error_code
from app.vendure import catalog as vendure_catalog
from app.vendure.client import ReviewGuard, VendureClient

log = logging.getLogger(__name__)

INACTIVAR, CORREGIR, MANTENER, DESPUBLICAR, REACTIVAR, DESHACER = (
    "inactivar", "corregir", "mantener", "despublicar", "reactivar", "deshacer")
DECISIONS = (CORREGIR, MANTENER, DESPUBLICAR, REACTIVAR)

# (estado, acción) → (estado nuevo, qué se escribe en Vendure: False apaga, True reactiva, None nada).
TRANSITIONS: dict[tuple[str, str], tuple[str, bool | None]] = {
    (pr.PROPUESTO, INACTIVAR): (pr.EN_REVISION, False),
    (pr.PROPUESTO, CORREGIR): (pr.CORRIGIENDO, False),
    (pr.PROPUESTO, MANTENER): (pr.MANTENIDO, None),
    (pr.PROPUESTO, DESPUBLICAR): (pr.DESPUBLICADO, False),
    (pr.EN_REVISION, CORREGIR): (pr.CORRIGIENDO, None),
    (pr.EN_REVISION, MANTENER): (pr.MANTENIDO, True),
    (pr.EN_REVISION, DESPUBLICAR): (pr.DESPUBLICADO, None),
    (pr.CORRIGIENDO, MANTENER): (pr.MANTENIDO, True),
    (pr.CORRIGIENDO, DESPUBLICAR): (pr.DESPUBLICADO, None),
    (pr.CORRIGIENDO, REACTIVAR): (pr.CORREGIDO, True),
    (pr.BUSCANDO_PROV, CORREGIR): (pr.CORRIGIENDO, None),
    (pr.BUSCANDO_PROV, MANTENER): (pr.MANTENIDO, True),
    (pr.BUSCANDO_PROV, DESPUBLICAR): (pr.DESPUBLICADO, None),
}
ACTION_LABEL = {
    INACTIVAR: "Inactivar y revisar", CORREGIR: "Corregir", MANTENER: "Mantener activo", DESPUBLICAR: "Despublicar",
    REACTIVAR: "Ya lo corregí: reactivar", DESHACER: "Deshacer",
}
MOTIVES: dict[str, dict[str, str]] = {
    CORREGIR: {"caja": "La caja", "tramos": "Los tramos", "cantidad": "La cantidad", "certificado": "El certificado",
               "otro": "Otra cosa"},
    MANTENER: {"nicho": "Es de nicho", "volumen": "Se vende por volumen", "margen_aceptado": "El margen se acepta",
               "match_mal": "El match de Mercado Libre está mal", "otro": "Otro motivo"},
    DESPUBLICAR: {"sin_margen": "No deja margen", "sin_proveedor": "No hay proveedor", "calidad": "Calidad",
                  "duplicado": "Está duplicado", "otro": "Otro motivo"},
}
# El texto libre: «siempre» obligatorio, obligatorio solo con el motivo «otro», u opcional.
TEXT_RULE = {MANTENER: "siempre", CORREGIR: "otro", DESPUBLICAR: "otro"}
TEXT_MIN, TEXT_MAX = 10, 500

# Eventos que cambian el estado de la ficha. Deshacer solo vale si el último de estos es la acción de una persona.
HUMAN_KINDS = {"aprobado", *(f"decision_{d}" for d in DECISIONS)}
STATE_KINDS = HUMAN_KINDS | {"propuesto", "deshacer", "reabierto", "cerrado_auto", "vendure_error", "reactivar_a_mano",
                             "intent_descartado"}
# Lo que cierra un `intent`: mientras el último de estos sea el `intent`, la escritura está en curso o quedó a medias.
INTENT_END_KINDS = ("inactivado", "reactivado", "ya_activo", "vendure_error", "reactivar_a_mano", "intent_descartado",
                    "cerrado_auto")
INTENT_STALE = timedelta(minutes=2)      # una intención más vieja que esto quedó a medias: se concilia
UNDO_WINDOW = timedelta(days=30)
APPROVAL_BLOCKED = {"pending": "está pendiente de aprobación en el portal de proveedores",
                    "rejected": "está rechazado en el portal de proveedores"}
_DATE_KEYS = {"state_since", "decision_at", "closed_at"}

DECIDERS_ONLY = "Solo deciden: ver PRICE_REVIEW_DECIDERS"
SHADOW = "La bandeja está en modo sombra (pm_mode = 0): las decisiones están frenadas"
IN_PROGRESS = "Hay un cambio en Vendure en curso para esta ficha"


class ActionError(Exception):
    """Una acción que no se hizo: el código HTTP, un código corto para la pantalla y una frase fija."""

    def __init__(self, status: int, code: str, message: str, item: PriceReviewItem | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.extra = dict(extra)
        if item is not None:
            self.extra.update(state=item.state, state_label=pr.STATE_LABEL.get(item.state, item.state),
                              version=item.version)

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(slots=True)
class _Move:
    """Una transición: de dónde a dónde, qué se escribe en Vendure y cómo estaban las columnas que cambia."""

    action: str
    from_state: str
    to_state: str
    write: bool | None
    values: dict[str, Any]                                   # columnas que cambia (además de state / version)
    before: dict[str, Any] = field(default_factory=dict)     # cómo estaban, con `state_since`: para volver atrás


# ─── Validación ───────────────────────────────────────────────────


def validate(action: str, motive_code: str | None, motive_text: str | None) -> tuple[str | None, str | None]:
    """El motivo y el texto de una acción, saneados (sin caracteres de control ni espacios de más). 400 si no sirven."""
    # Mismo saneo que el mail, sin recortar: el largo se valida acá.
    text = clean(motive_text, limit=len(motive_text)) if motive_text else None
    if text and len(text) > TEXT_MAX:
        raise ActionError(400, "texto_largo", f"El texto puede tener hasta {TEXT_MAX} caracteres.")
    motives = MOTIVES.get(action)
    if motives is None:
        if motive_code:
            raise ActionError(400, "motivo_invalido", "Esta acción no lleva un motivo de la lista.")
        return None, text or None
    if motive_code not in motives:
        raise ActionError(400, "motivo_invalido", "Elegí un motivo de la lista.")
    if (TEXT_RULE.get(action) == "siempre" or motive_code == "otro") and len(text or "") < TEXT_MIN:
        raise ActionError(400, "falta_texto", f"Contá el motivo con al menos {TEXT_MIN} caracteres.")
    return motive_code, text or None


# ─── Lecturas de la base ──────────────────────────────────────────


def _mode() -> int:
    return int(runtime.get("pm_mode") or 0)


def pending_intent(s: Session, item_id: int) -> PriceReviewEvent | None:
    """El `intent` sin cerrar de la ficha, o None."""
    ev = s.exec(select(PriceReviewEvent).where(
        PriceReviewEvent.item_id == item_id, PriceReviewEvent.kind.in_(("intent", *INTENT_END_KINDS)))  # type: ignore[attr-defined]
        .order_by(PriceReviewEvent.id.desc()).limit(1)).first()  # type: ignore[union-attr]
    return ev if ev is not None and ev.kind == "intent" else None


def _load(item_id: int) -> tuple[PriceReviewItem, PriceReviewEvent | None]:
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(PriceReviewItem, item_id)
        if item is None:
            raise ActionError(404, "no_existe", "Ficha no encontrada.")
        return item, pending_intent(s, item_id)


def _events(item_id: int) -> list[PriceReviewEvent]:
    with Session(engine) as s:
        return list(s.exec(select(PriceReviewEvent).where(PriceReviewEvent.item_id == item_id)
                           .order_by(PriceReviewEvent.id)).all())  # type: ignore[arg-type]


def _payload(ev: PriceReviewEvent | None) -> dict[str, Any]:
    try:
        out = json.loads(ev.payload) if ev is not None and ev.payload else {}
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _dump(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in values.items()}


def _parse(values: dict[str, Any]) -> dict[str, Any]:
    return {k: datetime.fromisoformat(v) if k in _DATE_KEYS and isinstance(v, str) else v for k, v in values.items()}


def last_human_action(events: Iterable[PriceReviewEvent]) -> tuple[PriceReviewEvent, bool | None] | None:
    """La última acción humana si después no cambió el estado, con lo que habría que escribir en Vendure para volver
    atrás (lo contrario de lo que escribió, o None si no escribió). None si después vino otra cosa que cambia el estado
    (otra decisión, la reapertura del 10 %, un cierre solo, un error) o si la última fue un Deshacer."""
    evs = sorted(events, key=lambda e: e.id or 0)
    last = next((e for e in reversed(evs) if e.kind in STATE_KINDS), None)
    if last is None or last.kind not in HUMAN_KINDS or not _payload(last).get("antes"):
        return None
    wrote = [e for e in evs if (e.id or 0) > (last.id or 0) and e.kind in ("inactivado", "reactivado")]
    return last, (None if not wrote else not wrote[-1].enabled_after)


def undo_candidate(events: Iterable[PriceReviewEvent], now: datetime | None = None
                   ) -> tuple[PriceReviewEvent, bool | None] | None:
    """`last_human_action`, si fue hace 30 días o menos."""
    found = last_human_action(events)
    return found if found is not None and (now or utcnow()) - found[0].at <= UNDO_WINDOW else None


# ─── Qué botones ve la pantalla ───────────────────────────────────


def available_actions(item: PriceReviewItem, events: list[PriceReviewEvent], *, decider: bool, mode: int,
                      pending: bool, now: datetime | None = None) -> list[dict[str, Any]]:
    """Las acciones que permite el estado de la ficha, cada una habilitada o no con el motivo. Los botones de la
    pantalla salen de acá (y el backend vuelve a validar todo al recibir el POST)."""
    def reason(undo: bool = False) -> str | None:
        if not decider:
            return DECIDERS_ONLY
        if mode == 0 and not undo:
            return SHADOW
        return IN_PROGRESS if pending else None

    def entry(code: str, write: bool | None, why: str | None, **extra: Any) -> dict[str, Any]:
        return {"code": code, "label": ACTION_LABEL[code], "enabled": why is None, "reason": why,
                "vendure": {False: "apagar", True: "reactivar", None: None}[write],
                "motives": [{"code": c, "label": lbl} for c, lbl in MOTIVES.get(code, {}).items()],
                "text": TEXT_RULE.get(code, "opcional"), **extra}

    out = []
    for code in (INACTIVAR, *DECISIONS):
        rule = TRANSITIONS.get((item.state, code))
        if rule is not None:
            out.append(entry(code, rule[1], reason(), to_state=rule[0], to_state_label=pr.STATE_LABEL[rule[0]]))
    undo = undo_candidate(events, now)
    if undo is not None:
        ev, write = undo
        out.append(entry(DESHACER, write, reason(undo=True), to_state=ev.from_state,
                         to_state_label=pr.STATE_LABEL.get(ev.from_state or "", ev.from_state),
                         deadline=(ev.at + UNDO_WINDOW).isoformat() + "Z", undoes=ev.kind))
    return out


# ─── La acción ────────────────────────────────────────────────────


def _state_changed(item: PriceReviewItem) -> ActionError:
    return ActionError(409, "estado_cambio",
                       f"Otra persona ya decidió: ahora está en «{pr.STATE_LABEL.get(item.state, item.state)}».", item)


def _plan_move(item: PriceReviewItem, action: str, actor: str, code: str | None, text: str | None,
               now: datetime) -> _Move:
    rule = TRANSITIONS.get((item.state, action))
    if rule is None:
        allowed = [a for a in (INACTIVAR, *DECISIONS) if (item.state, a) in TRANSITIONS]
        raise ActionError(409, "no_permitida", f"Desde «{pr.STATE_LABEL.get(item.state, item.state)}» no se puede "
                          f"«{ACTION_LABEL[action]}».", item, allowed=allowed)
    to_state, write = rule
    values: dict[str, Any] = {}
    if action != INACTIVAR:
        values.update(decision=action, decision_by=actor, decision_at=now, decision_motive_code=code,
                      decision_motive_text=text)
    if action == MANTENER:
        values.update(kept_ratio=item.reff_ratio, kept_median_cents=item.ml_median_cents,
                      kept_our_price_cents=item.our_price_cents)
    if to_state in pr.CLOSED_STATES:
        values.update(open_key=None, closed_at=now)
    return _Move(action, item.state, to_state, write, values)


def _plan_undo(item: PriceReviewItem, now: datetime) -> _Move:
    """Deshacer = la transición inversa de la última acción humana: vuelve las columnas que cambió (guardadas en
    `antes` en su evento) y escribe en Vendure lo contrario de lo que escribió esa acción."""
    found = last_human_action(_events(int(item.id)))  # type: ignore[arg-type]
    if found is None:
        raise ActionError(409, "nada_para_deshacer", "No hay nada para deshacer: la ficha cambió después de la última "
                          "acción, o la última fue un Deshacer.", item)
    ev, write = found
    if now - ev.at > UNDO_WINDOW:
        raise ActionError(409, "plazo_vencido", "Venció el plazo para deshacer (30 días).", item)
    return _Move(DESHACER, item.state, str(ev.from_state), write, {"state_since": now, **_parse(_payload(ev)["antes"])})


async def act(item_id: int, action: str, actor: str, *, expected_state: str, motive_code: str | None = None,
              motive_text: str | None = None, client: VendureClient | None = None) -> dict[str, Any]:
    """Hace una acción sobre una ficha y devuelve `{item_id, state, version, wrote}`. Levanta `ActionError` (400, 404,
    409, 502) si no se hizo: la ficha queda como estaba."""
    if action not in ACTION_LABEL:
        raise ActionError(400, "accion_invalida", "Acción desconocida.")
    code, text = validate(action, motive_code, motive_text)
    item, pending = _load(item_id)
    if pending is not None and utcnow() - pending.at >= INTENT_STALE:
        await reconcile(item_ids=[item_id], client=client)          # quedó a medias: se concilia antes de seguir
        item, pending = _load(item_id)
    if pending is not None:
        raise ActionError(409, "en_curso", IN_PROGRESS + ": probá de nuevo en un par de minutos.", item)
    if item.state != expected_state:
        raise _state_changed(item)
    if action != DESHACER and _mode() == 0:
        raise ActionError(409, "modo_sombra", "La bandeja está en modo sombra: las decisiones están frenadas.", item)
    now = utcnow()
    if action == DESHACER:
        mv, code = _plan_undo(item, now), "revertido"
    else:
        mv = _plan_move(item, action, actor, code, text, now)
    mv.before = {"state_since": item.state_since, **{k: getattr(item, k) for k in mv.values if k != "state_since"}}
    _start(item, mv, actor, code, text, now)
    if mv.write is None:
        return _result(item, wrote=False)
    return await _apply(item, mv, actor, client or VendureClient())


def _start(item: PriceReviewItem, mv: _Move, actor: str, code: str | None, text: str | None, now: datetime) -> None:
    """Paso 1: el cambio de estado atómico, el evento de la acción y (si hay que escribir) el `intent`, en UNA
    transacción. 409 si otra pestaña o la pasada movió la ficha antes."""
    kind = "aprobado" if mv.action == INACTIVAR else "deshacer" if mv.action == DESHACER else f"decision_{mv.action}"
    payload: dict[str, Any] = {"antes": _dump(mv.before)} if mv.action != DESHACER else {}
    if mv.action == MANTENER and mv.values.get("kept_ratio") is None:
        payload["sin_linea_base"] = "sin línea base: no se reabre sola"
    with Session(engine, expire_on_commit=False) as s:
        s.add(item)
        try:
            moved = pr.move_item(s, item, mv.to_state, now=now, **mv.values)
            if moved:
                ev = pr.add_event(s, item, kind, actor=actor, from_state=mv.from_state, to_state=mv.to_state,
                                  motive_code=code, motive_text=text, payload=payload or None, now=now)
                if mv.write is not None:
                    s.flush()
                    pr.add_event(s, item, "intent", actor=actor, from_state=mv.from_state, to_state=mv.to_state,
                                 enabled_before=not mv.write, enabled_after=mv.write, now=now,
                                 payload={"accion": mv.action, "evento": ev.id, "antes": _dump(mv.before)})
                s.commit()
        except IntegrityError:
            s.rollback()
            raise ActionError(409, "otra_ficha_abierta", "El producto ya tiene otra ficha abierta.") from None
    if not moved:
        current, _ = _load(int(item.id))  # type: ignore[arg-type]
        raise _state_changed(current)


def _result(item: PriceReviewItem, *, wrote: bool, note: str | None = None) -> dict[str, Any]:
    return {"item_id": item.id, "state": item.state, "version": item.version, "wrote": wrote, "note": note}


# ─── Vendure ──────────────────────────────────────────────────────


async def _apply(item: PriceReviewItem, mv: _Move, actor: str, client: VendureClient) -> dict[str, Any]:
    """Pasos 2 a 6 del protocolo. La ficha ya está en `mv.to_state` con su `intent`."""
    target = bool(mv.write)
    guard: ReviewGuard | None = None
    try:
        if target:
            guard = await client.get_review_guard(item.product_id)
            enabled = None if guard is None else guard.enabled
        else:
            enabled = await client.get_enabled_status(item.product_id)
    except TransportQueryError as exc:
        if not target:
            _undo_move(item, mv, actor, "vendure_error", "lectura", error=error_code(exc))
            raise _vendure_failed(item) from None
        # Vendure contestó pero no deja leer el estado de aprobación: no se adivina, se reactiva a mano.
        log.warning("price_review: no se pudo leer el guardia de %s: %s", item.product_id, error_code(exc))
        raise _manual(item, mv, actor, "aprobacion_ilegible", "no se pudo leer el estado de aprobación en Vendure")
    except Exception as exc:  # noqa: BLE001
        log.warning("price_review: no se pudo leer %s en Vendure: %s", item.product_id, error_code(exc))
        _undo_move(item, mv, actor, "vendure_error", "lectura", error=error_code(exc))
        raise _vendure_failed(item) from None
    if enabled is None:
        _undo_move(item, mv, actor, "vendure_error", "no_existe", error="no_existe")
        raise _vendure_failed(item)
    if enabled is target:
        return _already(item, mv, actor, target)
    if target:
        blocker = _reactivation_blocker(item, guard)
        if blocker is not None:
            raise _manual(item, mv, actor, *blocker)
    try:
        got = await client.set_enabled(item.product_id, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("price_review: set_enabled(%s, %s) falló: %s", item.product_id, target, error_code(exc))
        _undo_move(item, mv, actor, "vendure_error", "escritura", error=error_code(exc))
        raise _vendure_failed(item) from None
    if got is not target:
        _undo_move(item, mv, actor, "vendure_error", "valor_no_coincide", error="valor_no_coincide")
        raise _vendure_failed(item)
    if not _confirm(item, mv, actor, target, guard):
        raise _state_changed(_load(int(item.id))[0])  # type: ignore[arg-type]
    vendure_catalog.invalidate()
    return _result(item, wrote=True)


def _vendure_failed(item: PriceReviewItem) -> ActionError:
    return ActionError(502, "vendure_error", "Vendure no aceptó el cambio: la ficha quedó como estaba. Probá de nuevo "
                       "en unos minutos.", item, error=item.vendure_error)


def _manual(item: PriceReviewItem, mv: _Move, actor: str, code: str, why: str) -> ActionError:
    _undo_move(item, mv, actor, "reactivar_a_mano", code, text=f"Hay que reactivarlo a mano: {why}.")
    return ActionError(409, "reactivar_a_mano", f"Hay que reactivarlo a mano en Vendure: {why}.", item, reason=code)


def _reactivation_blocker(item: PriceReviewItem, guard: ReviewGuard | None) -> tuple[str, str] | None:
    """V-4: Hugo reactiva solo lo que apagó él, si nadie más lo apagó después y el portal de proveedores no lo frena."""
    if item.hugo_disabled is not True:
        return "no_lo_apago_hugo", "no lo apagó Hugo"
    status = guard.approval_status if guard is not None else None
    if status in APPROVAL_BLOCKED:
        return f"aprobacion_{status}", APPROVAL_BLOCKED[status]
    with Session(engine) as s:
        stmt = select(AuditLog.action).where(
            AuditLog.product_id == item.product_id,
            AuditLog.action.in_(("duplicate_disabled", "bx_no_image_disabled")))  # type: ignore[attr-defined]
        if item.disabled_at is not None:
            stmt = stmt.where(AuditLog.created_at > item.disabled_at)
        later = s.exec(stmt.limit(1)).first()
    if later == "duplicate_disabled":
        return "duplicado", "después lo apagó el control de duplicados"
    if later == "bx_no_image_disabled":
        return "bx_sin_imagen", "después lo apagó el control de productos BX sin foto"
    return None


def _undo_move(item: PriceReviewItem, mv: _Move, actor: str, kind: str, code: str, *, error: str | None = None,
               text: str | None = None, now: datetime | None = None) -> bool:
    """Vuelve la ficha atómicamente a como estaba antes de la transición y deja el evento que cierra el `intent`.
    False si otra cosa ya la había resuelto (no se duplica nada)."""
    now = now or utcnow()
    extra = {"vendure_error": error} if kind == "vendure_error" else {}
    with Session(engine, expire_on_commit=False) as s:
        s.add(item)
        moved = pr.move_item(s, item, mv.from_state, now=now, **mv.before, **extra)
        if moved:
            pr.add_event(s, item, kind, actor=actor, from_state=mv.to_state, to_state=mv.from_state, motive_code=code,
                         motive_text=text, now=now, payload={"accion": mv.action, **({"error": error} if error else {})})
            s.commit()
    if not moved:
        log.warning("price_review: la ficha #%s ya se había movido; no se vuelve atrás (%s)", item.id, kind)
    return moved


def _already(item: PriceReviewItem, mv: _Move, actor: str, target: bool) -> dict[str, Any]:
    """El producto ya está como se quería: no se escribe. Apagar algo apagado por otra razón cierra la ficha (regla
    única de 3.1); reactivar algo que otra persona ya reactivó termina la acción sin error."""
    now = utcnow()
    with Session(engine, expire_on_commit=False) as s:
        s.add(item)
        if not target:
            done = pr.close_item(s, item, note="El producto ya estaba inactivo por otra razón: Hugo no lo tocó.",
                                 actor=actor, code="ya_inactivo", now=now, hugo_disabled=False)
        else:
            done = pr.move_item(s, item, item.state, now=now, state_since=item.state_since, hugo_disabled=False)
            if done:
                pr.add_event(s, item, "ya_activo", actor=actor, enabled_before=True, enabled_after=True, now=now,
                             motive_text="Ya estaba activo: otra persona lo reactivó; Hugo no escribió nada.")
        s.commit()
    if not done:
        raise _state_changed(_load(int(item.id))[0])  # type: ignore[arg-type]
    return _result(item, wrote=False, note="ya_inactivo" if not target else "ya_activo")


def _confirm(item: PriceReviewItem, mv: _Move, actor: str, target: bool, guard: ReviewGuard | None,
             *, reconciled: bool = False) -> bool:
    """Paso 5: deja anotado lo que se escribió (la ficha, el evento y la fila de AuditLog para la historia del producto).
    Atómico: si otro proceso ya lo anotó (la conciliación), no se repite."""
    now = utcnow()
    values = ({"hugo_disabled": False, "reactivated_at": now} if target
              else {"hugo_disabled": True, "disabled_at": now, "enabled_before": True})
    with Session(engine, expire_on_commit=False) as s:
        s.add(item)
        if not pr.move_item(s, item, item.state, now=now, state_since=item.state_since, vendure_error=None, **values):
            log.warning("price_review: la ficha #%s ya estaba resuelta; no se anota dos veces", item.id)
            return False
        payload: dict[str, Any] = {"accion": mv.action}
        if guard is not None:
            payload["con_contacto"] = guard.has_supplier_contact
        if reconciled:
            payload["conciliado"] = True
        pr.add_event(s, item, "reactivado" if target else "inactivado", actor=actor, from_state=mv.from_state,
                     to_state=mv.to_state, enabled_before=not target, enabled_after=target, now=now, payload=payload)
        s.add(AuditLog(
            action="price_review_enabled" if target else "price_review_disabled", source="price_review",
            product_id=item.product_id, created_at=now,
            detail=(f"Bandeja de precios, ficha #{item.id}: «{ACTION_LABEL.get(mv.action, mv.action)}» de {actor}. "
                    f"{'Reactivado' if target else 'Inactivado'} en Vendure."),
            before=json.dumps({"enabled": not target}), after=json.dumps({"enabled": target}),
            product_name=item.product_name, product_code=item.product_code, product_image_url=item.image_url,
        ))
        s.commit()
    return True


# ─── Conciliación (el proceso murió a mitad de una escritura) ─────


async def reconcile(*, item_ids: list[int] | None = None, client: VendureClient | None = None,
                    now: datetime | None = None) -> int:
    """Cada `intent` sin cerrar de más de 2 minutos se concilia contra el `enabled` real: si Vendure ya tiene el valor
    pedido se completa (paso 5); si no, la ficha vuelve a como estaba con un evento `intent_descartado`. Solo LEE Vendure.
    Si no se puede leer, la intención queda para la próxima. Devuelve cuántas resolvió."""
    cutoff = (now or utcnow()) - INTENT_STALE
    with Session(engine) as s:
        last = select(func.max(PriceReviewEvent.id)).where(
            PriceReviewEvent.kind.in_(("intent", *INTENT_END_KINDS))).group_by(PriceReviewEvent.item_id)  # type: ignore[attr-defined]
        stmt = select(PriceReviewEvent).where(PriceReviewEvent.id.in_(last), PriceReviewEvent.kind == "intent",  # type: ignore[union-attr]
                                              PriceReviewEvent.at <= cutoff)
        if item_ids is not None:
            stmt = stmt.where(PriceReviewEvent.item_id.in_(item_ids))  # type: ignore[attr-defined]
        intents = list(s.exec(stmt).all())
    if not intents:
        return 0
    client = client or VendureClient()
    done = 0
    for intent in intents:
        try:
            done += await _reconcile_one(intent, client)
        except Exception as exc:  # noqa: BLE001  (una ficha no frena a las demás)
            log.warning("price_review: no se pudo conciliar la ficha #%s: %s", intent.item_id, error_code(exc))
    return done


async def _reconcile_one(intent: PriceReviewEvent, client: VendureClient) -> bool:
    data = _payload(intent)
    target = bool(intent.enabled_after)
    enabled = await client.get_enabled_status(intent.product_id)
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(PriceReviewItem, intent.item_id)
        still = pending_intent(s, intent.item_id) if item is not None else None
    if item is None or still is None or still.id != intent.id:
        return False                                   # otro proceso ya la resolvió
    mv = _Move(str(data.get("accion") or ""), intent.from_state or item.state, intent.to_state or item.state, target,
               {}, _parse(data.get("antes") or {}))
    if enabled is target:
        return _confirm(item, mv, intent.actor, target, None, reconciled=True)
    return _undo_move(item, mv, intent.actor, "intent_descartado", "conciliacion",
                      text="El cambio en Vendure quedó a medias: la ficha volvió a como estaba.")
