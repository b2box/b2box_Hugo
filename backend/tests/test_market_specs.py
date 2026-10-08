"""Chequeo de medidas, cantidad y capacidad (market_specs): funciones puras,
con títulos en español rioplatense como los de ML."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import market_specs as ms  # noqa: E402
from app.pricing.market_specs import OurSpecs, differences  # noqa: E402

# (nuestro título, título de ML, qué cambia). Vacío = no hay choque.
PAIRS = [
    # capacidad
    ("Botella térmica 500 ml", "Botella Térmica Acero Inox 1 Litro", ["capacidad"]),
    ("Botella térmica 500 ml", "Botella Térmica Acero 500ml Negra", []),
    ("Botella térmica 1 litro", "Botella Térmica 1000 ml Acero", []),
    ("Termo 1 L", "Termo Stanley 1,5 L", ["capacidad"]),
    ("Frasco 1.000 ml", "Frasco hermético 1 litro", []),
    ("Mochila 30 L", "Mochila 45 L Camping", ["capacidad"]),
    # cantidad / pack
    ("Organizador de cables", "Pack x6 Organizadores De Cables", ["cantidad"]),
    ("Pack x3 organizadores", "Pack x3 Organizadores Cables", []),
    ("Organizador x1", "Organizador x3", ["cantidad"]),
    ("Set de 12 vasos", "Juego de 6 vasos de vidrio", ["cantidad"]),
    ("Cuchillos 4 piezas", "Set Cuchillos 6 Piezas", ["cantidad"]),
    ("Lámpara LED", "Lámpara LED Escritorio Recargable", []),
    ("Combo 2x1 jabón", "Jabón líquido combo 2x1", []),
    ("Auricular 3 en 1", "Auricular Bluetooth 3 en 1", []),
    # medidas (título contra título)
    ("Organizador 30x40 cm", "Organizador Plegable 40 x 30 cm", []),
    ("Organizador 30x40 cm", "Organizador 40x50 cm", ["medida"]),
    ("Alfombra 40x60", "Alfombra Baño 50x80 Antideslizante", ["medida"]),
    ("Estante 120 x 60 x 75 cm", "Estante Metálico 120x60x75cm", []),
    ("Bandeja 30cm x 40cm", "Bandeja 30 x 40 cm Bambú", []),
    # sin números: no se inventa un choque
    ("Organizador de cocina", "Organizador De Cocina Apilable", []),
    ("Taza de cerámica", "Taza Cerámica Con Tapa Y Cuchara", []),
    # peso
    ("Bolsa 500 g", "Bolsa 800 g Resistente", ["peso"]),
    ("Bolsa 500 g", "Bolsa 0,5 kg", []),
]


@pytest.mark.parametrize("ours, theirs, expected", PAIRS)
def test_title_pairs(ours, theirs, expected):
    assert differences(ours, None, theirs) == expected


def test_there_are_at_least_twenty_cases():
    assert len(PAIRS) >= 20


# ─── contra las medidas de Vendure ────────────────────────────────────────

OURS = OurSpecs(length=40, width=30, height=10, weight=0.5)


def test_dims_within_tolerance_in_any_orientation():
    assert differences("Organizador", OURS, "Organizador 30 x 40 cm") == []
    assert differences("Organizador", OURS, "Organizador 41x32x10 cm") == []   # ≤ 10 % por lado


def test_dims_outside_tolerance_make_it_similar():
    assert differences("Organizador", OURS, "Organizador 50x60 cm") == ["medida"]
    assert differences("Organizador", OURS, "Organizador 30x40x20 cm") == ["medida"]


def test_tolerance_is_configurable():
    assert differences("Org", OURS, "Org 44x33 cm", dim_tol_pct=5) == ["medida"]
    assert differences("Org", OURS, "Org 44x33 cm", dim_tol_pct=12) == []


def test_a_single_side_must_match_one_of_ours():
    assert differences("Org", OURS, "Org 30 cm") == []
    assert differences("Org", OURS, "Org 100 cm") == ["medida"]


def test_publication_with_more_sides_than_we_have_is_not_compared():
    assert differences("Org", OurSpecs(length=40, width=30), "Org 40x30x10 cm") == []


def test_publication_without_measures_never_clashes():
    assert differences("Org", OURS, "Organizador De Cocina") == []


def test_weight_tolerance_is_15_percent_by_default():
    assert differences("Org", OURS, "Org 560 g") == []             # +12 %
    assert differences("Org", OURS, "Org 600 g") == ["peso"]       # +20 %
    assert differences("Org", OURS, "Org 600 g", weight_tol_pct=25) == []


def test_box_measures_are_stored_but_not_used_to_decide():
    box_only = OurSpecs(box_length=80, box_width=60, box_height=50, box_weight=12)
    assert box_only.dims_cm == ()
    assert differences("Org", box_only, "Org 10x5 cm 100 g") == []
    assert box_only.as_dict()["box_weight"] == 12


def test_our_dims_fall_back_to_the_ones_in_our_name():
    assert differences("Organizador Doble 40x30 Blanco", None, "Organizador 40x30 cm") == []
    assert differences("Organizador Doble 40x30 Blanco", None, "Organizador 20x15 cm") == ["medida"]


def test_attributes_of_a_catalog_card_are_compared_too():
    attrs = {"LENGTH": "80 cm", "WIDTH": "60 cm"}
    assert differences("Org", OURS, "Organizador", attrs) == ["medida"]
    assert differences("Org", OURS, "Organizador", {"LENGTH": "40 cm", "WIDTH": "30 cm"}) == []


# ─── extracción ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, qty", [
    ("Pack x6 organizadores", 6), ("Pack de 6 unidades", 6), ("Set 12 vasos", 12),
    ("Kit x3", 3), ("Taza 300ml x2", 2), ("Docena de huevos", 12), ("Vaso 4 piezas", 4),
    ("Combo 2x1 oferta", None), ("Kit 3 en 1 cepillo", None), ("Auricular 3 en 1", None),
    ("Alfombra 30 x 40", None), ("Botella x 500 ml", None), ("Organizador", None),
])
def test_quantity(text, qty):
    assert ms.extract_quantity(text) == qty


def test_units_are_normalized():
    assert ms.extract_capacity_ml("Botella 1,5 L") == (1500.0,)
    assert ms.extract_capacity_ml("Vaso 350cc") == (350.0,)
    assert ms.extract_dims_cm("Cinta 1,5 m") == ((150.0,),)
    assert ms.extract_dims_cm("Soporte 15 mm") == ((1.5,),)
    assert ms.extract_weight_kg("Harina 1 kg") == (1.0,)
    assert ms.extract_weight_kg("Sal 500 gr") == (0.5,)


def test_resolutions_and_model_codes_are_not_measures():
    assert ms.extract_dims_cm("Notebook 1920x1080 15.6") == ()
    assert ms.extract_dims_cm("Cuaderno A4 x 3") == ()


def test_our_specs_from_vendure_custom_fields():
    assert ms.our_specs_from_custom_fields(None) is None
    assert ms.our_specs_from_custom_fields({"length": None, "width": 0, "boxWeight": "x"}) is None
    specs = ms.our_specs_from_custom_fields({"length": 40.0, "width": 30, "weight": 0.5, "boxLength": 80})
    assert specs.dims_cm == (40.0, 30.0) and specs.box_length == 80
    assert OurSpecs.from_dict(specs.as_dict()) == specs


# ─── un "x 3" después de un código de modelo es un pack (BUG-L3) ──────────


@pytest.mark.parametrize("text, qty", [
    ("Foco LED 9W E27 x 3", 3), ("Zapatilla talle 42 x 2", 2), ("Cuaderno A4 x 3", 3),
    ("Batería AA x 4", 4), ("Lámpara E14 x 6 unidades", 6), ("Filtro N95 x 10", 10),
    # esto NO es un pack: es una medida
    ("Alfombra 30 x 40", None), ("Mesa 120 x 60 x 75 cm", None), ("Cinta 30cm x 40cm", None),
    ("Estante 15 x 15 x 15", None), ("Notebook 1920x1080", None),
])
def test_x_after_a_model_code_is_a_pack_but_a_real_measure_is_not(text, qty):
    assert ms.extract_quantity(text) == qty


def test_pack_after_a_model_code_is_a_different_quantity():
    assert differences("Foco LED 9W E27", None, "Foco LED 9W E27 x 3") == ["cantidad"]
    assert differences("Foco LED 9W E27 x 3", None, "Foco LED 9W E27 x 3") == []


# ─── atributos de una ficha: solo del producto, sin depender del orden (BUG-L8) ──


def test_package_attributes_are_the_shipping_box_and_never_compared():
    ours = OurSpecs(length=20, width=15, height=5, weight=0.4)
    attrs = {"PACKAGE_LENGTH": "30 cm", "PACKAGE_WIDTH": "25 cm", "PACKAGE_HEIGHT": "10 cm",
             "PACKAGE_WEIGHT": "600 g", "SELLER_PACKAGE_WEIGHT": "900 g",
             "LENGTH": "20 cm", "WEIGHT": "400 g"}
    assert differences("Producto", ours, "Producto", attrs) == []
    assert differences("Producto", ours, "Producto", dict(reversed(attrs.items()))) == []
    m = ms.measures_from_attributes(attrs)
    assert m.dims_cm == ((20.0,),) and m.weight_kg == (0.4,)


def test_the_catalog_card_dims_are_one_group_whatever_the_order():
    a = {"LENGTH": "80 cm", "WIDTH": "60 cm", "HEIGHT": "10 cm"}
    b = dict(reversed(a.items()))
    assert ms.measures_from_attributes(a).dims_cm == ms.measures_from_attributes(b).dims_cm == ((10.0, 60.0, 80.0),)
    ours = OurSpecs(length=40, width=30, height=10)
    assert differences("P", ours, "P", a) == differences("P", ours, "P", b) == ["medida"]


def test_title_and_catalog_card_dims_are_both_checked():
    ours = OurSpecs(length=40, width=30, height=10)
    attrs = {"LENGTH": "80 cm", "WIDTH": "60 cm"}
    assert differences("P", ours, "P 40x30 cm", attrs) == ["medida"]          # el título coincide, la ficha no
    assert differences("P", ours, "P 80x60 cm", {"LENGTH": "40 cm", "WIDTH": "30 cm"}) == ["medida"]
    assert differences("P", ours, "P 40x30 cm", {"LENGTH": "40 cm", "WIDTH": "30 cm"}) == []


def test_the_choice_between_title_groups_is_deterministic():
    ours = OurSpecs(length=40, width=30, height=10)
    one = differences("P", ours, "P 30x40 cm con base de 12 x 12 cm")
    two = differences("P", ours, "P con base de 12 x 12 cm de 30x40 cm")
    assert one == two


def test_bare_x_values_without_units_keep_working():
    assert ms.extract_dims_cm("Alfombra 40x60") == ((40.0, 60.0),)


# ─── tope de cordura de lo que dice Vendure ───────────────────────────────


def test_an_absurd_vendure_dimension_does_not_decide_and_is_noted():
    mm_as_cm = OurSpecs(length=400, width=300, height=100)          # mm cargados como cm
    result = ms.check("Org", mm_as_cm, "Org 40x30x10 cm")
    assert result.differences == [] and result.notes == [ms.NOTE_DOUBTFUL_SIZE]
    assert differences("Org", mm_as_cm, "Org 40x30x10 cm") == []


def test_a_vendure_dimension_far_beyond_the_publication_is_doubtful_not_a_difference():
    result = ms.check("Org", OurSpecs(length=250, width=200), "Org 20x15 cm")      # >10x
    assert result.differences == [] and result.notes == [ms.NOTE_DOUBTFUL_SIZE]
    tiny = ms.check("Org", OurSpecs(length=2, width=1.5), "Org 40x30 cm")            # <0,1x
    assert tiny.differences == [] and tiny.notes == [ms.NOTE_DOUBTFUL_SIZE]


def test_a_plausible_mismatch_still_decides_without_a_note():
    result = ms.check("Org", OurSpecs(length=40, width=30), "Org 80x60 cm")          # 2x: es otro producto
    assert result.differences == ["medida"] and result.notes == []


def test_an_absurd_vendure_weight_does_not_decide():
    grams_as_kg = OurSpecs(length=40, width=30, weight=500)         # 500 "kg" de un producto de 500 g
    result = ms.check("Org", grams_as_kg, "Org 500 g")
    assert result.differences == [] and result.notes == [ms.NOTE_DOUBTFUL_WEIGHT]
    assert ms.check("Org", OurSpecs(weight=0.4), "Org 900 g").differences == ["peso"]   # 2,25x: decide


def test_the_name_measures_are_not_subject_to_the_vendure_sanity_check():
    result = ms.check("Org 400x300 cm", None, "Org 40x30 cm")       # lo dice el nombre, no Vendure
    assert result.notes == []
