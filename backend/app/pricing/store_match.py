"""Semáforo contra las tiendas: para cada producto nuestro, qué hay en Casa Perfecta,
Gadnic y las demás tiendas cargadas, clasificado IGUAL / SIMILAR / DIFERENTE.

Va DENTRO de la misma corrida del semáforo que Mercado Libre (`price_monitor.
evaluate_product` llama a `attach`), contra el catálogo local que dejó el indexador
(`store_catalog`): cero requests a las tiendas por producto.

Por producto y por tienda:

  1. prefiltro por nombre (fuzzy, sobre los títulos normalizados): los K=6 más
     parecidos. Siempre se trae algo, aunque el parecido sea bajo;
  2. CLIP con la foto de cada candidato contra las nuestras (embedding cacheado, la
     misma escala centrada que usa Mercado Libre);
  3. el mismo veredicto de tres valores: reglas por foto + nombre
     (`market_match.classify`), juez IA solo para la banda ambigua y para los que
     declaran una marca conocida (Nico: marca genérica = igual, marca conocida =
     similar; la marca propia de la tienda cuenta como genérica), y chequeo de
     medidas / cantidad / capacidad (`market_match.apply_specs`).

Los 6 candidatos se guardan en `store_match` con su categoría, sus puntajes y el
motivo, también los DIFERENTES. Las tiendas son REFERENCIA: no cambian el color
del semáforo (que sale de Mercado Libre) salvo que `pm_stores_affect_color` esté
en 1; en ese caso el color usa la mediana de los idénticos de ML Y de las tiendas.
Un precio dudoso nunca cuenta (ni para el color ni para "más barato afuera").

"No es el mismo" / "Es el mismo": una persona corrige un candidato. Queda en
`store_match_feedback` y vale para ese producto en las próximas corridas.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
from rapidfuzz import fuzz, process
from sqlalchemy import case, delete, exists, func
from sqlmodel import Session, select

from app import runtime
from app.clock import utcnow
from app.db.models import (
    ImageEmbedCache,
    MarketPriceSnapshot,
    MarketStore,
    PriceMonitorRun,
    StoreCatalogItem,
    StoreMatch,
    StoreMatchFeedback,
)
from app.db.session import engine
from app.dedup import fuzzy_text
from app.pricing import market_judge, market_match, market_specs, semaforo, store_catalog, store_parse, store_urls
from app.pricing.market_ml import MlCandidate
from app.vendure.client import VendureProduct

log = logging.getLogger(__name__)

ORIGIN_STORE = "store"
CANDIDATES_PER_STORE = 6
# Peso de token_set_ratio (recall) frente a token_sort_ratio (precisión) en el prefiltro.
_SET_WEIGHT = 0.6

IGUAL, SIMILAR, DIFERENTE = market_judge.CAT_IGUAL, market_judge.CAT_SIMILAR, market_judge.CAT_DIFERENTE
CATEGORIES = (IGUAL, SIMILAR, DIFERENTE)
_CAT_RANK = {IGUAL: 0, SIMILAR: 1, DIFERENTE: 2}
LABEL_YES, LABEL_NO = "es", "no_es"
LABELS = (LABEL_YES, LABEL_NO)
SOURCE_ML = "ml"
# Un precio 10 veces más chico o más grande que el nuestro es casi seguro un dato malo.
PRICE_RATIO_LIMIT = 10.0
_DIFFERENCES_OK = frozenset({*market_judge.DIFFERENCES, market_specs.DIFF_QUANTITY, market_specs.DIFF_CAPACITY,
                             market_specs.DIFF_SIZE, market_specs.DIFF_WEIGHT})

JudgeTool = Callable[..., Awaitable[None]]
BrandTool = Callable[[MlCandidate], str]


def store_key(store_id: int) -> str:
    return f"store:{store_id}"


# ─── El índice de una tienda en memoria ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: int
    url: str
    title: str
    price_cents: int | None
    price_doubtful: bool
    price_note: str
    image_url: str | None
    brand: str
    stock: int | None


@dataclass(slots=True)
class StoreIndex:
    """Los productos leídos de una tienda, con el título normalizado para el prefiltro."""
    info: store_catalog.StoreInfo
    entries: list[CatalogEntry]
    normalized: list[str] = field(default_factory=list)
    by_id: dict[int, CatalogEntry] = field(default_factory=dict)
    _position: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.normalized = [fuzzy_text._normalize(e.title) for e in self.entries]
        self.by_id = {e.id: e for e in self.entries}
        self._position = {e.id: i for i, e in enumerate(self.entries)}

    def prefilter(self, query: str, k: int = CANDIDATES_PER_STORE, *, exclude: frozenset[int] = frozenset(),
                  force: frozenset[int] = frozenset()) -> list[CatalogEntry]:
        """Los `k` productos de la tienda con el nombre más parecido, y los que una
        persona confirmó (`force`), sin los que descartó (`exclude`).

        `token_set_ratio` solo da 100 a todo título que contenga las palabras del nuestro
        (y empata a cientos): se le suma `token_sort_ratio`, que premia al que además tiene
        las mismas palabras y no otras. Se puntúan TODOS los títulos de la tienda (rapidfuzz
        en C++, unos milisegundos para 20.000), no un recorte."""
        q = fuzzy_text._normalize(query)
        picked: list[CatalogEntry] = []
        if q and self.entries:
            by_set = process.cdist([q], self.normalized, scorer=fuzz.token_set_ratio, processor=None, workers=1)[0]
            by_sort = process.cdist([q], self.normalized, scorer=fuzz.token_sort_ratio, processor=None, workers=1)[0]
            score = _SET_WEIGHT * by_set + (1.0 - _SET_WEIGHT) * by_sort
            for item_id in exclude:
                pos = self._position.get(item_id)
                if pos is not None:
                    score[pos] = -1.0
            order = np.argsort(-score, kind="stable")[:k]
            picked = [self.entries[int(i)] for i in order if score[int(i)] >= 0]
        have = {e.id for e in picked}
        picked += [self.by_id[i] for i in sorted(force) if i in self.by_id and i not in have]
        return picked


def load_index(info: store_catalog.StoreInfo) -> StoreIndex:
    """Lo vivo y legible de una tienda: con título, leído alguna vez, sin `dead` y que
    sigue en el sitemap. Las fotos se vuelven a sanear con los hosts de HOY."""
    with Session(engine) as s:
        rows = s.exec(
            select(StoreCatalogItem).where(
                StoreCatalogItem.store_id == info.id, StoreCatalogItem.in_sitemap.is_(True),  # type: ignore[attr-defined]
                StoreCatalogItem.dead.is_(False), StoreCatalogItem.last_seen_at.is_not(None),  # type: ignore[attr-defined,union-attr]
                StoreCatalogItem.title.is_not(None))  # type: ignore[union-attr]
        ).all()
    entries = [
        CatalogEntry(
            id=int(r.id), url=r.url, title=r.title or "", price_cents=r.price_cents,  # type: ignore[arg-type]
            price_doubtful=bool(r.price_doubtful), price_note=r.price_note or "",
            image_url=store_urls.safe_image(r.image_url, info.image_hosts), brand=r.brand or "", stock=r.stock)
        for r in rows
    ]
    return StoreIndex(info=info, entries=entries)


# ─── La corrida ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StoresRun:
    stores: list[StoreIndex]
    # (product_id, store_id) → {item_id: "es" | "no_es"}
    feedback: dict[tuple[str, int], dict[int, str]]
    affect_color: bool
    topup: list[store_catalog.IndexReport] = field(default_factory=list)


def load_feedback() -> dict[tuple[str, int], dict[int, str]]:
    out: dict[tuple[str, int], dict[int, str]] = {}
    try:
        with Session(engine) as s:
            for pid, sid, iid, label in s.exec(select(
                    StoreMatchFeedback.product_id, StoreMatchFeedback.store_id,
                    StoreMatchFeedback.item_id, StoreMatchFeedback.label)).all():
                out.setdefault((pid, sid), {})[iid] = label
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo leer store_match_feedback: %s", exc)
    return out


async def prepare(**topup_kwargs: Any) -> StoresRun | None:
    """Al empezar la corrida: refresca el índice viejo (dentro del cupo y del tiempo
    de `pm_stores_topup_minutes`) y carga en memoria el de cada tienda activa. None
    si no hay tiendas activas. Nunca lanza: sin tiendas la corrida sigue con ML."""
    try:
        minutes = int(runtime.get("pm_stores_topup_minutes") or 0)
        topup = await store_catalog.topup_for_run(minutes, **topup_kwargs) if minutes > 0 else []
        await asyncio.to_thread(store_catalog.refresh_allowed_image_hosts)
        infos = await asyncio.to_thread(store_catalog.active_stores)
        if not infos:
            return None
        stores = await asyncio.to_thread(lambda: [load_index(i) for i in infos])
        feedback = await asyncio.to_thread(load_feedback)
    except Exception as exc:  # noqa: BLE001
        log.warning("tiendas: no se pudieron preparar para la corrida: %s", exc)
        return None
    for st in stores:
        log.info("tiendas: %s → %d productos indexados para comparar", st.info.name, len(st.entries))
    return StoresRun(stores=stores, feedback=feedback, affect_color=bool(int(runtime.get("pm_stores_affect_color") or 0)),
                     topup=topup)


# ─── Un producto contra una tienda ──────────────────────────────────────────


def _candidate(info: store_catalog.StoreInfo, e: CatalogEntry) -> MlCandidate:
    """Una ficha de tienda con la forma que entienden `market_match` y el juez.
    La marca propia de la tienda cuenta como genérica (Nico: genérica = igual)."""
    house = info.house_brand.lower()
    brand = "" if house and e.brand.lower() == house else e.brand
    return MlCandidate(
        id=f"s{info.id}:{e.id}", name=e.title, image_urls=[e.image_url] if e.image_url else [],
        permalink=e.url, origin=ORIGIN_STORE, price_cents=e.price_cents, currency="ARS", brand=brand,
        seller=info.name,
    )


def _entry_id(c: MlCandidate) -> int:
    return int(c.id.split(":", 1)[1])


async def _store_scorer(our: VendureProduct, urls: list[str]) -> float | None:
    """CLIP: la foto del candidato (ya saneada con los hosts de su tienda) contra las nuestras."""
    return await market_match.clip_score_urls(our, [u for u in urls if u])


def _category(d: market_match.Decision) -> str:
    return {market_match.MATCH: IGUAL, market_match.SIMILAR: SIMILAR}.get(d.verdict, DIFERENTE)


def _why_different(d: market_match.Decision, thr: market_match.Thresholds) -> str:
    if d.judged and d.reason:
        return d.reason
    if d.image_score is None:
        return "sin foto para comparar"
    if d.image_score < thr.image_veto:
        return "la foto no se parece"
    if d.name_score < thr.name_veto:
        return "el nombre no se parece"
    if d.verdict == market_match.AMBIGUOUS:
        return "dudoso y sin juez IA para decidir: no se da por igual"
    return d.reason or "no es el mismo producto"


def _doubt(price: int | None, entry_doubtful: bool, entry_note: str, our_price: int | None) -> tuple[bool, str]:
    """¿Este precio no se puede creer? Lo marca el indexador (no coincide con la página,
    absurdo) o la comparación con el nuestro (10 veces más o menos)."""
    if price is None or price <= 0:
        return False, ""
    if entry_doubtful:
        return True, entry_note
    if our_price and our_price > 0:
        ratio = price / our_price
        if ratio < 1 / PRICE_RATIO_LIMIT or ratio > PRICE_RATIO_LIMIT:
            return True, "el precio es 10 veces más chico o más grande que el nuestro"
    return False, ""


def _targets(decisions: list[market_match.Decision], brand_of: BrandTool,
             human: set[str]) -> list[market_match.Decision]:
    """A quién le pregunta el juez: la banda ambigua y los que ya pasaron por reglas pero
    declaran una marca (regla de marca de Nico). Lo que confirmó una persona no se discute."""
    ambiguous = [d for d in decisions if d.verdict == market_match.AMBIGUOUS and d.candidate.id not in human]
    branded = [d for d in decisions if d.verdict == market_match.MATCH and d.candidate.id not in human
               and brand_of(d.candidate)]
    branded.sort(key=lambda d: d.image_score or 0.0, reverse=True)
    return (ambiguous + branded)[:market_judge.MAX_CANDIDATES]


async def _match_store(run: StoresRun, store: StoreIndex, ctx: Any, product: VendureProduct, query: str,
                       specs: market_specs.OurSpecs | None, our_price: int | None,
                       judge: JudgeTool, brand_of: BrandTool) -> list[StoreMatch]:
    labels = run.feedback.get((product.id, store.info.id), {})
    excluded = frozenset(i for i, lab in labels.items() if lab == LABEL_NO)
    confirmed = frozenset(i for i, lab in labels.items() if lab == LABEL_YES)
    entries = store.prefilter(query, CANDIDATES_PER_STORE, exclude=excluded, force=confirmed)
    if not entries:
        return []
    candidates = [_candidate(store.info, e) for e in entries]
    decisions = await market_match.score_candidates(
        product, candidates, ctx.thresholds, scorer=_store_scorer, max_candidates=len(candidates))
    human = {c.id for c, e in zip(candidates, entries) if e.id in confirmed}
    # Lo que habría dicho Hugo por reglas (sirve para deshacer la corrección humana).
    auto = {d.candidate.id: _category(d) for d in decisions}
    for d in decisions:
        if d.candidate.id in human:
            d.verdict, d.source, d.confidence, d.reason = market_match.MATCH, "humano", 1.0, "marcado a mano"
    targets = _targets(decisions, brand_of, human)
    if targets and ctx.judge_max_calls > 0:
        await judge(ctx, product, targets, {})
    if ctx.spec_check:
        for d in decisions:
            if d.candidate.id not in human:
                market_match.apply_specs(d, product.name, specs, dim_tol_pct=ctx.dim_tol_pct,
                                         weight_tol_pct=ctx.weight_tol_pct)
    rows: list[StoreMatch] = []
    for d in decisions:
        entry = store.by_id[_entry_id(d.candidate)]
        category = _category(d)
        doubtful, note = _doubt(entry.price_cents, entry.price_doubtful, entry.price_note, our_price)
        reason = _why_different(d, ctx.thresholds) if category == DIFERENTE else (d.reason or "")
        rows.append(StoreMatch(
            run_id=ctx.run_id, product_id=product.id, store_id=store.info.id, item_id=entry.id,
            category=category, auto_category=auto[d.candidate.id],
            source=(d.source or ("veto" if d.verdict == market_match.NO and d.image_score is not None else "none"))[:16],
            title=store_parse.one_line(entry.title, 300), url=entry.url, image_url=entry.image_url,
            brand=store_parse.one_line(entry.brand, 80) or None,
            price_cents=entry.price_cents, price_doubtful=doubtful, price_note=note[:200] or None, stock=entry.stock,
            image_score=None if d.image_score is None else round(d.image_score, 3),
            name_score=round(d.name_score, 3),
            confidence=d.confidence, reason=store_parse.one_line(reason, 300) or None,
            differences=json.dumps(list(d.differences)) if d.differences else None,
            notes=store_parse.one_line(" · ".join(d.notes), 300) or None,
            human_label=LABEL_YES if d.candidate.id in human else None,
        ))
    rows.sort(key=lambda r: (_CAT_RANK[r.category], -(r.image_score or 0.0), -(r.name_score or 0.0)))
    for i, r in enumerate(rows, 1):
        r.rank = i
    return rows


def _specs_of(snap: MarketPriceSnapshot) -> market_specs.OurSpecs | None:
    try:
        data = json.loads(snap.our_specs) if snap.our_specs else None
    except ValueError:
        return None
    return market_specs.OurSpecs.from_dict(data) if isinstance(data, dict) else None


def save_matches(run_id: int, product_id: str, rows: list[StoreMatch]) -> None:
    """Reemplaza lo guardado de este producto en esta corrida. Borrar y volver a
    insertar hace idempotente la reanudación: si la corrida se corta entre esto y el
    snapshot, el producto se reevalúa y no quedan filas duplicadas ni huérfanas."""
    # expire_on_commit=False: el llamador sigue leyendo las filas (para el color) con la sesión cerrada.
    with Session(engine, expire_on_commit=False) as s:
        s.execute(delete(StoreMatch).where(StoreMatch.run_id == run_id, StoreMatch.product_id == product_id))
        s.add_all(rows)
        s.commit()


async def attach(ctx: Any, product: VendureProduct, snap: MarketPriceSnapshot, *,
                 judge: JudgeTool, brand_of: BrandTool) -> MarketPriceSnapshot:
    """Compara el producto contra todas las tiendas activas, guarda los candidatos y, con
    `pm_stores_affect_color`, deja que cuenten para el color. Nunca lanza: lo de las
    tiendas es referencia, no puede romper el resultado de Mercado Libre."""
    run: StoresRun | None = ctx.extra.get("stores")
    if run is None or not run.stores:
        return snap
    query = market_match.search_query(product.name)
    if not query or not (product.featured_image_url or product.image_urls) or not ctx.can_score(product):
        return snap
    try:
        specs = _specs_of(snap)
        rows: list[StoreMatch] = []
        for store in run.stores:
            rows += await _match_store(run, store, ctx, product, query, specs, snap.our_price_cents, judge, brand_of)
        await asyncio.to_thread(save_matches, ctx.run_id, product.id, rows)
        if run.affect_color:
            apply_color(snap, rows, green_min=ctx.green_min, yellow_min=ctx.yellow_min)
    except Exception:  # noqa: BLE001
        log.warning("tiendas: %s no se pudo comparar", product.id, exc_info=True)
    return snap


# ─── Color (opcional) ───────────────────────────────────────────────────────


def counting_prices(rows: list[StoreMatch]) -> list[int]:
    """Precios de las tiendas que pueden contar: IGUALES y creíbles."""
    return [int(r.price_cents) for r in rows
            if r.category == IGUAL and r.price_cents and r.price_cents > 0 and not r.price_doubtful]


def ml_prices(snap: MarketPriceSnapshot) -> list[int]:
    """Precios de Mercado Libre que entraron a la mediana del snapshot."""
    if snap.ml_status != "ok":
        return []
    try:
        matched = json.loads(snap.matched_listings) if snap.matched_listings else []
    except ValueError:
        return []
    out: list[int] = []
    for m in matched if isinstance(matched, list) else []:
        if not isinstance(m, dict):
            continue
        listed = m.get("prices_cents")
        if not (isinstance(listed, list) and listed):
            n = max(1, int(m.get("listings") or 1))
            listed = [m.get("median_cents")] * n          # filas anteriores a `prices_cents`
        out.extend(int(p) for p in listed if isinstance(p, (int, float)) and p > 0)
    return out


def _recolor(snap: MarketPriceSnapshot, prices: list[int], *, green_min: float, yellow_min: float) -> None:
    median = semaforo.median_cents(prices)
    margin = semaforo.estimated_margin_pct(median, snap.our_price_cents, snap.commission_pct or 0.0,
                                           snap.shipping_cents or 0)
    snap.color = semaforo.color(margin, snap.our_price_cents, median, green_min, yellow_min)
    snap.est_margin_pct = None if margin is None else round(margin, 2)


def apply_color(snap: MarketPriceSnapshot, rows: list[StoreMatch], *, green_min: float, yellow_min: float) -> bool:
    """Color y ganancia con la mediana de TODOS los idénticos (ML + tiendas). No toca
    los campos `ml_*`: la mediana, el mínimo y las publicaciones de ML siguen siendo de
    ML. Sin precios de tienda que valgan, deja el color como lo dejó ML."""
    store = counting_prices(rows)
    if not store or not snap.our_price_cents:
        return False
    ml = ml_prices(snap)
    _recolor(snap, ml + store, green_min=green_min, yellow_min=yellow_min)
    snap.price_basis = "ml+tiendas" if ml else "tiendas"
    return True


def affect_color_enabled() -> bool:
    return bool(int(runtime.get("pm_stores_affect_color") or 0))


def reapply_color(session: Session, snap: MarketPriceSnapshot) -> None:
    """Recalcula el color de un snapshot ya guardado después de que una persona corrigió
    una coincidencia (de ML o de una tienda). No hace nada si las tiendas no cuentan y el
    snapshot nunca las usó."""
    enabled = affect_color_enabled()
    if not enabled and snap.price_basis == "ml":
        return
    rows = list(session.exec(select(StoreMatch).where(
        StoreMatch.run_id == snap.run_id, StoreMatch.product_id == snap.product_id)).all())
    store = counting_prices(rows) if enabled else []
    ml = ml_prices(snap)
    prices = ml + store
    if not snap.our_price_cents:
        return
    if prices:
        _recolor(snap, prices, green_min=float(runtime.get("pm_green_min_pct")),
                 yellow_min=float(runtime.get("pm_yellow_min_pct")))
    else:
        snap.color, snap.est_margin_pct = semaforo.SIN_DATO, None
    snap.price_basis = ("ml+tiendas" if ml else "tiendas") if store else "ml"


# ─── Contadores por fuente (card de Salud) ──────────────────────────────────


def source_stats(session: Session, run_id: int) -> dict[str, dict[str, Any]]:
    """Por fuente (Mercado Libre y cada tienda): cuántos productos de la corrida tienen un
    idéntico, solo similares, solo diferentes o nada."""
    snaps = session.exec(select(
        MarketPriceSnapshot.ml_status, MarketPriceSnapshot.similar_count, MarketPriceSnapshot.candidates_count,
    ).where(MarketPriceSnapshot.run_id == run_id)).all()
    total = len(snaps)
    ml: Counter[str] = Counter()
    for status, similar, candidates in snaps:
        if status == "ok":
            ml[IGUAL] += 1
        elif (similar or 0) > 0:
            ml[SIMILAR] += 1
        elif (candidates or 0) > 0:
            ml[DIFERENTE] += 1
        else:
            ml["nada"] += 1
    out: dict[str, dict[str, Any]] = {
        SOURCE_ML: {"label": "Mercado Libre", "total": total, **{k: ml[k] for k in (*CATEGORIES, "nada")}}}
    rank = case((StoreMatch.category == IGUAL, 0), (StoreMatch.category == SIMILAR, 1), else_=2)
    best = session.exec(
        select(StoreMatch.store_id, StoreMatch.product_id, func.min(rank))
        .where(StoreMatch.run_id == run_id).group_by(StoreMatch.store_id, StoreMatch.product_id)).all()
    per_store: dict[int, Counter[str]] = {}
    for sid, _pid, r in best:
        per_store.setdefault(int(sid), Counter())[CATEGORIES[int(r)]] += 1
    names = {int(i): (n, bool(en)) for i, n, en in session.exec(
        select(MarketStore.id, MarketStore.name, MarketStore.enabled)).all()}
    for sid, (name, enabled) in sorted(names.items()):
        if not enabled and sid not in per_store:
            continue
        c = per_store.get(sid, Counter())
        out[store_key(sid)] = {
            "label": name, "total": total, **{k: c[k] for k in CATEGORIES},
            "nada": max(0, total - sum(c[k] for k in CATEGORIES))}
    return out


def run_sources(run: PriceMonitorRun) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(run.source_stats) if run.source_stats else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ─── Para el dashboard ───────────────────────────────────────────────────────


def _store_infos(session: Session) -> dict[int, store_catalog.StoreInfo]:
    return {int(r.id): store_catalog.StoreInfo.from_row(r)
            for r in session.exec(select(MarketStore).where(MarketStore.enabled.is_(True)).order_by(MarketStore.id)).all()}  # type: ignore[attr-defined]


def sources_meta(session: Session) -> list[dict[str, Any]]:
    """Las columnas de fuente de la tabla: Mercado Libre y las tiendas activas."""
    return [{"key": SOURCE_ML, "label": "Mercado Libre"}] + [
        {"key": store_key(i), "label": info.name} for i, info in _store_infos(session).items()]


def match_to_dict(m: StoreMatch, info: store_catalog.StoreInfo) -> dict[str, Any]:
    """Un candidato listo para servir. Se sanea al servir (no solo al guardar): el link
    tiene que ser https a la tienda, la foto a su CDN y lo demás, texto de una línea."""
    try:
        diffs = json.loads(m.differences) if m.differences else []
    except ValueError:
        diffs = []
    return {
        "id": m.id, "store_id": m.store_id, "store": info.name, "rank": m.rank, "category": m.category,
        "auto_category": m.auto_category, "source": m.source,
        "title": store_parse.one_line(m.title, 160),
        "url": store_urls.safe_link(m.url, info.base_url) or None,
        "image_url": store_urls.safe_image(m.image_url, info.image_hosts),
        "brand": store_parse.one_line(m.brand, 40) or None,
        "price_cents": m.price_cents, "price_doubtful": bool(m.price_doubtful),
        "price_note": store_parse.one_line(m.price_note, 200) or None, "stock": m.stock,
        "image_score": m.image_score, "name_score": m.name_score, "confidence": m.confidence,
        "differences": [d for d in diffs if isinstance(d, str) and d in _DIFFERENCES_OK],
        "reason": store_parse.one_line(m.reason, 300) or None,
        "notes": store_parse.one_line(m.notes, 300) or None,
        "human_label": m.human_label if m.human_label in LABELS else None,
    }


def _cell(key: str, label: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """La celda de una tienda: su mejor resultado (idéntico; si no hay, similar; si no, el
    más parecido) con el precio, el veredicto y el link."""
    counts = {c: sum(1 for m in matches if m["category"] == c) for c in CATEGORIES}
    best = min(matches, key=lambda m: (_CAT_RANK[m["category"]], m["rank"]), default=None)
    cell: dict[str, Any] = {"key": key, "label": label, "category": None, "counts": counts}
    if best is not None:
        cell.update(category=best["category"], price_cents=best["price_cents"], price_doubtful=best["price_doubtful"],
                    price_note=best["price_note"], title=best["title"], url=best["url"], image_url=best["image_url"],
                    match_id=best["id"], human_label=best["human_label"])
    return cell


def _ml_cell(item: dict[str, Any]) -> dict[str, Any]:
    """La celda de Mercado Libre, armada con lo que ya trae el snapshot servido."""
    matched = item.get("matched_listings") or []
    similar = item.get("similar_listings") or []
    different = item.get("different_listings") or []
    counts = {IGUAL: len(matched), SIMILAR: int(item.get("similar_count") or len(similar)), DIFERENTE: len(different)}
    cell: dict[str, Any] = {"key": SOURCE_ML, "label": "Mercado Libre", "category": None, "counts": counts}
    if matched and item.get("ml_status") == "ok":
        b = matched[0]
        cell.update(category=IGUAL, price_cents=item.get("ml_median_cents"), title=b.get("title"), url=b.get("permalink") or None)
    elif similar:
        b = similar[0]
        cell.update(category=SIMILAR, price_cents=b.get("price_cents"), title=b.get("title"), url=b.get("permalink") or None)
    elif different:
        b = different[0]
        cell.update(category=DIFERENTE, price_cents=b.get("price_cents"), title=b.get("title"), url=b.get("permalink") or None)
    cell.setdefault("price_doubtful", False)
    return cell


def _cheapest(item: dict[str, Any], by_store: dict[int, list[dict[str, Any]]],
              infos: dict[int, store_catalog.StoreInfo]) -> dict[str, Any] | None:
    """El precio más bajo entre los idénticos de todas las fuentes (sin precios dudosos)."""
    options: list[dict[str, Any]] = []
    if item.get("ml_status") == "ok" and item.get("ml_min_cents"):
        low = item["ml_min_cents"]
        listing = next((m for m in item.get("matched_listings") or [] if m.get("min_cents") == low),
                       (item.get("matched_listings") or [{}])[0])
        options.append({"key": SOURCE_ML, "label": "Mercado Libre", "price_cents": low,
                        "title": listing.get("title"), "url": listing.get("permalink") or None})
    for sid, matches in by_store.items():
        ok = [m for m in matches if m["category"] == IGUAL and m["price_cents"] and not m["price_doubtful"]]
        if ok:
            m = min(ok, key=lambda x: x["price_cents"])
            options.append({"key": store_key(sid), "label": infos[sid].name, "price_cents": m["price_cents"],
                            "title": m["title"], "url": m["url"]})
    return min(options, key=lambda o: o["price_cents"], default=None)


def decorate_items(session: Session, run_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suma a cada fila de la tabla una celda por fuente (`cells`), el detalle de cada tienda
    (`stores`) y "más barato afuera" (`cheapest_outside`). Una sola consulta para toda la página."""
    infos = _store_infos(session)
    ids = [it["product"]["id"] for it in items]
    rows = session.exec(select(StoreMatch).where(
        StoreMatch.run_id == run_id, StoreMatch.product_id.in_(ids),  # type: ignore[attr-defined]
        StoreMatch.store_id.in_(list(infos) or [-1]))  # type: ignore[attr-defined]
        .order_by(StoreMatch.store_id, StoreMatch.rank)).all() if ids else []
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for m in rows:
        grouped.setdefault((m.product_id, m.store_id), []).append(match_to_dict(m, infos[m.store_id]))
    for it in items:
        pid = it["product"]["id"]
        by_store = {sid: grouped.get((pid, sid), []) for sid in infos}
        cells = {SOURCE_ML: _ml_cell(it)}
        cells.update({store_key(sid): _cell(store_key(sid), infos[sid].name, ms) for sid, ms in by_store.items()})
        it["cells"] = cells
        it["stores"] = {str(sid): {"label": infos[sid].name, "matches": ms} for sid, ms in by_store.items()}
        it["cheapest_outside"] = _cheapest(it, by_store, infos)
        it["price_basis"] = it.get("price_basis") or "ml"
    return items


def parse_source(value: str | None) -> str | None:
    """"ml" o el id de una tienda ("3" / "store:3") → la clave canónica, o None si es inválida."""
    v = (value or "").strip().lower()
    if v == SOURCE_ML:
        return SOURCE_ML
    v = v.removeprefix("store:")
    return store_key(int(v)) if v.isdigit() and int(v) < 2**31 else None


def filter_conditions(source: str | None, igual_in: list[str]) -> list[Any]:
    """Condiciones SQL de los filtros por fuente: `source` = tiene algo de esa fuente;
    `igual_in` = tiene un idéntico en alguna de esas fuentes."""
    def has_rows(sid: int, category: str | None = None):
        conds = [StoreMatch.run_id == MarketPriceSnapshot.run_id, StoreMatch.product_id == MarketPriceSnapshot.product_id,
                 StoreMatch.store_id == sid]
        if category:
            conds.append(StoreMatch.category == category)
        return exists().where(*conds)

    out: list[Any] = []
    if source == SOURCE_ML:
        out.append(MarketPriceSnapshot.candidates_count > 0)
    elif source:
        out.append(has_rows(int(source.split(":", 1)[1])))
    if igual_in:
        from sqlalchemy import or_

        parts = [(MarketPriceSnapshot.ml_status == "ok") if k == SOURCE_ML else has_rows(int(k.split(":", 1)[1]), IGUAL)
                 for k in igual_in]
        out.append(or_(*parts))
    return out


# ─── "No es el mismo" / "Es el mismo" ───────────────────────────────────────


def set_label(session: Session, match_id: int, label: str, actor: str | None = None) -> StoreMatch | None:
    """Una persona dice que este candidato SÍ o NO es el mismo producto. Cambia su categoría
    en esta corrida, recuerda la corrección para las próximas y rehace el color si hace falta.
    No commitea: lo hace el llamador, junto con los contadores de la corrida
    (`price_monitor.recount_run`)."""
    if label not in LABELS:
        raise ValueError("label inválido")
    m = session.get(StoreMatch, match_id)
    if m is None:
        return None
    fb = session.exec(select(StoreMatchFeedback).where(
        StoreMatchFeedback.product_id == m.product_id, StoreMatchFeedback.store_id == m.store_id,
        StoreMatchFeedback.item_id == m.item_id)).first()
    if fb is None:
        product_name = session.exec(select(MarketPriceSnapshot.product_name).where(
            MarketPriceSnapshot.run_id == m.run_id, MarketPriceSnapshot.product_id == m.product_id)).first()
        fb = StoreMatchFeedback(
            product_id=m.product_id, store_id=m.store_id, item_id=m.item_id, label=label,
            auto_category=m.auto_category, image_score=m.image_score, name_score=m.name_score,
            title=m.title[:300], product_name=(product_name or "")[:200] or None, actor=(actor or "")[:120] or None)
    else:
        fb.label, fb.actor = label, (actor or "")[:120] or fb.actor
    session.add(fb)
    m.human_label = label
    m.category = IGUAL if label == LABEL_YES else DIFERENTE
    session.add(m)
    _after_label(session, m)
    return m


def clear_label(session: Session, match_id: int) -> StoreMatch | None:
    """Deshace la corrección: el candidato vuelve a la categoría que dijo Hugo."""
    m = session.get(StoreMatch, match_id)
    if m is None:
        return None
    session.execute(delete(StoreMatchFeedback).where(
        StoreMatchFeedback.product_id == m.product_id, StoreMatchFeedback.store_id == m.store_id,
        StoreMatchFeedback.item_id == m.item_id))
    m.human_label, m.category = None, m.auto_category
    session.add(m)
    _after_label(session, m)
    return m


def _after_label(session: Session, m: StoreMatch) -> None:
    session.flush()
    snap = session.exec(select(MarketPriceSnapshot).where(
        MarketPriceSnapshot.run_id == m.run_id, MarketPriceSnapshot.product_id == m.product_id)).first()
    if snap is not None:
        reapply_color(session, snap)
        session.add(snap)
    session.flush()


# ─── Retención ───────────────────────────────────────────────────────────────


def prune(retention_days: int) -> dict[str, int]:
    """Borra las coincidencias de corridas que ya no tienen snapshots (la poda del semáforo
    se los saca) y los productos de tienda que hace más de 90 días que no figuran en el sitemap."""
    out = {"matches": 0, "items": 0}
    with Session(engine) as s:
        if retention_days > 0:
            live = select(MarketPriceSnapshot.run_id).distinct()
            out["matches"] = s.execute(delete(StoreMatch).where(StoreMatch.run_id.notin_(live))).rowcount or 0  # type: ignore[attr-defined]
        out["items"] = s.execute(delete(StoreCatalogItem).where(
            StoreCatalogItem.in_sitemap.is_(False),  # type: ignore[attr-defined]
            StoreCatalogItem.last_checked_at < utcnow() - timedelta(days=90))).rowcount or 0  # type: ignore[arg-type]
        s.commit()
    if any(out.values()):
        log.info("prune: %d coincidencias de tiendas y %d productos de tienda viejos", out["matches"], out["items"])
    return out


def prune_embed_cache(days: int) -> int:
    """Poda el cache de embeddings de las fotos de tiendas más viejo que `days` (las de ML las
    poda el job de siempre; las de nuestro catálogo no se tocan)."""
    hosts = store_urls.allowed_image_hosts()
    if days <= 0 or not hosts:
        return 0
    from sqlalchemy import or_

    cutoff = utcnow() - timedelta(days=days)
    with Session(engine) as s:
        result = s.execute(delete(ImageEmbedCache).where(
            ImageEmbedCache.updated_at < cutoff,  # type: ignore[arg-type]
            or_(*[ImageEmbedCache.url.like(f"https://%{h}/%") for h in hosts])))  # type: ignore[attr-defined]
        s.commit()
    return int(result.rowcount or 0)
