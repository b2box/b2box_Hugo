"""Chequeo de medidas, cantidad y capacidad entre NUESTRO producto y una
publicación de ML. Funciones puras: sin red, sin DB, sin settings.

CLIP y el nombre no distinguen un pack x1 de un pack x6, ni una botella de 500 ml
de una de 1 litro (`token_set_ratio` da ~100 cuando un título es subconjunto del
otro). Nico pidió que el semáforo cuente SOLO lo idéntico, así que antes de
aceptar una publicación como IGUAL se comparan:

  * cantidad por pack ("x3", "pack de 6", "set 12", "4 unidades");
  * capacidad ("500 ml" contra "1 L"; "1 litro" y "1000 ml" son lo mismo);
  * medidas ("30x40 cm") contra las de Vendure (largo/ancho/alto del producto,
    ±`dim_tol` % por lado) o, si Vendure no las tiene, contra las del nombre;
  * peso, ±`weight_tol` %.

Reglas para no inventar diferencias:
  * una medida solo choca con otra de su misma clase y con valores comparables;
  * una publicación sin medidas no genera choque (NO se supone nada);
  * las medidas de la CAJA (`box*`) se guardan para mostrarlas, pero no se usan
    para decidir: una caja puede traer varias unidades.
La única asimetría deliberada es la cantidad: un pack explícito contra un
título sin cantidad cuenta como choque (el pedido de Nico: "pack distinto =
similar"; sin número se supone una unidad).
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

DIFF_QUANTITY = "cantidad"
DIFF_CAPACITY = "capacidad"
DIFF_SIZE = "medida"
DIFF_WEIGHT = "peso"

# Capacidades que se consideran la misma si difieren menos que esto.
_CAPACITY_TOL = 0.03

_NUM = r"(\d{1,4}(?:[.,]\d{1,3})?)"


def _norm(text: str) -> str:
    """Minúsculas, sin acentos, ×→x. Los espacios se conservan."""
    t = unicodedata.normalize("NFKD", (text or "").lower().replace("×", "x"))
    return "".join(c for c in t if not unicodedata.combining(c))


def _num(raw: str) -> float | None:
    """'1,5' → 1.5; '1.000' → 1000 (punto de miles); '12' → 12."""
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        return float(raw.replace(".", ""))
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


# ─── Extracción ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Measures:
    quantity: int | None = None
    capacity_ml: tuple[float, ...] = ()
    # Grupos de lados en cm: ((30, 40),) para "30x40 cm"; ((60,),) para "60 cm".
    dims_cm: tuple[tuple[float, ...], ...] = ()
    weight_kg: tuple[float, ...] = ()

    def as_dict(self) -> dict:
        return {k: v for k, v in {
            "quantity": self.quantity,
            "capacity_ml": list(self.capacity_ml) or None,
            "dims_cm": [list(g) for g in self.dims_cm] or None,
            "weight_kg": list(self.weight_kg) or None,
        }.items() if v}


_QTY_KEYWORD = re.compile(
    r"\b(?:pack|set|kit|combo|lote|juego|caja)\s*(?:de|x)?\s*(\d{1,3})\b(?!\s*(?:en\s*(?:1|uno)|x\s*1\b))")
_QTY_X = re.compile(r"(?:^|[\s(\[\-/,])x\s?(\d{1,3})\b")
_QTY_UNITS = re.compile(
    r"(?<![\d.,])(\d{1,3})\s*(?:unidades|unidad|unid|uds|ud|un|u|piezas|pzas|pzs|pcs|pc)\b(?!\s*/)")
_QTY_AFTER_X_UNITS = re.compile(
    r"\s*(?:cm|mm|ml|cc|l\b|lt|lts|litro|kg|g\b|gr|w\b|v\b|mah|gb|tb|mt|\"|pulg)")


def extract_quantity(text: str) -> int | None:
    """Cantidad por pack declarada en el título, o None si no dice."""
    t = _norm(text)
    if re.search(r"\bdocena\b", t):
        return 12
    m = _QTY_KEYWORD.search(t)
    if m:
        return int(m.group(1))
    for m in _QTY_X.finditer(t):
        before = t[:m.start()].rstrip()
        if before[-1:].isdigit():          # "30 x 40": es una medida, no un pack
            continue
        if _QTY_AFTER_X_UNITS.match(t, m.end()):  # "x 500 ml": capacidad
            continue
        return int(m.group(1))
    m = _QTY_UNITS.search(t)
    return int(m.group(1)) if m else None


_CAPACITY = re.compile(
    rf"(?<![\d.,]){_NUM}\s*(ml|cc|cm3|litros|litro|lts|lt|l)(?![a-z0-9])")


def extract_capacity_ml(text: str) -> tuple[float, ...]:
    out: list[float] = []
    for m in _CAPACITY.finditer(_norm(text)):
        value = _num(m.group(1))
        if value is None or value <= 0:
            continue
        out.append(value * (1000.0 if m.group(2).startswith(("l",)) else 1.0))
    return tuple(out)


_UNIT_PATTERN = r"(?:\s*(cm|cms|mm|mts|metros|metro|m)(?![a-z0-9²]))?"
_DIMS = re.compile(
    rf"(?<![a-z\d.,]){_NUM}{_UNIT_PATTERN}\s*[x*]\s*{_NUM}{_UNIT_PATTERN}"
    rf"(?:\s*[x*]\s*{_NUM}{_UNIT_PATTERN})?")
_SINGLE_DIM = re.compile(rf"(?<![\d.,x*]){_NUM}\s*(cm|cms|mm|mts|metros|metro|m)(?![a-z0-9²])")

_TO_CM = {"cm": 1.0, "cms": 1.0, "mm": 0.1, "m": 100.0, "mts": 100.0, "metro": 100.0, "metros": 100.0}


def extract_dims_cm(text: str) -> tuple[tuple[float, ...], ...]:
    t = _norm(text)
    groups: list[tuple[float, ...]] = []
    spans: list[tuple[int, int]] = []
    for m in _DIMS.finditer(t):
        values = [m.group(i) for i in (1, 3, 5) if m.group(i)]
        units = [u for u in (m.group(2), m.group(4), m.group(6)) if u]
        nums = [_num(v) for v in values]
        if any(n is None or n <= 0 for n in nums):
            continue
        if units:
            factor = _TO_CM[units[-1]]
            cm = tuple(round(n * factor, 2) for n in nums)  # type: ignore[operator]
        elif all(3 <= n <= 400 for n in nums):             # "40x60" sin unidad: cm
            cm = tuple(nums)  # type: ignore[arg-type]
        else:
            continue
        groups.append(cm)
        spans.append(m.span())
    for m in _SINGLE_DIM.finditer(t):
        if any(a <= m.start() < b for a, b in spans):
            continue
        n = _num(m.group(1))
        if n and n > 0:
            groups.append((round(n * _TO_CM[m.group(2)], 2),))
    return tuple(groups)


_WEIGHT = re.compile(rf"(?<![\d.,]){_NUM}\s*(kgs|kg|kilos|kilo|gramos|gramo|grs|gr|g)(?![a-z0-9])")


def extract_weight_kg(text: str) -> tuple[float, ...]:
    out: list[float] = []
    for m in _WEIGHT.finditer(_norm(text)):
        value = _num(m.group(1))
        if value is None or value <= 0:
            continue
        out.append(value if m.group(2).startswith(("kg", "kilo")) else value / 1000.0)
    return tuple(out)


def extract(text: str) -> Measures:
    return Measures(
        quantity=extract_quantity(text),
        capacity_ml=extract_capacity_ml(text),
        dims_cm=extract_dims_cm(text),
        weight_kg=extract_weight_kg(text),
    )


def measures_from_attributes(attrs: Mapping[str, str] | None) -> Measures:
    """Medidas que trae una ficha de la API en sus atributos ("30 cm", "1,2 kg").
    Mejor esfuerzo: lo que no se entiende se ignora."""
    dims: list[tuple[float, ...]] = []
    weights: list[float] = []
    for key, value in (attrs or {}).items():
        k = key.upper()
        if k.endswith(("LENGTH", "WIDTH", "HEIGHT")):
            dims.extend(extract_dims_cm(value))
        elif k.endswith("WEIGHT"):
            weights.extend(extract_weight_kg(value))
    return Measures(dims_cm=tuple(dims), weight_kg=tuple(weights))


# ─── Lo nuestro ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OurSpecs:
    """Medidas de la variante de Vendure con la que se compara el precio.
    cm y kg (las unidades del custom field)."""
    length: float | None = None
    width: float | None = None
    height: float | None = None
    weight: float | None = None
    box_length: float | None = None
    box_width: float | None = None
    box_height: float | None = None
    box_weight: float | None = None

    @property
    def dims_cm(self) -> tuple[float, ...]:
        return tuple(v for v in (self.length, self.width, self.height) if v and v > 0)

    @classmethod
    def from_dict(cls, data: Mapping) -> "OurSpecs":
        """Inversa de `as_dict`; ignora claves que no son medidas."""
        return cls(**{k: float(v) for k, v in data.items()
                      if k in cls.__dataclass_fields__ and isinstance(v, (int, float))})

    def as_dict(self) -> dict:
        return {k: v for k, v in (
            ("length", self.length), ("width", self.width), ("height", self.height),
            ("weight", self.weight), ("box_length", self.box_length),
            ("box_width", self.box_width), ("box_height", self.box_height),
            ("box_weight", self.box_weight)) if v}


_SPEC_FIELDS = {"length": "length", "width": "width", "height": "height", "weight": "weight",
                "boxLength": "box_length", "boxWidth": "box_width",
                "boxHeight": "box_height", "boxWeight": "box_weight"}


def our_specs_from_custom_fields(custom: Mapping | None) -> OurSpecs | None:
    """customFields de la variante de Vendure → OurSpecs, o None si no hay nada."""
    if not isinstance(custom, Mapping):
        return None
    values: dict[str, float] = {}
    for src, dst in _SPEC_FIELDS.items():
        v = custom.get(src)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            values[dst] = float(v)
    return OurSpecs(**values) if values else None


# ─── Comparación ──────────────────────────────────────────────────


def _within(a: float, b: float, tol: float) -> bool:
    return b > 0 and abs(a - b) / b <= tol


def dims_conflict(theirs: tuple[float, ...], ours: tuple[float, ...], tol: float) -> bool:
    """¿Los lados de la publicación NO caben en los nuestros? Se ordenan de
    mayor a menor (no sabemos qué lado es el largo) y se prueba cada subconjunto
    de nuestros lados del mismo tamaño: choca solo si NINGUNO encaja. Con menos
    lados nuestros que los de la publicación no hay con qué comparar."""
    k = len(theirs)
    if k == 0 or len(ours) < k:
        return False
    t_sorted = sorted(theirs, reverse=True)
    for combo in itertools.combinations(sorted(ours, reverse=True), k):
        if all(_within(t, o, tol) for t, o in zip(t_sorted, combo)):
            return False
    return True


def _capacity_conflict(ours: Iterable[float], theirs: Iterable[float]) -> bool:
    ours, theirs = list(ours), list(theirs)
    if not ours or not theirs:
        return False
    return not any(_within(a, b, _CAPACITY_TOL) for a in ours for b in theirs)


def differences(
    our_name: str,
    our_specs: OurSpecs | None,
    their_title: str,
    their_attrs: Mapping[str, str] | None = None,
    *,
    dim_tol_pct: float = 10.0,
    weight_tol_pct: float = 15.0,
) -> list[str]:
    """Qué cambia entre nuestro producto y la publicación, con vocabulario
    cerrado: cantidad, capacidad, medida, peso. Lista vacía = no se encontró
    ninguna diferencia (que no es lo mismo que "se verificó todo")."""
    ours = extract(our_name)
    theirs = extract(their_title)
    from_attrs = measures_from_attributes(their_attrs)
    diffs: list[str] = []

    if (ours.quantity or 1) != (theirs.quantity or 1):
        diffs.append(DIFF_QUANTITY)
    if _capacity_conflict(ours.capacity_ml, theirs.capacity_ml):
        diffs.append(DIFF_CAPACITY)

    dim_tol = max(0.0, dim_tol_pct) / 100.0
    our_dims = our_specs.dims_cm if our_specs and our_specs.dims_cm else (
        max(ours.dims_cm, key=len) if ours.dims_cm else ())
    their_groups = [g for g in (*theirs.dims_cm, *from_attrs.dims_cm) if g]
    if our_dims and their_groups:
        biggest = max(their_groups, key=len)
        if dims_conflict(biggest, our_dims, dim_tol):
            diffs.append(DIFF_SIZE)

    weight_tol = max(0.0, weight_tol_pct) / 100.0
    our_weights = (our_specs.weight,) if our_specs and our_specs.weight else ours.weight_kg
    their_weights = (*theirs.weight_kg, *from_attrs.weight_kg)
    if our_weights and their_weights and not any(
            _within(w, o, weight_tol) for w in their_weights for o in our_weights):
        diffs.append(DIFF_WEIGHT)
    return diffs
