"""Calibra los umbrales de /app/lookup contra el catálogo REAL.

Los defaults de `embed_match_threshold` / `embed_suggest_threshold` salieron de un
set público de fotos de e-commerce. El catálogo de B2Box es más homogéneo (todo
gadget de importación, mismo estilo de foto), así que los impostores puntúan más
alto y los umbrales hay que moverlos. Este script mide eso.

Cómo funciona: indexa SOLO la foto featured de cada producto y después usa como
query la segunda foto — una que el índice no vio. El positivo es el propio
producto; el impostor es el mejor de los demás. Con esas dos distribuciones
propone umbrales.

Ojo con leer el recall como si fuera el de producción: la segunda foto de nuestras
fichas suele ser una lámina de detalle o una escena de uso, no otra toma del
producto (medido: en el 62% de los casos se parece menos a su propio producto que
a uno ajeno). Contra la foto limpia de un marketplace el recall real es mejor. La
tasa de falsos positivos, en cambio, sí es representativa: sale del catálogo real.

Uso (necesita VENDURE_API_URL y VENDURE_CHANNEL_TOKEN en el entorno):

    python -m app.dedup.calibrate_embed              # 150 productos de muestra
    python -m app.dedup.calibrate_embed --sample 400
"""

from __future__ import annotations

import argparse
import asyncio
import random

import numpy as np

from app.dedup import catalog_index, image_embed
from app.vendure import catalog as vendure_catalog


def _pct(arr: np.ndarray, q: float) -> float:
    return float(np.percentile(arr, q)) if arr.size else float("nan")


async def _queries(sample: int, seed: int) -> list[tuple[str, str]]:
    """(product_id, url) held-out: una foto que NO es la featured, así el índice
    (que solo tiene featureds) nunca la vio. Sin segunda foto el producto no sirve
    como query: matchearía consigo mismo con score 1.0 y no mediría nada."""
    products = [p for p in await vendure_catalog.get_catalog() if p.enabled]
    out: list[tuple[str, str]] = []
    for p in products:
        extra = [u for u in (p.image_urls or []) if u and u != p.featured_image_url]
        if extra and p.featured_image_url:
            out.append((p.id, extra[0]))
    random.Random(seed).shuffle(out)
    return out[:sample]


async def main(sample: int, seed: int) -> None:
    if not image_embed.available():
        print("CLIP no disponible: revisá EMBED_MODEL_PATH y EMBED_ENABLED.")
        return

    # Índice de solo featureds: si indexara 2 fotos por producto, la foto que uso
    # como query estaría adentro y el positivo saldría 1.0 — mediría nada.
    settings = catalog_index.get_settings()
    original_per_product = settings.embed_images_per_product
    settings.embed_images_per_product = 1
    print("Construyendo el índice (puede tardar varios minutos la primera vez)…")
    try:
        status = await catalog_index.build(force=True)
    finally:
        settings.embed_images_per_product = original_per_product
    print(f"  vectores={status['vectors']} productos={status['products']} "
          f"centrado={status['centered']}")
    if not status["ready"]:
        print(f"  índice vacío: {status['last_error']}")
        return

    targets = await _queries(sample, seed)
    print(f"Midiendo {len(targets)} queries…")

    positives: list[float] = []
    impostors: list[float] = []
    top1_ok = 0
    used = 0

    for product_id, url in targets:
        vec = await image_embed.embed_url(url)
        if vec is None:
            continue
        hits = catalog_index.search(vec, top_k=20)
        if not hits:
            continue
        used += 1
        mine = [s for p, s, _ in hits if p.id == product_id]
        others = [s for p, s, _ in hits if p.id != product_id]
        if mine:
            positives.append(max(mine))
        if others:
            impostors.append(max(others))
        if hits[0][0].id == product_id:
            top1_ok += 1

    if used < 10 or not positives or not impostors:
        print(f"Muestra insuficiente (queries usables={used}). Subí --sample.")
        return

    pos = np.array(positives)
    imp = np.array(impostors)

    print(f"\n=== {used} queries — top1 correcto: {top1_ok / used * 100:.1f}% ===")
    print("                     p10    p50    p90    p99")
    print(f"  producto correcto  {_pct(pos,10):.3f}  {_pct(pos,50):.3f}  "
          f"{_pct(pos,90):.3f}  {_pct(pos,99):.3f}")
    print(f"  mejor impostor     {_pct(imp,10):.3f}  {_pct(imp,50):.3f}  "
          f"{_pct(imp,90):.3f}  {_pct(imp,99):.3f}")

    # match: afirmar "lo tenemos" es lo caro de equivocar → lo atamos a la cola
    # alta de los impostores (p99), no a la mediana.
    match = round(max(_pct(imp, 99), _pct(pos, 10)), 2)
    # suggest: sugerir es barato (el cliente puede decir que no) → alcanza con
    # dejar afuera al impostor típico.
    suggest = round(_pct(imp, 90), 2)
    rescue = round(_pct(imp, 50), 2)

    print("\n=== umbrales sugeridos ===")
    print(f"  embed_match_threshold        = {match}")
    print(f"  embed_suggest_threshold      = {suggest}")
    print(f"  embed_name_rescue_image_floor= {rescue}")
    print(f"\n  con match={match}: recupera {float((pos >= match).mean())*100:.1f}% "
          f"de los productos que SÍ están, y deja pasar "
          f"{float((imp >= match).mean())*100:.1f}% de falsos positivos.")
    print("\nCargarlos en el dashboard (grupo 'app') o como default en config.py.")
    print("El recall de acá es un piso — ver el docstring del módulo.")


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=150, help="productos a medir")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    asyncio.run(main(args.sample, args.seed))


if __name__ == "__main__":
    _cli()
