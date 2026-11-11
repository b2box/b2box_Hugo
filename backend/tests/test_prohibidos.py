"""Lo que no vendemos (app/prohibidos.py): la comparación con sus excepciones y de dónde sale la lista.

  * frena lo prohibido en español, inglés y chino, y NO frena lo que solo se le parece (accesorios, «cremallera»);
  * la lista sale de la nube si está configurada, si no del archivo que viaja con el código, y si no del mínimo;
  * con el mínimo la lista NO es completa (las activaciones automáticas se niegan: ver test_prohibidos_gate.py).

Sin red: la nube es un cliente falso.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import prohibidos


@pytest.fixture(autouse=True)
def _limpio():
    prohibidos.reiniciar()
    yield
    prohibidos.reiniciar()


def _config(monkeypatch, url: str = "", key: str = "") -> None:
    monkeypatch.setattr(prohibidos, "get_settings",
                        lambda: SimpleNamespace(motor_prohibidos_url=url, motor_prohibidos_key=key))


class _Nube:
    """Un `httpx.AsyncClient` de mentira: cuenta los pedidos y contesta lo que se le diga."""

    def __init__(self, status: int = 200, body=None, exc: Exception | None = None) -> None:
        self.status, self.body, self.exc = status, body, exc
        self.calls: list[dict] = []

    async def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if self.exc is not None:
            raise self.exc
        import json as _json

        raw = _json.dumps(self.body).encode()
        return SimpleNamespace(status_code=self.status, content=raw, json=lambda: self.body)

    async def aclose(self) -> None:  # pragma: no cover  (no es propio: no se cierra)
        raise AssertionError("no se cierra un cliente que no es propio")


# ─── La comparación ────────────────────────────────────────────────


@pytest.mark.parametrize("texto, categoria", [
    ("vitamina c 1000mg 60 capsulas", "suplementos"),
    ("Whey Protein Isolate 2kg", "suplementos"),
    ("保健品 维生素 片", "suplementos"),
    ("kit de pesticida concentrado", "quimicos"),
    ("Ácido clorhídrico 1 litro", "quimicos"),
    ("农药 杀虫剂", "quimicos"),
    ("jeringa descartable 5ml x100", "medicamentos"),
    ("vape desechable 5000 puff", "tabaco y vapeo"),
    ("电子烟 一次性", "tabaco y vapeo"),
    ("pistola airsoft 9mm resorte", "armas"),
    ("encendedor de bencina recargable", "inflamables y aerosoles"),
    ("shampoo anticaspa 400ml", "limpieza y cosmetica"),
    ("croquetas para perro adulto 15kg", "comida y bebida"),
])
def test_frena_lo_que_no_vendemos(texto, categoria):
    v = prohibidos.revisar(texto)
    assert v is not None, f"{texto!r} tendría que estar prohibido"
    assert v.categoria == categoria
    assert v.palabra in v.motivo and categoria in v.motivo


@pytest.mark.parametrize("texto", [
    "pistola de silicona caliente 40w",            # encoladora
    "pistola de masaje muscular",
    "热熔胶枪 glue gun",
    "pistola de agua para niños",
    "cremallera metalica 20cm",                    # «crema» adentro de «cremallera»
    "placido descanso almohada",                   # «acido» adentro de «placido»
    "encendedor usb recargable sin gas",
    "jeringa de silicona para reposteria",
    "molinillo de café manual",                    # nombra un consumible sin serlo
    "dispensador de jabón automático",
    "organizadores de maquillaje acrílico",        # la excepción vale como comienzo de palabra
    "pastillero semanal 7 dias",
    "termómetro de cocina digital",
    "食品级 硅胶 铲",                               # «grado alimenticio»
    "mochila escolar impermeable",
    "auriculares bluetooth deportivos",
])
def test_no_frena_lo_que_si_vendemos(texto):
    v = prohibidos.revisar(texto)
    assert v is None, f"{texto!r} se frenó de más: {v.motivo if v else ''}"


def test_mira_todos_los_textos_juntos():
    assert prohibidos.revisar("gomitas surtidas", "保健品") is not None
    assert prohibidos.revisar("Botella térmica", "保健品 维生素") is not None      # lo dice solo el título en chino
    assert prohibidos.revisar(None, "", "mochila") is None
    assert prohibidos.revisar() is None


def test_normalizar_saca_tildes_mayusculas_y_separadores():
    assert prohibidos.normalizar("  ÁCIDO-Clorhídrico, 1L ") == "acido clorhidrico 1l"
    assert prohibidos.normalizar(None) == ""


# ─── El archivo y el mínimo ────────────────────────────────────────


def test_la_lista_que_viaja_con_el_codigo_se_lee_entera():
    """Si alguien rompe el YAML, que falle acá y no en silencio en producción."""
    assert prohibidos.RUTA.exists(), f"falta {prohibidos.RUTA}"
    found = prohibidos.lista_local()
    assert found.fuente == prohibidos.FUENTE_ARCHIVO and found.completa
    assert len(found.categorias) >= 8
    for c in found.categorias:
        assert c.porque, f"la categoría {c.categoria} no dice por qué"
        assert c.es or c.zh
    nombres = {c.categoria for c in found.categorias}
    assert {"suplementos", "quimicos", "medicamentos", "armas"} <= nombres


def test_sin_archivo_queda_el_minimo_y_no_es_una_lista_completa(tmp_path):
    found = prohibidos.lista_local(tmp_path / "no-existe.yaml")
    assert found.fuente == prohibidos.FUENTE_MINIMO and not found.completa
    assert [c.categoria for c in found.categorias] == ["suplementos", "quimicos"]
    assert prohibidos.revisar("vitamina c", categorias=found.categorias) is not None     # nunca «todo permitido»


@pytest.mark.parametrize("contenido", ["prohibidos: []\n", "esto: [no es, una lista\n", "", "prohibidos:\n  - categoria: x\n"])
def test_archivo_vacio_o_roto_tampoco_abre_la_puerta(tmp_path, contenido):
    ruta = tmp_path / "roto.yaml"
    ruta.write_text(contenido, encoding="utf-8")
    found = prohibidos.lista_local(ruta)
    assert found.fuente == prohibidos.FUENTE_MINIMO and not found.completa
    assert prohibidos.revisar("pesticida", categorias=found.categorias) is not None


def test_el_archivo_se_relee_si_cambia(tmp_path):
    ruta = tmp_path / "lista.yaml"
    ruta.write_text("prohibidos:\n  - categoria: uno\n    es: [cosa]\n", encoding="utf-8")
    assert [c.categoria for c in prohibidos.lista_local(ruta).categorias][0] == "uno"
    ruta.write_text("prohibidos:\n  - categoria: dos\n    es: [otra]\n", encoding="utf-8")
    import os

    os.utime(ruta, (1_900_000_000, 1_900_000_000))
    assert [c.categoria for c in prohibidos.lista_local(ruta).categorias][0] == "dos"


# ─── La nube ───────────────────────────────────────────────────────

NUBE_JSON = {"version": 3, "categorias": [
    {"categoria": "suplementos", "porque": "No vendemos nada que se ingiera.", "es": ["suplemento", "vitamina"],
     "zh": ["保健品"]},
    {"categoria": "pirotecnia", "porque": "No.", "es": ["petardo"], "zh": [], "excepto": ["petardo de juguete"]},
]}


async def test_sin_url_no_se_pide_nada_y_va_el_archivo(monkeypatch):
    _config(monkeypatch)
    nube = _Nube(body=NUBE_JSON)
    found = await prohibidos.lista(nube)
    assert found.fuente == prohibidos.FUENTE_ARCHIVO and nube.calls == []


async def test_con_url_y_clave_se_usa_la_lista_de_la_nube_con_su_cabecera_y_cache(monkeypatch):
    _config(monkeypatch, "https://nube.example/motor-prohibidos", "clave-de-prueba")
    nube = _Nube(body=NUBE_JSON)
    found = await prohibidos.lista(nube)
    assert found.fuente == prohibidos.FUENTE_NUBE and found.completa and found.version == 3
    assert [c.categoria for c in found.categorias][:2] == ["suplementos", "pirotecnia"]
    assert nube.calls[0]["url"] == "https://nube.example/motor-prohibidos"
    assert nube.calls[0]["headers"]["x-motor-key"] == "clave-de-prueba"
    assert prohibidos.revisar("petardo grande", categorias=found.categorias).categoria == "pirotecnia"
    assert prohibidos.revisar("petardo de juguete", categorias=found.categorias) is None
    # Caché en memoria: el segundo pedido no sale a la red.
    again = await prohibidos.lista(nube)
    assert again is found and len(nube.calls) == 1


async def test_una_categoria_de_la_nube_sin_excepto_hereda_las_del_archivo(monkeypatch):
    """La nube puede mandar solo las palabras: sin las excepciones volverían los falsos positivos de accesorios."""
    _config(monkeypatch, "https://nube.example/x", "k")
    found = await prohibidos.lista(_Nube(body=NUBE_JSON))
    sup = next(c for c in found.categorias if c.categoria == "suplementos")
    assert "shaker" in sup.excepto
    assert prohibidos.revisar("shaker para proteina", categorias=found.categorias) is None
    # Si la nube manda `excepto` (aunque sea vacío), manda la nube.
    prohibidos.reiniciar()
    body = {"categorias": [{"categoria": "suplementos", "es": ["proteina"], "zh": [], "excepto": []}]}
    found = await prohibidos.lista(_Nube(body=body))
    assert prohibidos.revisar("shaker para proteina", categorias=found.categorias) is not None


@pytest.mark.parametrize("nube", [
    _Nube(status=500, body={}),
    _Nube(status=401, body={"error": "no"}),
    _Nube(body={"categorias": []}),
    _Nube(body=["no", "es", "un", "objeto"]),
    _Nube(exc=TimeoutError()),
])
async def test_si_la_nube_falla_va_el_archivo_y_no_se_insiste_enseguida(monkeypatch, nube):
    _config(monkeypatch, "https://nube.example/x", "k")
    found = await prohibidos.lista(nube)
    assert found.fuente == prohibidos.FUENTE_ARCHIVO and found.completa
    await prohibidos.lista(nube)
    assert len(nube.calls) == 1, "después de un fallo no se vuelve a pedir por un rato"


async def test_una_url_que_no_es_https_o_sin_clave_no_se_usa(monkeypatch):
    nube = _Nube(body=NUBE_JSON)
    _config(monkeypatch, "http://nube.example/x", "k")
    assert (await prohibidos.lista(nube)).fuente == prohibidos.FUENTE_ARCHIVO
    _config(monkeypatch, "https://nube.example/x", "")
    assert (await prohibidos.lista(nube)).fuente == prohibidos.FUENTE_ARCHIVO
    assert nube.calls == []


async def test_sin_nube_y_sin_archivo_queda_el_minimo(monkeypatch, tmp_path):
    _config(monkeypatch)
    monkeypatch.setattr(prohibidos, "RUTA", tmp_path / "no-existe.yaml")
    found = await prohibidos.lista()
    assert found.fuente == prohibidos.FUENTE_MINIMO and not found.completa and found.categorias
    assert prohibidos.estado(found) == {"fuente": "minimo", "version": None, "categorias": 2, "completa": False}


# ─── Ninguna fuente puede achicar la protección ────────────────────


async def test_una_lista_de_la_nube_con_una_sola_categoria_no_reemplaza_todo(monkeypatch):
    """Antes una respuesta 200 con una categoría dejaba pasar armas, químicos, vapeo… Lo que falta sale del archivo."""
    _config(monkeypatch, "https://nube.example/x", "k")
    body = {"version": 9, "categorias": [{"categoria": "pirotecnia", "es": ["petardo"], "zh": []}]}
    found = await prohibidos.lista(_Nube(body=body))
    assert found.fuente == prohibidos.FUENTE_NUBE and found.version == 9
    nombres = [c.categoria for c in found.categorias]
    assert nombres[0] == "pirotecnia" and {"suplementos", "quimicos", "armas", "tabaco y vapeo"} <= set(nombres)
    for texto in ("petardo grande", "vitamina c", "pistola airsoft", "vape desechable", "shampoo anticaspa"):
        assert prohibidos.revisar(texto, categorias=found.categorias) is not None, texto


async def test_dentro_de_una_categoria_que_la_nube_trae_mandan_sus_palabras_salvo_el_minimo(monkeypatch):
    """La nube puede sacar una palabra de una categoría (así se corrige un falso positivo), pero no las del mínimo."""
    _config(monkeypatch, "https://nube.example/x", "k")
    body = {"categorias": [{"categoria": "medicamentos", "es": ["jeringa"], "zh": [], "excepto": []},
                           {"categoria": "suplementos", "es": ["gomitas"], "zh": [], "excepto": []}]}
    found = await prohibidos.lista(_Nube(body=body))
    assert prohibidos.revisar("termometro clinico", categorias=found.categorias) is None       # la nube lo sacó
    assert prohibidos.revisar("gomitas surtidas", categorias=found.categorias) is not None
    for texto in ("vitamina c", "creatina 300 g", "保健品"):                                    # el mínimo no se saca
        v = prohibidos.revisar(texto, categorias=found.categorias)
        assert v is not None and v.categoria == "suplementos", texto


def test_el_archivo_tambien_lleva_el_minimo(tmp_path):
    ruta = tmp_path / "corta.yaml"
    ruta.write_text("prohibidos:\n  - categoria: suplementos\n    es: [gomitas]\n", encoding="utf-8")
    found = prohibidos.lista_local(ruta)
    assert found.fuente == prohibidos.FUENTE_ARCHIVO
    assert {c.categoria for c in found.categorias} == {"suplementos", "quimicos"}
    assert prohibidos.revisar("vitamina c", categorias=found.categorias) is not None
    assert prohibidos.revisar("pesticida", categorias=found.categorias) is not None


def test_con_piso_no_duplica_ni_cambia_lo_que_ya_estaba():
    base = prohibidos.lista_local().categorias
    assert prohibidos.con_piso(base, categorias=base, palabras=prohibidos.categorias_de(prohibidos.MINIMO)) == base
