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

Barre VARIANTES, no solo proveedores: cada una es proveedor:modelo:effort, así
se puede contestar "¿cuánto pierdo bajando a Sonnet?" y "¿cuánto pierdo bajando
el effort a medium?" en la misma corrida y sobre los mismos casos.

Uso (necesita VENDURE_API_URL, VENDURE_CHANNEL_TOKEN y las API keys):

    python -m app.dedup.calibrate_vision --sample 40
    python -m app.dedup.calibrate_vision --sample 40 \
        --variants anthropic:claude-sonnet-5:medium,openai:gpt-5:

Costo: 2 llamadas por producto y por variante. Con --sample 40 y 4 variantes son
320 llamadas. Empezá con --sample 10 para ver que todo corre.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from dataclasses import dataclass

from app.dedup import catalog_index, image_embed, vision_rerank
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureProduct


@dataclass
class _Score:
    """Acumulador de una variante. Las llamadas que no contestaron no cuentan
    como acierto ni como error: solo bajan la muestra útil."""

    hits: int = 0
    n_pos: int = 0
    said_none: int = 0
    n_neg: int = 0
    answered: int = 0
    _ms: float = 0.0
    _in: int = 0
    _out: int = 0

    def absorb(self, v) -> None:
        if v is None:
            return
        self.answered += 1
        self._ms += v.elapsed_ms
        self._in += v.input_tokens
        self._out += v.output_tokens

    @property
    def seconds(self) -> float:
        return self._ms / self.answered / 1000 if self.answered else 0.0

    @property
    def tok_in(self) -> float:
        return self._in / self.answered if self.answered else 0.0

    @property
    def tok_out(self) -> float:
        return self._out / self.answered if self.answered else 0.0


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


async def main(sample: int, seed: int, variants: list[vision_rerank.Options]) -> None:
    if not image_embed.available():
        print("CLIP no disponible: revisá EMBED_MODEL_PATH y EMBED_ENABLED.")
        return

    usable = [v for v in variants if vision_rerank.available(v.provider)]
    if not usable:
        print("Ninguna variante usable: faltan las API keys.")
        return
    for v in variants:
        if v not in usable:
            print(f"  salteo {v.provider}:{v.model}: sin API key")

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
    print(f"Midiendo {len(cases)} productos × 2 casos × {len(usable)} variantes…\n")

    tally: dict[vision_rerank.Options, _Score] = {v: _Score() for v in usable}
    for product, photo in cases:
        shortlist = await _shortlist(photo, topk)
        if not any(p.id == product.id for p in shortlist):
            # CLIP no lo trajo: no hay nada que rerankear en el caso positivo, y
            # el negativo sería idéntico al positivo. Se descarta el par entero.
            continue
        sin_correcto = [p for p in shortlist if p.id != product.id]

        for v in usable:
            score = tally[v]
            pos = await vision_rerank.pick_match(
                [photo], shortlist, title=product.name,
                provider=v.provider, model=v.model, effort=v.effort,
            )
            score.n_pos += 1
            score.absorb(pos)
            if pos is not None and pos.product is not None and pos.product.id == product.id:
                score.hits += 1

            neg = await vision_rerank.pick_match(
                [photo], sin_correcto, title=product.name,
                provider=v.provider, model=v.model, effort=v.effort,
            )
            score.n_neg += 1
            score.absorb(neg)
            if neg is not None and neg.product is None:
                score.said_none += 1

    print("=== resultados ===")
    print(
        f"\n  {'variante':<38} {'acierta':>8} {'dice ninguno':>13} "
        f"{'seg':>6} {'tok in':>8} {'tok out':>8}"
    )
    for v, sc in tally.items():
        name = f"{v.model} ({v.effort})" if v.provider == vision_rerank.ANTHROPIC else v.model
        if not sc.n_pos:
            print(f"  {name:<38} sin casos usables")
            continue
        print(
            f"  {name:<38} {sc.hits / sc.n_pos * 100:7.1f}% "
            f"{sc.said_none / sc.n_neg * 100:12.1f}% "
            f"{sc.seconds:6.1f} {sc.tok_in:8.0f} {sc.tok_out:8.0f}"
        )

    print(
        "\n  acierta      = el correcto estaba en la lista y lo eligió\n"
        "  dice ninguno = le sacamos el correcto y NO inventó un match\n"
        "  seg/tokens   = promedio por llamada\n"
    )
    print(
        "El segundo número es el que decide. Si es bajo, esa variante inventa un "
        "match cuando no tenemos el producto — el bug que estamos arreglando.\n"
        "Los tokens son reales (los devuelve la API): multiplicá por el precio de "
        "cada modelo para comparar costo, en vez de estimarlo."
    )


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=40, help="productos a medir")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--variants",
        default=",".join(f"{p}::" for p in vision_rerank.PROVIDERS),
        help=(
            "coma-separadas, cada una proveedor:modelo:effort. Los campos vacíos "
            "toman el valor de la config. Ej: "
            "anthropic:claude-opus-5:high,anthropic:claude-sonnet-5:medium"
        ),
    )
    args = ap.parse_args()
    variants = []
    for raw in args.variants.split(","):
        parts = (raw.strip().split(":") + ["", ""])[:3]
        variants.append(vision_rerank.options(parts[0] or None, parts[1] or None, parts[2] or None))
    asyncio.run(main(args.sample, args.seed, variants))


if __name__ == "__main__":
    _cli()
