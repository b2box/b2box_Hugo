"""Variantes de búsqueda para la API de ML (y para el buscador de la oficina).

`/products/search?q=<título completo>` no encuentra ficha cuando el título es
largo o genérico: nuestros nombres arrastran medidas, packs, colores y adjetivos
("Organizador Doble Ajustable 3 Niveles 40x30 Blanco") que vuelven la búsqueda
demasiado específica. Acá se arman, SIN IA y de forma determinista, hasta tres
consultas por producto, de la más específica a la más general:

  1. `titulo`: el título como siempre (sin códigos internos BX/PA).
  2. `corto`: sin medidas, cantidades, códigos, colores ni palabras de relleno;
     las primeras palabras con contenido.
  3. `claves`: el sustantivo principal y uno o dos atributos.

Con `pm_ml_query_variants` = 1 el plan es exactamente el de antes: el título y,
SOLO si no trajo ninguna ficha, las primeras 4 palabras (`inicio`).

Todo es texto de nuestro catálogo: las variantes no se parsean de ML.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.pricing import market_match

LABEL_TITLE = "titulo"
LABEL_SHORT = "corto"
LABEL_KEYS = "claves"
# Con una sola variante: las primeras 4 palabras, solo si el título no trajo nada.
LABEL_START = "inicio"
LABELS = (LABEL_TITLE, LABEL_SHORT, LABEL_KEYS, LABEL_START)

MAX_VARIANTS = 3
# Palabras con contenido de la variante corta, y atributos que acompañan al
# sustantivo en la de palabras clave.
SHORT_WORDS = 5
KEY_ATTRIBUTES = 2
MAX_QUERY_CHARS = 120

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _fold(word: str) -> str:
    """Minúscula y sin tildes ("Metálico" → "metalico"), solo para comparar."""
    text = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _words(text: str) -> frozenset[str]:
    return frozenset(_fold(w) for w in text.split())


# Unidades de medida sueltas ("30 x 40 cm": el número cae por tener dígitos, la unidad por estar acá).
_UNITS = _words(
    "cm cms mm m mt mts metro metros kg kgs g gr grs gramo gramos kilo kilos lt lts l litro litros "
    "ml cc w watt watts v volt volts mah gb mb tb hz pulgada pulgadas pulg oz talle talles tamano medida medidas"
)
# Cantidades y packs.
_QUANTITY = _words(
    "x pack packs par pares unidad unidades unid uds ud u pcs pc pzs pza pzas pieza piezas docena lote"
)
_COLORS = _words(
    "blanco blanca blancos blancas negro negra negros negras rojo roja rojos rojas azul azules verde verdes "
    "amarillo amarilla amarillos amarillas rosa rosas rosado rosada rosados rosadas gris grises naranja naranjas "
    "violeta violetas morado morada morados moradas lila marron marrones celeste celestes beige dorado dorada "
    "dorados doradas plateado plateada plateados plateadas turquesa bordo fucsia crema multicolor multicolores "
    "color colores black white red blue green pink grey gray yellow"
)
# Conectores y relleno de marketing: no dicen qué es el producto.
_FILLER = _words(
    "de del la el los las un una unos unas y e o para por con sin en al a que se su sus tu mi lo le es como "
    "mas muy ya nuevo nueva nuevos nuevas original originales premium profesional universal importado "
    "importada oferta super mega ultra excelente calidad ideal practico mejor gratis envio liquidacion"
)
# Modificadores que no sirven de atributo en las palabras clave.
_WEAK = _words("doble triple simple extra grande chico mini")
# Si el título empieza con esto (o con un modificador débil), el sustantivo es lo que viene después
# ("Kit de herramientas", "Mini lámpara").
_CONTAINERS = _words("kit set juego combo conjunto surtido") | _WEAK


def _is_noise(word: str) -> bool:
    folded = _fold(word)
    return (
        len(folded) < 2
        or any(c.isdigit() for c in folded)      # medidas, códigos, modelos ("40x30", "E27", "500ml")
        or folded in _UNITS or folded in _QUANTITY or folded in _COLORS or folded in _FILLER
    )


def content_words(title: str) -> list[str]:
    """Las palabras del título que dicen qué es el producto, en orden y sin repetir
    (con las mayúsculas que tenían)."""
    out: list[str] = []
    seen: set[str] = set()
    for word in _TOKEN.findall(title or ""):
        folded = _fold(word)
        if folded in seen or _is_noise(word):
            continue
        seen.add(folded)
        out.append(word)
    return out


def short_title(name: str) -> str:
    """Variante 2: las primeras palabras con contenido."""
    return " ".join(content_words(market_match.search_query(name))[:SHORT_WORDS])[:MAX_QUERY_CHARS].strip()


def keywords(name: str) -> str:
    """Variante 3: el sustantivo principal (en español va primero) y uno o dos atributos."""
    words = content_words(market_match.search_query(name))
    while len(words) > 1 and _fold(words[0]) in _CONTAINERS:
        words = words[1:]
    if not words:
        return ""
    noun, rest = words[0], [w for w in words[1:] if _fold(w) not in _WEAK]
    return " ".join([noun, *rest[:KEY_ATTRIBUTES]])[:MAX_QUERY_CHARS].strip()


def _key(query: str) -> str:
    return " ".join(_fold(query).split())


@dataclass(frozen=True, slots=True)
class QueryStep:
    label: str
    query: str
    # Solo se prueba si lo anterior no trajo NINGUNA ficha (el fallback de siempre).
    only_if_empty: bool = False


def clamp_variants(value: object) -> int:
    try:
        return max(1, min(int(value), MAX_VARIANTS))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 1


def query_plan(name: str, variants: object) -> list[QueryStep]:
    """Las búsquedas de un producto, en el orden en que se prueban. Sin repetidas.

    `variants` = 1 es el comportamiento de antes. Más de 1: título, corto y
    palabras clave (hasta ese número), sin repetir las que quedaron iguales."""
    n = clamp_variants(variants)
    title = market_match.search_query(name)
    if not title:
        return []
    if n == 1:
        steps = [QueryStep(LABEL_TITLE, title)]
        fallback = market_match.fallback_query(name)
        if fallback:
            steps.append(QueryStep(LABEL_START, fallback, only_if_empty=True))
        return steps
    steps: list[QueryStep] = []
    seen: set[str] = set()
    for label, query in ((LABEL_TITLE, title), (LABEL_SHORT, short_title(name)), (LABEL_KEYS, keywords(name))):
        if query and _key(query) not in seen:
            seen.add(_key(query))
            steps.append(QueryStep(label, query))
        if len(steps) >= n:
            break
    return steps


def query_variants(name: str, variants: object) -> list[str]:
    """Solo los textos de `query_plan` (lo que se le manda al buscador de la oficina)."""
    return [s.query for s in query_plan(name, variants)]
