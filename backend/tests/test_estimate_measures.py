"""Medidas de la unidad para el estimado: sizeInfo de OTAPI y la IA."""

import json

from app.estimate import measures
from app.estimate.otapi_item import parse_item

# sizeInfo real de un spinner (oferta 725268539232, 28-oct-2026), recortado.
SIZE_INFO = json.dumps([
    {"skuId": "1", "weight": 0.07, "width": 9, "height": 2, "length": 9, "pkgSizeSource": "商家自填"},
    {"skuId": "2", "weight": 0.08, "width": 9, "height": 2, "length": 9},
    {"skuId": "3", "weight": 0.9, "width": 30, "height": 20, "length": 40},
    {"skuId": "4", "weight": 0, "width": 9, "height": 2, "length": 9},
])


def test_size_info_mediana_por_sku_y_descarta_los_rotos():
    unit = measures.from_size_info([{"Name": "unit", "Value": "个"},
                                    {"Name": "sizeInfo", "Value": SIZE_INFO}])
    assert unit is not None and unit.source == measures.SOURCE_OTAPI
    assert unit.kg == 0.08                                   # mediana de 0,07 / 0,08 / 0,9
    assert (unit.length_cm, unit.width_cm, unit.height_cm) == (9.0, 9.0, 2.0)
    assert abs(unit.cbm - 0.000162) < 1e-12


def test_sin_size_info_no_hay_medidas():
    assert measures.from_size_info([{"Name": "unit", "Value": "个"}]) is None
    assert measures.from_size_info("basura") is None
    assert measures.from_size_info([{"Name": "sizeInfo", "Value": "[{"}]) is None


def test_parse_item_lee_la_unidad_de_la_oferta():
    item = {"Title": "spinner", "Price": {"OriginalPrice": 1.49},
            "FeaturedValues": [{"Name": "sizeInfo", "Value": SIZE_INFO}],
            # El de la oferta entera (un pack): no se usa.
            "PhysicalParameters": {"Weight": 0.92625, "Length": 9.75, "Width": 7.8, "Height": 14.5}}
    offer = parse_item("725268539232", item)
    assert offer is not None and offer.unit is not None and offer.unit.kg == 0.08


def test_parse_ai_valida_rangos():
    ok = measures.parse_ai('```json\n{"largo_cm": 9, "ancho_cm": 9, "alto_cm": 2, "peso_kg": 0.07}\n```')
    assert ok is not None and ok.source == measures.SOURCE_AI and ok.kg == 0.07
    assert measures.parse_ai('{"largo_cm": 9, "ancho_cm": 9, "alto_cm": 2, "peso_kg": 900}') is None
    assert measures.parse_ai('{"largo_cm": 0, "ancho_cm": 9, "alto_cm": 2, "peso_kg": 0.1}') is None
    assert measures.parse_ai("no sé") is None
