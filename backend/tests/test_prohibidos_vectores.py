"""Los vectores compartidos del comparador de lo que no vendemos (regla v2).

`tests/prohibidos_vectores.json` es una COPIA de los casos que corren todos los sistemas que usan la lista (el radar,
el que busca proveedor, la nube y este). Se corren contra la lista REAL (`app/prohibidos.yaml`) y se exige el 100 %: si
uno falla, el port se desvió de la regla o la copia de la lista quedó vieja. Un vector que parezca mal no se saca de acá:
se discute con el dueño del comparador.

Se corren como `examinar(es, zh)`: dos textos SEPARADOS, en ese orden; `null` = ese texto no está.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import prohibidos

DATA = json.loads((Path(__file__).parent / "prohibidos_vectores.json").read_text(encoding="utf-8"))
VECTORES = DATA["vectores"]


@pytest.fixture(autouse=True)
def _lista_real():
    prohibidos.reiniciar()
    yield
    prohibidos.reiniciar()


def _id(v: dict) -> str:
    return f"{v['es']!s:.40}|{v['zh']!s:.20}"


@pytest.mark.parametrize("v", VECTORES, ids=_id)
def test_vector(v):
    lista = prohibidos.lista_local()
    assert lista.fuente == prohibidos.FUENTE_ARCHIVO, "los vectores se corren contra la lista real, no contra el mínimo"
    r = prohibidos.examinar(v["es"], v["zh"], categorias=lista.categorias)
    obtenido = r.veredicto.categoria if r.veredicto is not None else None
    assert (r.estado, obtenido) == (v["estado"], v["espera"]), (
        f"{v['es']!r} / {v['zh']!r}: esperaba {v['estado']} ({v['espera']}), dio {r.estado} ({obtenido})"
        f"{' por «' + r.veredicto.palabra + '»' if r.veredicto else ''}. Nota: {v.get('nota')}")


def test_estan_todos_y_el_cien_por_ciento_pasa():
    """La cuenta, para que un archivo recortado no pase por bueno."""
    assert DATA["regla"] == 2 and len(VECTORES) >= 141
    estados = [v["estado"] for v in VECTORES]
    assert estados.count("prohibido") >= 72 and estados.count("ok") >= 63 and estados.count("sin_texto") >= 6
    lista = prohibidos.lista_local().categorias
    mal = []
    for v in VECTORES:
        r = prohibidos.examinar(v["es"], v["zh"], categorias=lista)
        if (r.estado, r.veredicto.categoria if r.veredicto else None) != (v["estado"], v["espera"]):
            mal.append(v["es"] or v["zh"])
    assert mal == [], f"{len(mal)} de {len(VECTORES)} vectores no dan lo esperado"


def test_revisar_no_distingue_sin_texto_pero_examinar_si():
    assert prohibidos.revisar("12345", None) is None and prohibidos.revisar("Mochila escolar") is None
    assert prohibidos.examinar("12345", None).estado == prohibidos.SIN_TEXTO
    assert prohibidos.examinar("+ / ,", "").se_puede is False
    assert prohibidos.examinar("Mochila escolar").se_puede is True
    assert prohibidos.examinar("Vitamina C").se_puede is False
