"""Variantes de búsqueda para la API de ML (`pricing/market_query.py`): puras y deterministas."""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import market_match, market_query as mq  # noqa: E402

LONG = "Organizador Doble Ajustable 3 Niveles 40x30 Blanco"


def _queries(name: str, n: int) -> list[str]:
    return mq.query_variants(name, n)


def test_the_first_variant_is_the_title_as_before_without_internal_codes():
    assert _queries("BX0123 Kit de Herramientas 100 Piezas", 3)[0] == "Kit de Herramientas 100 Piezas"
    assert _queries(LONG, 3)[0] == market_match.search_query(LONG)


def test_short_title_drops_measures_quantities_colors_codes_and_filler():
    assert mq.short_title(LONG) == "Organizador Doble Ajustable Niveles"
    assert mq.short_title("Pack x6 Medias Deportivas Algodón Talle 40-44 Negras") == "Medias Deportivas Algodón"
    assert mq.short_title("Lámpara LED USB Recargable 3 Colores Luz Cálida 5W") == "Lámpara LED USB Recargable Luz"
    assert mq.short_title("Termo 500ml E27 BX99 x3 acero para el mate") == "Termo E27 acero mate"      # E27 identifica; 500ml y x3 no


def test_short_title_keeps_at_most_five_content_words_and_never_repeats_one():
    assert mq.short_title("Cable cable USB tipo reforzado trenzado largo resistente") == "Cable USB tipo reforzado trenzado"


def test_keywords_are_the_noun_and_up_to_two_attributes():
    assert mq.keywords(LONG) == "Organizador Ajustable Niveles"            # "Doble" es un modificador débil
    assert mq.keywords("Soporte Celular Auto Magnetico Universal Reforzado 360") == "Soporte Celular Auto"
    assert mq.keywords("Taza ceramica") == "Taza ceramica"


def test_a_leading_kit_or_modifier_is_not_the_noun():
    assert mq.keywords("Kit de Herramientas 100 Piezas Maletín Profesional Negro") == "Herramientas Maletín"
    assert mq.keywords("Mini Lámpara LED USB") == "Lámpara LED USB"


def test_the_plan_goes_from_specific_to_general_and_cuts_at_the_asked_number():
    plan = mq.query_plan(LONG, 3)
    assert [s.label for s in plan] == ["titulo", "corto", "claves"]
    assert [s.query for s in plan] == [
        market_match.search_query(LONG), "Organizador Doble Ajustable Niveles", "Organizador Ajustable Niveles"]
    assert not any(s.only_if_empty for s in plan)
    assert [s.label for s in mq.query_plan(LONG, 2)] == ["titulo", "corto"]


def test_variants_that_end_up_equal_are_searched_once():
    # Un título que ya es corto y limpio: las tres variantes son la misma.
    assert _queries("Taza ceramica", 3) == ["Taza ceramica"]
    # Dos iguales salvo mayúsculas y tildes: una sola.
    assert _queries("Lámpara LED", 3) == ["Lámpara LED"]
    # La corta ya es la de palabras clave: no se repite, y la tercera tampoco aparece de más.
    assert _queries("Mate de calabaza", 3) == ["Mate de calabaza", "Mate calabaza"]


def test_a_repeated_variant_makes_room_for_the_next_one():
    # El título ya es limpio: la corta sería idéntica. Con 2 variantes se prueba la de palabras clave en su lugar.
    plan = mq.query_plan("Lampara LED escritorio plegable", 2)
    assert [(s.label, s.query) for s in plan] == [
        ("titulo", "Lampara LED escritorio plegable"), ("claves", "Lampara LED escritorio")]


def test_one_variant_is_what_was_done_before_the_title_and_the_four_first_words_if_it_found_nothing():
    plan = mq.query_plan(LONG, 1)
    assert [(s.label, s.query, s.only_if_empty) for s in plan] == [
        ("titulo", market_match.search_query(LONG), False),
        ("inicio", market_match.fallback_query(LONG), True)]
    assert market_match.fallback_query(LONG) == "Organizador Doble Ajustable 3"
    # sin nada más corto que probar no hay segunda búsqueda
    assert [s.query for s in mq.query_plan("Taza ceramica", 1)] == ["Taza ceramica"]


@pytest.mark.parametrize("value, expected", [(None, 1), ("x", 1), (0, 1), (-4, 1), (1, 1), (2, 2), (3, 3), (9, 3), ("2", 2), (2.9, 2)])
def test_the_number_of_variants_is_clamped(value, expected):
    assert mq.clamp_variants(value) == expected


def test_a_name_without_words_has_no_plan():
    assert mq.query_plan("", 3) == [] and mq.query_plan("   ", 1) == [] and mq.query_plan("BX0123", 3) == []


def test_it_is_deterministic():
    runs = [mq.query_plan(LONG, 3) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_enye_and_accents_survive_in_the_output():
    assert mq.short_title("Baño Organizador Plástico Blanco 30x20") == "Baño Organizador Plástico"


def test_hostile_titles_do_not_break_it_and_stay_short():
    for name in ("x" * 5000, "\x00\x1b[31m" * 50, "🔥" * 100, " ".join(["Lámpara"] * 300), "‮Evil‬ título"):
        for n in (1, 2, 3):
            for step in mq.query_plan(name, n):
                assert step.query and len(step.query) <= mq.MAX_QUERY_CHARS


# ─── no perder lo que identifica al producto ───────────────────────────────


@pytest.mark.parametrize("title, short, keys", [
    ("Funda Silicona iPhone 13 Pro Max Transparente", "Funda Silicona iPhone 13 Pro Max", "Funda Silicona iPhone 13"),
    ("Auriculares Bluetooth Inalámbricos TWS i12 Blanco", "Auriculares Bluetooth Inalámbricos TWS i12", "Auriculares Bluetooth i12"),
    ("Globo Metalizado Número 5 Dorado 40 cm", "Globo Metalizado Número 5", None),                 # claves == corto: una sola
    ("Cargador Rápido 20W USB-C PD Cable Incluido", "Cargador Rápido USB-C PD Cable", "Cargador Rápido USB-C"),
    ("Destornillador Philips PH2 x 100 mm Mango Bi-material", "Destornillador Philips PH2 Mango Bi-material",
     "Destornillador PH2 Bi-material"),
    ("Linterna LED Recargable USB 3 Modos CREE T6", "Linterna LED Recargable USB Modos", "Linterna LED T6"),
    ("Cuaderno A5 Tapa Dura Rayado 100 Hojas Celeste", "Cuaderno A5 Tapa Dura Rayado", "Cuaderno A5 Tapa"),
    ("Lámpara Luna 3D 15 cm Recargable USB Touch", "Lámpara Luna 3D Recargable USB", "Lámpara Luna 3D"),
])
def test_model_numbers_sizes_and_connectors_survive(title, short, keys):
    assert mq.short_title(title) == short
    if keys is not None:
        assert mq.keywords(title) == keys


@pytest.mark.parametrize("title, gone", [
    ("Organizador 3 Niveles 40x30", "3"), ("Kit de Herramientas 108 Piezas", "108"), ("Pack 10 Llaveros", "10"),
    ("Taladro 20V con 2 Baterías", "2"), ("Pistola de Silicona 40W con 10 Barras", "10"), ("Mate x 6 Calabaza", "6"),
    ("Tupper 1.5 L Hermético", "1.5"), ("Amoladora Angular 4 1/2 Pulgadas 850W", "4"), ("Termo 1 Litro Acero", "1"),
    ("Cinta Aisladora 20 m x 18 mm Pack 10", "20"), ("Escalera 4 Escalones Aluminio", "4"), ("Cable 7791234567890 USB", "7791234567890"),
])
def test_counts_measures_and_barcodes_are_still_dropped(title, gone):
    assert gone not in mq.content_words(title) and not any(gone in w.split() for w in mq.content_words(title))


def test_a_number_that_names_a_plus_model_is_not_taken_for_a_count():
    assert "iPhone 13" in mq.content_words("Funda iPhone 13 Plus Silicona")
    assert "Galaxy 21" in mq.content_words("Funda Galaxy 21 Ultra")
    assert "Cuaderno" in mq.content_words("Cuaderno 3 Materias") and not any("3" in w for w in mq.content_words("Cuaderno 3 Materias"))


def test_hyphenated_connectors_and_decimals_are_single_words():
    assert mq.content_words("Cable USB-C a USB-A de 1,5 m") == ["Cable", "USB-C", "USB-A"]
    assert mq.content_words("Parlante 3.5mm Jack") == ["Parlante", "Jack"]


def test_internal_codes_never_leak_even_glued_to_punctuation():
    assert "PA1" not in mq.short_title("Soporte (PA1) Pared") and "BX12" not in mq.keywords("Taza [BX12] Cerámica Grande")


@pytest.mark.parametrize("title", ["Set de Juego", "Kit de Limpieza", "Combo Kit Set", "Juego", "Doble Mini Extra Pro"])
def test_a_generic_single_word_is_never_a_query_of_its_own(title):
    for extra in mq.query_variants(title, 3)[1:]:
        assert len(extra.split()) >= 2, (title, extra)


def test_the_title_itself_is_always_the_first_query_even_if_it_is_one_word():
    assert mq.query_variants("Taza", 3) == ["Taza"] and mq.query_variants("Set de Juego", 3)[0] == "Set de Juego"
