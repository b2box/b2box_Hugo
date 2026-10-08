"""Calibra el filtro "mismo producto" del semáforo contra un set ETIQUETADO.

Criterio de aceptación de la etapa sombra: con ~100 productos etiquetados a
mano, el filtro tiene que dar precisión ≥ 90 % y recall ≥ 60 % (un precio de
referencia equivocado es peor que un `sin_dato`, por eso se exige más
precisión que recall).

Dos pasos:

1. `export` (en el container de prod, con ML y CLIP disponibles): toma una
   muestra de productos habilitados, busca cada uno en ML igual que el job y
   escribe un CSV con TODAS las fichas puntuadas (no solo las aceptadas) y la
   columna `same_product` vacía. Gasta requests del budget diario de ML
   (`pm_ml_daily_budget`): ~1-2 por producto.

       python -m app.pricing.calibrate_market_match export --sample 100 --out pares.csv

2. Una persona completa `same_product` (1/0, si/no, true/false; vacío = no
   etiquetado, se ignora) mirando las dos fotos y los títulos.

3. `evaluate` (offline, sin red): mide precisión y recall con los umbrales
   actuales del dashboard y, con `--grid`, propone la combinación que maximiza
   el recall respetando la precisión mínima.

       python -m app.pricing.calibrate_market_match evaluate pares.csv
       python -m app.pricing.calibrate_market_match evaluate pares.csv --grid
       python -m app.pricing.calibrate_market_match evaluate pares.csv --image 0.62 --name 0.55

Los pares que una persona marcó "No es el mismo" en el dashboard
(`market_match_feedback`) salen en el CSV ya etiquetados con 0.

La banda ambigua se cuenta como "no es el mismo producto" (así se comporta el
job con el juez LLM apagado) y se informa aparte cuántos positivos cayeron ahí:
es lo que el juez podría rescatar.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from app.pricing import market_match

CSV_FIELDS = (
    "product_id", "product_code", "our_name", "our_image_url",
    "ml_id", "ml_title", "ml_permalink", "ml_image_url",
    "image_score", "name_score", "verdict", "same_product",
)

# Celdas que una planilla interpretaría como fórmula (CSV injection): títulos
# de ML y nombres los escribe cualquiera. Solo columnas de texto; los scores
# pueden ser negativos (escala centrada) y tienen que seguir siendo números.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_TEXT_FIELDS = ("product_id", "product_code", "our_name", "our_image_url", "ml_id", "ml_title",
                "ml_permalink", "ml_image_url", "verdict")

_TRUE = {"1", "si", "sí", "s", "yes", "y", "true", "x"}
_FALSE = {"0", "no", "n", "false"}


@dataclass(slots=True, frozen=True)
class LabeledPair:
    product_id: str
    ml_id: str
    image_score: float | None
    name_score: float
    same_product: bool


@dataclass(slots=True, frozen=True)
class Metrics:
    tp: int
    fp: int
    fn: int
    tn: int
    positives_in_ambiguous: int
    products_with_match: int
    products_labeled: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _unescape(text: str | None) -> str:
    """Inverso de csv_safe para leer el CSV etiquetado."""
    text = text or ""
    return text[1:] if text.startswith("'") and text[1:].startswith(_FORMULA_PREFIXES) else text


def export_row(product, decision, *, known_negative: bool = False) -> dict[str, str]:
    """Una fila del CSV a etiquetar (texto escapado contra fórmulas).
    `known_negative`: alguien ya marcó "No es el mismo" en el dashboard; la fila
    sale etiquetada 0 (se puede corregir en la planilla)."""
    row = {
        "product_id": product.id, "product_code": product.product_code or "",
        "our_name": product.name, "our_image_url": product.featured_image_url or "",
        "ml_id": decision.candidate.id, "ml_title": decision.candidate.name,
        "ml_permalink": decision.candidate.permalink,
        "ml_image_url": (decision.candidate.image_urls or [""])[0],
        "verdict": decision.verdict,
    }
    out = {k: csv_safe(v) for k, v in row.items()}
    out["image_score"] = "" if decision.image_score is None else f"{decision.image_score:.4f}"
    out["name_score"] = f"{decision.name_score:.4f}"
    out["same_product"] = "0" if known_negative else ""
    return out


def parse_label(raw: str | None) -> bool | None:
    v = (raw or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _float_or_none(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def load_labeled(rows: Iterable[dict[str, str]]) -> list[LabeledPair]:
    """Filas del CSV → pares etiquetados. Sin etiqueta = se ignora. Si falta
    `name_score` pero están los dos títulos, se calcula acá (local, sin red)."""
    out: list[LabeledPair] = []
    for row in rows:
        label = parse_label(row.get("same_product"))
        if label is None:
            continue
        name = _float_or_none(row.get("name_score"))
        if name is None:
            name = market_match.name_score(_unescape(row.get("our_name")), _unescape(row.get("ml_title")))
        out.append(LabeledPair(
            product_id=_unescape(row.get("product_id")).strip(),
            ml_id=_unescape(row.get("ml_id")).strip(),
            image_score=_float_or_none(row.get("image_score")),
            name_score=float(name),
            same_product=label,
        ))
    return out


def evaluate(pairs: Sequence[LabeledPair], thr: market_match.Thresholds) -> Metrics:
    tp = fp = fn = tn = ambiguous_pos = 0
    with_match: set[str] = set()
    for p in pairs:
        verdict, _source = market_match.classify(p.image_score, p.name_score, thr)
        predicted = verdict == market_match.MATCH
        if predicted:
            with_match.add(p.product_id)
        if p.same_product and predicted:
            tp += 1
        elif p.same_product:
            fn += 1
            if verdict == market_match.AMBIGUOUS:
                ambiguous_pos += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    return Metrics(tp, fp, fn, tn, ambiguous_pos, len(with_match), len({p.product_id for p in pairs}))


def _frange(lo: float, hi: float, step: float) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def grid_search(
    pairs: Sequence[LabeledPair], base: market_match.Thresholds, min_precision: float,
) -> tuple[market_match.Thresholds, Metrics] | None:
    """La combinación de umbrales de imagen/nombre (y de imagen sola) con más
    recall que cumple `min_precision`. A igual recall gana la de más precisión.
    Los vetos quedan como están: son el piso de cordura, no se calibran."""
    best: tuple[market_match.Thresholds, Metrics] | None = None
    for image in _frange(0.40, 0.80, 0.02):
        for name in _frange(0.30, 0.90, 0.05):
            for strong in _frange(max(image, 0.60), 0.95, 0.05):
                thr = replace(base, image=image, name=name, image_strong=strong)
                m = evaluate(pairs, thr)
                if m.precision is None or m.precision < min_precision:
                    continue
                key = (m.recall or 0.0, m.precision)
                if best is None or key > ((best[1].recall or 0.0), best[1].precision or 0.0):
                    best = (thr, m)
    return best


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f} %"


def _print_metrics(title: str, thr: market_match.Thresholds, m: Metrics) -> None:
    print(f"\n{title}")
    print(f"  umbrales: imagen≥{thr.image:.2f} y nombre≥{thr.name:.2f} | imagen sola≥{thr.image_strong:.2f}"
          f" | vetos imagen<{thr.image_veto:.2f} nombre<{thr.name_veto:.2f}")
    print(f"  precisión {_fmt(m.precision)} · recall {_fmt(m.recall)}"
          f"  (VP {m.tp} · FP {m.fp} · FN {m.fn} · VN {m.tn})")
    print(f"  positivos en banda ambigua (los podría rescatar el juez): {m.positives_in_ambiguous}")
    print(f"  productos con al menos un match: {m.products_with_match} de {m.products_labeled}")


def cmd_evaluate(args: argparse.Namespace) -> int:
    with open(args.csv, newline="", encoding="utf-8") as fh:
        pairs = load_labeled(csv.DictReader(fh))
    if not pairs:
        print("El CSV no tiene filas etiquetadas (columna same_product).")
        return 2
    base = market_match.Thresholds.from_runtime()
    thr = replace(
        base,
        image=args.image if args.image is not None else base.image,
        name=args.name if args.name is not None else base.name,
        image_strong=args.image_strong if args.image_strong is not None else base.image_strong,
    )
    m = evaluate(pairs, thr)
    print(f"{len(pairs)} pares etiquetados de {m.products_labeled} productos "
          f"({sum(p.same_product for p in pairs)} positivos).")
    _print_metrics("Umbrales evaluados:", thr, m)
    if args.grid:
        found = grid_search(pairs, thr, args.min_precision)
        if found is None:
            print(f"\nNinguna combinación llega a precisión {args.min_precision:.0%}.")
        else:
            _print_metrics(f"Mejor recall con precisión ≥ {args.min_precision:.0%}:", *found)
            print("  → cargar en el dashboard: pm_image_threshold, pm_name_threshold, pm_image_strong")
    ok = (m.precision or 0.0) >= args.min_precision and (m.recall or 0.0) >= args.min_recall
    print(f"\nCriterio (precisión ≥ {args.min_precision:.0%}, recall ≥ {args.min_recall:.0%}): "
          f"{'CUMPLE' if ok else 'NO CUMPLE'}")
    return 0 if ok else 1


async def _export(sample: int, seed: int, out: Path) -> int:
    """Corre búsqueda + puntaje como el job, sin juez y sin guardar snapshots."""
    from app import runtime
    from app.ingest import meli
    from app.pricing import match_feedback, price_monitor
    from app.pricing.market_ml import BudgetExhausted, MlMarket
    from app.vendure import catalog as vendure_catalog

    if not meli.enabled():
        print("MELI_CLIENT_ID / MELI_CLIENT_SECRET no configurados.")
        return 2
    await price_monitor._ensure_clip_index()
    products = [p for p in await vendure_catalog.get_catalog() if p.enabled and market_match.indexed(p)]
    random.Random(seed).shuffle(products)
    products = products[:sample]
    thr = market_match.Thresholds.from_runtime()
    # Lo que una persona ya descartó con "No es el mismo" sale etiquetado 0.
    negatives = match_feedback.feedback_pairs()
    rows = 0
    async with MlMarket(budget=int(runtime.get("pm_ml_daily_budget"))) as ml, \
            open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, product in enumerate(products, 1):
            try:
                candidates = await ml.search(market_match.search_query(product.name))
                if not candidates and (shorter := market_match.fallback_query(product.name)):
                    candidates = await ml.search(shorter)
            except BudgetExhausted:
                print("Budget ML del día agotado: corto acá.")
                break
            except meli.MeliError as exc:
                print(f"  {product.id}: ML falló ({exc}), sigo")
                continue
            for d in await market_match.score_candidates(product, candidates, thr):
                writer.writerow(export_row(
                    product, d, known_negative=(product.id, d.candidate.id) in negatives))
                rows += 1
            print(f"  {i}/{len(products)} {product.id}: {len(candidates)} fichas")
    print(f"\n{rows} pares en {out}. Completá la columna same_product y corré `evaluate`. "
          f"Requests ML usados: {ml.requests_used}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pricing.calibrate_market_match")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="arma el CSV a etiquetar (usa ML y CLIP: correr en prod)")
    ex.add_argument("--sample", type=int, default=100)
    ex.add_argument("--seed", type=int, default=7)
    ex.add_argument("--out", type=Path, default=Path("market_match_pairs.csv"))

    ev = sub.add_parser("evaluate", help="mide precisión/recall desde un CSV etiquetado (offline)")
    ev.add_argument("csv", type=Path)
    ev.add_argument("--image", type=float, default=None, help="override de pm_image_threshold")
    ev.add_argument("--name", type=float, default=None, help="override de pm_name_threshold")
    ev.add_argument("--image-strong", type=float, default=None, help="override de pm_image_strong")
    ev.add_argument("--min-precision", type=float, default=0.90)
    ev.add_argument("--min-recall", type=float, default=0.60)
    ev.add_argument("--grid", action="store_true", help="buscar la mejor combinación de umbrales")

    args = parser.parse_args(argv)
    if args.cmd == "export":
        return asyncio.run(_export(args.sample, args.seed, args.out))
    return cmd_evaluate(args)


if __name__ == "__main__":
    sys.exit(main())
