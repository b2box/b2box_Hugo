"""Mide qué proveedor de visión acierta más contra el catálogo REAL.

`/app/vision-compare` sirve para mirar un link; esto sirve para decidir. Corre
los dos proveedores sobre la misma muestra y mide las DOS cosas que importan,
que son independientes:

  positivos — el producto correcto está en la lista corta. ¿Lo elige?
  negativos — le sacamos el correcto de la lista a propósito. ¿Dice "ninguno"?

El segundo es el que decide. Un modelo que acierta el 95% de los positivos pero
nunca dice "ninguno" te devuelve el bug original: proponer cualquier producto
cuando el que el cliente pidió no lo tenemos. Y como la mayoría de las consultas
del app son de productos que NO tenemos, ese caso es el más frecuente.

Uso (necesita VENDURE_API_URL, VENDURE_CHANNEL_TOKEN y las API keys):

    python -m app.dedup.calibrate_vision --sample 40
    python -m app.dedup.calibrate_vision --sample 40 --providers anthropic

Costo: 2 llamadas por producto y por proveedor. Con --sample 40 son 80 llamadas
por proveedor — unos pocos dólares en los modelos grandes. Empezá chico.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from app.dedup import catalog_index, image_embed, vision_rerank
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureProduct


async def _cases(sample: int, seed: int) -> list[tuple[VendureProduct, str]]:
    """(producto, foto held-out). La featured queda en el índice, esta no."""
    products = [p for p in await vendure_catalog.get_catalog() if p.enabled]
    out = [
        (p, extra[0])
        for p in products
        if p.featured_image_url
        for extra in [[u for u in (p.image_urls or []) if u and u != p.featured_image_url]]
        if extra
    ]
    random.Random(seed).shuffle(out)
    return out[:sample]


async def _shortlist(url: str, topk: int) -> list[VendureProduct]:
    vec = await image_embed.embed_url(url)
    if vec is None:
        return []
    return [p for p, _score, _img in catalog_index.search(vec, top_k=topk)]


async def main(sample: int, seed: int, providers: list[str]) -> None:
    if not image_embed.available():
        print("CLIP no disponible: revisá EMBED_MODEL_PATH y EMBED_ENABLED.")
        return

    usable = [p for p in providers if vision_rerank.available(p)]
    if not usable:
        print(f"Ningún proveedor usable de {providers}: faltan las API keys.")
        return

    settings = catalog_index.get_settings()
    original = settings.embed_images_per_product
    settings.embed_images_per_product = 1  # la foto de query tiene que quedar afuera
    print("Construyendo el índice…")
    try:
        status = await catalog_index.build(force=True)
    finally:
        settings.embed_images_per_product = original
    if not status["ready"]:
        print(f"Índice vacío: {status['last_error']}")
        return
    print(f"  {status['vectors']} vectores de {status['products']} productos")

    cases = await _cases(sample, seed)
    topk = settings.vision_topk
    print(f"Midiendo {len(cases)} productos × 2 casos × {len(usable)} proveedores…\n")

    # {proveedor: [aciertos_pos, total_pos, dijo_ninguno_neg, total_neg, ms]}
    tally: dict[str, list[float]] = {p: [0, 0, 0, 0, 0.0] for p in usable}
    for product, photo in cases:
        shortlist = await _shortlist(photo, topk)
        if not any(p.id == product.id for p in shortlist):
            # CLIP no lo trajo: no hay nada que rerankear en el caso positivo, y
            # el negativo sería idéntico al positivo. Se descarta el par entero.
            continue
        sin_correcto = [p for p in shortlist if p.id != product.id]

        for provider in usable:
            pos = await vision_rerank.pick_match(
                [photo], shortlist, title=product.name, provider=provider
            )
            tally[provider][1] += 1
            if pos is not None:
                tally[provider][4] += pos.elapsed_ms
                if pos.product is not None and pos.product.id == product.id:
                    tally[provider][0] += 1

            neg = await vision_rerank.pick_match(
                [photo], sin_correcto, title=product.name, provider=provider
            )
            tally[provider][3] += 1
            if neg is not None:
                tally[provider][4] += neg.elapsed_ms
                if neg.product is None:
                    tally[provider][2] += 1

    print("=== resultados ===")
    for provider, (ok, n_pos, none_ok, n_neg, ms) in tally.items():
        if not n_pos:
            print(f"\n  {provider}: sin casos usables")
            continue
        calls = n_pos + n_neg
        print(f"\n  {provider}")
        print(f"    acierta el correcto      {ok}/{n_pos} = {ok / n_pos * 100:.1f}%")
        print(f"    dice 'ninguno' cuando no {none_ok}/{n_neg} = {none_ok / n_neg * 100:.1f}%")
        print(f"    latencia media           {ms / calls / 1000:.1f} s")

    print(
        "\nEl segundo número es el que decide: si es bajo, el modelo inventa un "
        "match cuando no lo tenemos — que es el bug que estamos arreglando."
    )


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=40, help="productos a medir")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--providers",
        default=",".join(vision_rerank.PROVIDERS),
        help="coma-separados: anthropic,openai",
    )
    args = ap.parse_args()
    asyncio.run(main(args.sample, args.seed, [p.strip() for p in args.providers.split(",")]))


if __name__ == "__main__":
    _cli()
