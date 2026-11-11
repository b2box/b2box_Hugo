"""Lo que B2BOX no vende: el freno antes de activar un producto o de gastar una búsqueda en él.

Químicos, suplementos, medicamentos, vapeo, armas, inflamables, comida y cosmética no se venden. Acá no es un puntaje: un
producto que cae en la lista NO se activa solo (pricing/price_gate.py), no lo activa la bandeja
(pricing/price_review_actions.py) y, con `prohibidos_verify = 1`, `/verify` no lo reenvía a buscar (api/routes.py).

De dónde sale la lista, siempre en este orden (el mismo en los tres sistemas que la usan):

  1. si están `MOTOR_PROHIBIDOS_URL` y `MOTOR_PROHIBIDOS_KEY`: `GET` a esa URL con la cabecera `x-motor-key`; la respuesta
     200 es `{"version": 3, "categorias": [{"categoria", "porque", "es": [...], "zh": [...], "excepto": [...]}]}`.
     Se guarda en memoria 10 minutos; tope de espera 5 s;
  2. si no hay URL o falla: `prohibidos.yaml`, que viaja al lado de este archivo (adentro de la imagen);
  3. si tampoco: el mínimo fijo de abajo (suplementos y químicos). Nunca «sin lista = todo permitido».

Ninguna fuente puede ACHICAR la protección (`con_piso`): si a la lista de la nube le falta una categoría que el archivo
tiene, esa categoría se toma del archivo (una respuesta 200 con una sola categoría no reemplaza todo); y las palabras del
mínimo de código rigen siempre, vengan de donde vengan las demás. Dentro de una categoría que la nube sí trae mandan sus
palabras: ahí es donde una persona corrige la lista.

`prohibidos.yaml` es una COPIA de la lista canónica del radar de productos: no se edita a mano acá (se vuelve a copiar).

Con el mínimo (3) el filtro sigue frenando lo que conoce, pero la lista NO está completa (`Lista.completa` es False): las
activaciones automáticas se niegan hasta que vuelva una lista de verdad. Dos categorías no alcanzan para prender productos
sin que nadie mire.

Comparación: la regla v2 del radar de productos, portada tal cual (ver «Cómo se decide» más abajo):

  * NFKC, minúsculas, sin tildes y con los separadores vueltos espacio; en chino, sin los espacios del medio;
  * español / inglés: PALABRA ENTERA («acido» no frena «placido»), con sus plurales; chino: pedazo de texto;
  * una excepción (`excepto`) perdona SOLO la palabra prohibida que tiene pegada: «molinillo de café» pasa, «proteína
    whey 1 kg + shaker de regalo» frena. Ya no apaga la categoría entera;
  * cada texto del producto se lee por separado;
  * tres respuestas (`examinar`): `prohibido`, `ok` y `sin_texto`. «No había nada que revisar» no es «ok»: quien activa
    sin una persona lo trata como freno.

`examinar` y `revisar` son puras (sin red ni base). `lista()` es la única que sale a la red.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import get_settings

log = logging.getLogger(__name__)

RUTA = Path(__file__).resolve().parent / "prohibidos.yaml"

NUBE_TTL_S = 600.0            # caché en memoria de la lista de la nube
NUBE_TIMEOUT_S = 5.0
NUBE_REINTENTO_S = 60.0       # después de un fallo de la nube no se vuelve a probar por este rato
NUBE_MAX_BYTES = 1_000_000

FUENTE_NUBE, FUENTE_ARCHIVO, FUENTE_MINIMO = "nube", "archivo", "minimo"

# Lo que se aplica si no hay nube ni archivo. Nunca quedarse sin filtro.
MINIMO: list[dict[str, Any]] = [
    {"categoria": "suplementos", "porque": "No vendemos nada que se ingiera.",
     "es": ["suplemento", "suplementos", "vitamina", "vitaminas", "proteina", "creatina", "colageno"],
     "zh": ["保健品", "维生素", "蛋白粉"]},
    {"categoria": "quimicos", "porque": "Quimicos: carga peligrosa, no entran en consolidado.",
     "es": ["quimico", "quimicos", "pesticida", "insecticida", "solvente", "acido"],
     "zh": ["化学品", "农药", "杀虫剂"]},
]


@dataclass(frozen=True)
class Categoria:
    categoria: str
    porque: str
    es: tuple[str, ...]
    zh: tuple[str, ...]
    # Frases que DESACTIVAN la categoría para ese producto.
    excepto: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lista:
    categorias: tuple[Categoria, ...]
    fuente: str                       # nube | archivo | minimo
    version: int | None = None

    @property
    def completa(self) -> bool:
        """¿Es una lista de verdad (la de la nube o la del archivo)? Con el mínimo de código, no."""
        return self.fuente in (FUENTE_NUBE, FUENTE_ARCHIVO) and bool(self.categorias)


@dataclass(frozen=True)
class Veredicto:
    categoria: str
    porque: str
    palabra: str

    @property
    def motivo(self) -> str:
        return f"no vendemos {self.categoria} ({self.palabra})"


# ─── Normalización ─────────────────────────────────────────────────


def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def _nfkc(texto: str | None) -> str:
    """Las formas «de compatibilidad» a su letra común: ｖｉｔａｍｉｎａ (ancho completo) es vitamina, ＋ es +, ， es una coma.
    Sin esto un nombre escrito con esas letras pasaba el filtro entero."""
    return unicodedata.normalize("NFKC", str(texto or ""))


def normalizar(texto: str | None) -> str:
    """NFKC, minúsculas, sin tildes, con los separadores vueltos espacio."""
    t = _sin_tildes(_nfkc(texto).lower())
    return re.sub(r"[^0-9a-z一-鿿]+", " ", t).strip()


_ZH_ESPACIO = re.compile(r"(?<=[一-鿿]) +(?=[一-鿿])")


def _zh_junto(texto_norm: str) -> str:
    """El chino no se separa con espacios: «蛋白 粉» es 蛋白粉. Se sacan los espacios que quedan ENTRE dos caracteres
    chinos (los que tocan letras o números no)."""
    return _ZH_ESPACIO.sub("", texto_norm)


def tiene_texto(*textos: str | None) -> bool:
    """¿Hay algo que revisar? Un nombre vacío, o solo números y signos, no es un nombre: de eso no se puede decir que
    «no está prohibido»."""
    return any(re.search(r"[a-z一-鿿]", normalizar(t)) for t in textos)


def _palabras(valor: Any) -> tuple[str, ...]:
    if not isinstance(valor, Iterable) or isinstance(valor, (str, bytes)):
        return ()
    vistas: list[str] = []
    for v in valor:
        p = normalizar(v)
        if p and p not in vistas:
            vistas.append(p)
    return tuple(vistas)


def _filas(crudo: Any) -> list[dict[str, Any]]:
    """Las filas de categorías de cualquiera de las dos formas: el YAML (`prohibidos:`) o el JSON de la nube
    (`categorias:`)."""
    if isinstance(crudo, dict):
        crudo = crudo.get("categorias") if "categorias" in crudo else crudo.get("prohibidos")
    return [f for f in crudo if isinstance(f, dict)] if isinstance(crudo, list) else []


def categorias_de(crudo: Any, heredar: dict[str, Categoria] | None = None) -> tuple[Categoria, ...]:
    """`heredar` (nombre → categoría del archivo): una fila que no trae la clave `excepto` toma las excepciones de la
    categoría del mismo nombre del archivo, para que una lista de la nube sin excepciones no vuelva a frenar accesorios."""
    salida: list[Categoria] = []
    for fila in _filas(crudo):
        nombre = str(fila.get("categoria") or "").strip()
        if not nombre:
            continue
        es, zh = _palabras(fila.get("es")), _palabras(fila.get("zh"))
        if not es and not zh:
            continue
        if "excepto" in fila or not heredar or nombre not in heredar:
            excepto = _palabras(fila.get("excepto"))
        else:
            excepto = heredar[nombre].excepto
        salida.append(Categoria(nombre, str(fila.get("porque") or "").strip(), es, zh, excepto))
    return tuple(salida)


def con_piso(cats: Iterable[Categoria], *, categorias: Iterable[Categoria] = (),
             palabras: Iterable[Categoria] = ()) -> tuple[Categoria, ...]:
    """`cats` con un piso debajo: ninguna fuente puede ACHICAR la protección.

      * `categorias` (el archivo que viaja con el código): una categoría que a `cats` le falta se toma entera de ahí. Las
        que `cats` sí trae quedan como vienen: ahí es donde una persona corrige palabras, y tiene que poder sacar una;
      * `palabras` (el mínimo fijo en código): rige siempre. Sus categorías se agregan si faltan y, si están, sus
        palabras se suman a las que haya. Lo del mínimo no se puede sacar desde afuera.
    """
    out = list(cats)
    at = {c.categoria: i for i, c in enumerate(out)}
    for extra in categorias:
        if extra.categoria not in at:
            at[extra.categoria] = len(out)
            out.append(extra)
    for floor in palabras:
        i = at.get(floor.categoria)
        if i is None:
            at[floor.categoria] = len(out)
            out.append(floor)
            continue
        c = out[i]
        es = c.es + tuple(w for w in floor.es if w not in c.es)
        zh = c.zh + tuple(w for w in floor.zh if w not in c.zh)
        if (es, zh) != (c.es, c.zh):
            out[i] = Categoria(c.categoria, c.porque, es, zh, c.excepto)
    return tuple(out)


# ─── Cómo se decide (regla v2) ─────────────────────────────────────
#
# Port fiel del comparador canónico (el del radar de productos). No se rediseña acá: si cambia allá, se vuelve a portar,
# y `tests/test_prohibidos_vectores.py` corre los mismos casos que corren los otros sistemas y exige el 100 %.
#
# Una palabra prohibida frena SALVO que una excepción la perdone, y una excepción perdona SOLO la palabra prohibida que
# tiene pegada: el accesorio y su consumible tienen que nombrar una sola cosa («shaker para proteína», «soap dispenser»,
# «termómetro de cocina»). Ya no apaga la categoría entera: «Proteína whey 1 kg + shaker de regalo» frena.
# Rompen esa unión: un corte (+ & , ; |), un número en el medio, un «con / y / incluye / regalo» justo antes del
# consumible, una preposición después del consumible o un peso después del accesorio. Los plurales frenan igual que el
# singular (los singulares no se deducen). Cada texto se lee por separado.

_CJK = re.compile(r"[\u4e00-\u9fff]")

#: Corta el nombre en dos productos: «+», «&», coma.
_CORTE = "xxcortexx"
#: Artículos y nexos que no dicen nada entre el accesorio y su consumible.
_ARTICULOS = frozenset({"la", "el", "los", "las", "un", "una", "the", "a", "su", "tu"})
#: Antes de la palabra prohibida, esto dice que el producto LA TRAE.
_INCLUYE = frozenset({"con", "with", "y", "e", "and", "mas", "plus", "incluye", "incluyen", "incluido", "incluida",
                      "includes", "included", "trae", "regalo"})
#: Después del consumible, una preposición abre SU complemento («alimento PARA perros»).
_PREPOSICIONES = frozenset({"para", "de", "del", "en", "con", "por", "sin", "x", "for", "of", "with", "in", "y", "e",
                            "and"})
_PESO = re.compile(r"^\d+(?:kg|kgs|g|gr|grs|kilo|kilos|lb|lbs)$")
_UNIDADES_PESO = frozenset({"kg", "kgs", "g", "gr", "grs", "kilo", "kilos", "lb", "lbs"})
#: Cuántas palabras puede haber entre el accesorio y su consumible cuando el accesorio va primero.
_MAX_ANTES = 6
#: Y cuando el consumible va primero («dog food storage container»).
_MAX_DESPUES = 2
#: En chino, caracteres entre una cosa y la otra, y lo que delata un regalo.
_MAX_ZH = 2
_ZH_REGALO = re.compile(r"[0-9送赠加]")
_Y = frozenset({"y", "e", "and", "o", "or"})


def _tokens(texto: str | None) -> list[str]:
    """Las palabras del nombre, con un corte donde el nombre suma OTRO producto."""
    # NFKC antes de buscar los cortes: «＋» y «，» también cortan.
    crudo = re.sub(r"[+&,;|]", f" {_CORTE} ", _nfkc(texto))
    return normalizar(crudo).split()


def _formas(palabra: str) -> tuple[str, ...]:
    """La palabra y sus plurales. Solo se AGREGAN plurales: sacarle la «s» a una palabra de la lista inventa singulares
    que nadie quiso frenar."""
    if len(palabra) < 3 or not palabra.isalpha():
        return (palabra,)
    formas = [palabra, palabra + "s", palabra + "es"]
    if palabra.endswith("z"):
        formas.append(palabra[:-1] + "ces")
    return tuple(formas)


def _apariciones(tokens: list[str], frase: str, por_prefijo: bool = False) -> list[tuple[int, int]]:
    """Dónde aparece `frase` en el nombre: (primera palabra, última palabra). `por_prefijo` es para las excepciones de
    una sola palabra: «organizador» cubre «organizadores»."""
    partes = frase.split()
    if not partes:
        return []
    n = len(partes)
    if n == 1 and por_prefijo:
        return [(i, i) for i, t in enumerate(tokens) if t.startswith(partes[0])]
    formas = [_formas(p) for p in partes]
    return [(i, i + n - 1) for i in range(len(tokens) - n + 1)
            if all(tokens[i + k] in formas[k] for k in range(n))]


def _es_numero(token: str) -> bool:
    return any(c.isdigit() for c in token)


def _perdona(tokens: list[str], prohibida: tuple[int, int], excepcion: tuple[int, int]) -> bool:
    """¿Esta excepción y esta palabra prohibida nombran UNA sola cosa?"""
    (pi, pf), (ei, ef) = prohibida, excepcion
    if not (ef < pi or ei > pf):
        return True                               # la excepción contiene la palabra: «termómetro de cocina»
    if ef < pi:
        # Accesorio primero: «dispensador automático de jabón».
        medio = tokens[ef + 1:pi]
        if len(medio) > _MAX_ANTES or _CORTE in medio or any(_es_numero(t) for t in medio):
            return False
        previo = next((t for t in reversed(medio) if t not in _ARTICULOS), "")
        return previo not in _INCLUYE              # «shaker CON proteína»: el producto la trae
    # Consumible primero: «soap dispenser», «pet food storage container».
    medio = tokens[pf + 1:ei]
    if len(medio) > _MAX_DESPUES or _CORTE in medio:
        return False
    if any(_es_numero(t) or t in _PREPOSICIONES for t in medio):
        return False
    # «alimento balanceado bolsa 20 kg»: lo que se vende por peso es el consumible.
    despues = tokens[ef + 1:ef + 3]
    if despues and (_PESO.match(despues[0])
                    or (despues[0].isdigit() and len(despues) > 1 and despues[1] in _UNIDADES_PESO)):
        return False
    return True


def _perdonar_coordinadas(tokens: list[str], apariciones: list[tuple[int, int]], perdonadas: list[bool]) -> None:
    """«Taza mezcladora para café Y BEBIDAS», «soap AND shampoo dispenser»: lo que va coordinado con un consumible ya
    perdonado es del mismo accesorio. «Shaker y proteína» no entra: ahí la «y» une el accesorio con el consumible."""
    def salta_articulos(i: int, paso: int) -> int:
        while 0 <= i < len(tokens) and tokens[i] in _ARTICULOS:
            i += paso
        return i

    cambio = True
    while cambio:
        cambio = False
        finales = {pf for (_, pf), ok in zip(apariciones, perdonadas) if ok}
        inicios = {pi for (pi, _), ok in zip(apariciones, perdonadas) if ok}
        for k, (pi, pf) in enumerate(apariciones):
            if perdonadas[k]:
                continue
            antes = salta_articulos(pi - 1, -1)
            despues = salta_articulos(pf + 1, 1)
            tras_la_y = (0 <= antes < len(tokens) and tokens[antes] in _Y
                         and salta_articulos(antes - 1, -1) in finales)
            antes_de_la_y = (0 <= despues < len(tokens) and tokens[despues] in _Y
                             and salta_articulos(despues + 1, 1) in inicios)
            if tras_la_y or antes_de_la_y:
                perdonadas[k] = True
                cambio = True


def _zh_apariciones(texto: str, pedazo: str) -> list[tuple[int, int]]:
    salida, desde = [], 0
    while pedazo:
        i = texto.find(pedazo, desde)
        if i < 0:
            break
        salida.append((i, i + len(pedazo) - 1))
        desde = i + 1
    return salida


def _zh_perdona(texto: str, prohibida: tuple[int, int], excepcion: tuple[int, int]) -> bool:
    (pi, pf), (ei, ef) = prohibida, excepcion
    if not (ef < pi or ei > pf):
        return True                               # 食品级 contiene 食品
    medio = texto[ef + 1:pi] if ef < pi else texto[pf + 1:ei]
    # 蛋白粉摇摇杯 es un shaker; 蛋白粉 1kg 送摇摇杯 es proteína con un regalo.
    return len(medio) <= _MAX_ZH and not _ZH_REGALO.search(medio)


@dataclass(frozen=True)
class _Lectura:
    """Lo que dice UN texto sobre UNA categoría."""
    sin_perdon: tuple[str, ...]   # palabras prohibidas que ninguna excepción cubre
    hay_excepcion: bool           # nombra algún accesorio de la categoría
    solo_nombra: bool             # no dice nada más que la palabra prohibida


def _leer(texto: str | None, cat: Categoria) -> _Lectura:
    tokens = _tokens(texto)
    norm = " ".join(tokens)

    # Español / inglés: PALABRA ENTERA («crema» no matchea «cremallera»).
    prohibidas = [(ap, p) for p in cat.es for ap in _apariciones(tokens, p)]
    excepciones = [ap for e in cat.excepto if e and not _CJK.search(e)
                   for ap in _apariciones(tokens, e, por_prefijo=True)]
    perdonadas = [any(_perdona(tokens, ap, ex) for ex in excepciones) for ap, _ in prohibidas]
    _perdonar_coordinadas(tokens, [ap for ap, _ in prohibidas], perdonadas)
    sin_perdon = [p for (_, p), ok in zip(prohibidas, perdonadas) if not ok]

    # Chino: pedazo de texto, sin los espacios del medio («蛋白 粉» es 蛋白粉).
    zh = _zh_junto(norm)
    zh_prohibidas = [(ap, p) for p in cat.zh if p for ap in _zh_apariciones(zh, p)]
    zh_excepciones = [ap for e in cat.excepto if e and _CJK.search(e) for ap in _zh_apariciones(zh, e)]
    sin_perdon += [p for ap, p in zh_prohibidas if not any(_zh_perdona(zh, ap, ex) for ex in zh_excepciones)]

    cubiertos = {i for (a, b), _ in prohibidas for i in range(a, b + 1)}
    resto = [t for i, t in enumerate(tokens)
             if i not in cubiertos and t != _CORTE and t not in _ARTICULOS and t not in _PREPOSICIONES]
    return _Lectura(tuple(sin_perdon), bool(excepciones or zh_excepciones),
                    bool(prohibidas) and not zh_prohibidas and not resto)


def _matchea_textos(textos: Iterable[str | None], cat: Categoria) -> str | None:
    """La palabra que frena este producto en esta categoría, o None. Cada texto se lee por separado: el accesorio de un
    texto no perdona la palabra prohibida de otro. Única salvedad: si un texto SOLO nombra el consumible («café») y otro
    es limpiamente el accesorio («molinillo manual», 磨豆机), el producto es el accesorio."""
    lecturas = [_leer(t, cat) for t in textos if str(t or "").strip()]
    hay_accesorio_limpio = [lec.hay_excepcion and not lec.sin_perdon for lec in lecturas]
    for i, lectura in enumerate(lecturas):
        if not lectura.sin_perdon:
            continue
        otro_es_el_accesorio = any(ok for j, ok in enumerate(hay_accesorio_limpio) if j != i)
        if lectura.solo_nombra and otro_es_el_accesorio:
            continue
        return lectura.sin_perdon[0]
    return None


PROHIBIDO, OK, SIN_TEXTO = "prohibido", "ok", "sin_texto"


@dataclass(frozen=True)
class Resultado:
    """Lo que dio la revisión. Tres respuestas, no dos:

      prohibido  cae en la lista (`veredicto` dice por qué);
      ok         se revisó y no cae;
      sin_texto  NO HABÍA NADA QUE REVISAR: nombre vacío, o solo números y signos.

    `sin_texto` NO es `ok`. Quien activa SIN una persona lo trata como un freno: no se puede decir que algo no está
    prohibido si no se sabe qué es. Con una persona delante alcanza con `revisar`."""
    estado: str
    veredicto: Veredicto | None = None

    @property
    def se_puede(self) -> bool:
        """True solo si se revisó y no cae en la lista."""
        return self.estado == OK


def examinar(*textos: str | None, categorias: Iterable[Categoria] | None = None) -> Resultado:
    """Revisa los nombres de un producto y dice cuál de las tres respuestas es. Cada texto va por separado (el nombre de
    la tienda, el de la ficha, el título original). Sin `categorias` usa la lista local (archivo o mínimo)."""
    if not tiene_texto(*textos):
        return Resultado(SIN_TEXTO)
    cats = lista_local().categorias if categorias is None else categorias
    for cat in cats:
        palabra = _matchea_textos(textos, cat)
        if palabra:
            return Resultado(PROHIBIDO, Veredicto(cat.categoria, cat.porque, palabra))
    return Resultado(OK)


def revisar(*textos: str | None, categorias: Iterable[Categoria] | None = None) -> Veredicto | None:
    """El primer motivo por el que este producto no se vende, o None.

    OJO: None quiere decir «no cae en la lista» O «no había nada que revisar». Quien decide solo, sin una persona, usa
    `examinar`, que los distingue."""
    return examinar(*textos, categorias=categorias).veredicto


# ─── De dónde sale la lista ────────────────────────────────────────

_archivo: tuple[tuple[str, float], Lista | None] | None = None     # ((ruta, mtime), lista)
_nube: tuple[float, Lista] | None = None                           # (vence_en monotonic, lista)
_nube_fallo_hasta = 0.0
_avisado: set[str] = set()


def _avisar_una_vez(clave: str, mensaje: str, *args: Any) -> None:
    if clave not in _avisado:
        _avisado.add(clave)
        log.error(mensaje, *args)


def _leer_archivo(ruta: Path | None = None) -> Lista | None:
    """La lista del archivo que viaja con el código, o None si no está o no se puede usar. Se relee si cambia."""
    global _archivo
    ruta = ruta or RUTA
    try:
        clave = (str(ruta), ruta.stat().st_mtime)
    except OSError:
        _avisar_una_vez(f"falta:{ruta}", "prohibidos: no está %s: queda el mínimo de código", ruta.name)
        return None
    if _archivo is not None and _archivo[0] == clave:
        return _archivo[1]
    try:
        with open(ruta, encoding="utf-8") as fh:
            crudo = yaml.safe_load(fh)
        cats = categorias_de(crudo)
        if not cats:
            raise ValueError("sin categorías usables")
        cats = con_piso(cats, palabras=categorias_de(MINIMO))          # el mínimo rige siempre
        version = crudo.get("version") if isinstance(crudo, dict) else None
        found: Lista | None = Lista(cats, FUENTE_ARCHIVO, version if isinstance(version, int) else None)
        log.info("prohibidos: %d categorías desde el archivo", len(cats))
    except Exception as exc:  # noqa: BLE001  (YAML roto, lista vacía)
        _avisar_una_vez(f"roto:{ruta}", "prohibidos: %s no se pudo usar (%s): queda el mínimo de código", ruta.name,
                        type(exc).__name__)
        found = None
    _archivo = (clave, found)
    return found


def _minimo() -> Lista:
    return Lista(categorias_de(MINIMO), FUENTE_MINIMO)


def lista_local(ruta: Path | None = None) -> Lista:
    """Sin red: el archivo o, si no se puede, el mínimo de código."""
    return _leer_archivo(ruta) or _minimo()


def _nube_config() -> tuple[str, str] | None:
    s = get_settings()
    url, key = (s.motor_prohibidos_url or "").strip(), (s.motor_prohibidos_key or "").strip()
    if not url or not key:
        return None
    if not url.lower().startswith("https://"):
        _avisar_una_vez("nube_http", "prohibidos: MOTOR_PROHIBIDOS_URL no es https: no se usa")
        return None
    return url, key


async def _leer_nube(http: Any = None) -> Lista | None:
    """La lista de la nube (con caché), o None si no está configurada o no respondió bien."""
    global _nube, _nube_fallo_hasta
    cfg = _nube_config()
    if cfg is None:
        return None
    ahora = time.monotonic()
    if _nube is not None and ahora < _nube[0]:
        return _nube[1]
    if ahora < _nube_fallo_hasta:
        return None
    url, key = cfg
    propio = http is None
    http = http or httpx.AsyncClient(timeout=NUBE_TIMEOUT_S)
    try:
        r = await http.get(url, headers={"x-motor-key": key, "Accept": "application/json"})
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        if len(r.content) > NUBE_MAX_BYTES:
            raise ValueError("respuesta demasiado grande")
        data = r.json()
        local = _leer_archivo()
        heredar = {c.categoria: c for c in local.categorias} if local is not None else None
        cats = categorias_de(data, heredar)
        if not cats:
            raise ValueError("sin categorías usables")
        # La lista de la nube no puede achicar la protección: lo que le falte del archivo se toma del archivo, y el
        # mínimo de código rige siempre. Una respuesta 200 con una sola categoría ya no reemplaza todo.
        traia = {c.categoria for c in cats}
        cats = con_piso(cats, categorias=local.categorias if local is not None else (),
                        palabras=categorias_de(MINIMO))
        faltaban = [c.categoria for c in cats if c.categoria not in traia]
        if faltaban:
            log.warning("prohibidos: a la lista de la nube le faltan %d categorías (%s): se toman del archivo",
                        len(faltaban), ", ".join(faltaban)[:200])
        version = data.get("version") if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001  (red, timeout, JSON roto, lista vacía): va el archivo
        # Solo el tipo: el mensaje de httpx puede arrastrar la URL.
        log.warning("prohibidos: la nube no dio la lista (%s): va la del archivo", type(exc).__name__)
        _nube_fallo_hasta = time.monotonic() + NUBE_REINTENTO_S
        return None
    finally:
        if propio:
            await http.aclose()
    found = Lista(cats, FUENTE_NUBE, version if isinstance(version, int) else None)
    _nube = (time.monotonic() + NUBE_TTL_S, found)
    return found


async def lista(http: Any = None) -> Lista:
    """La lista vigente: nube → archivo → mínimo. Nunca lanza y nunca devuelve una lista vacía."""
    try:
        found = await _leer_nube(http)
    except Exception as exc:  # noqa: BLE001
        log.warning("prohibidos: la lectura de la nube falló (%s)", type(exc).__name__)
        found = None
    return found or lista_local()


def reiniciar() -> None:
    """Olvida las cachés (pruebas, o después de cambiar la configuración)."""
    global _archivo, _nube, _nube_fallo_hasta
    _archivo, _nube, _nube_fallo_hasta = None, None, 0.0
    _avisado.clear()


def estado(found: Lista) -> dict[str, Any]:
    """Para mostrar: de dónde salió la lista y cuánto trae (sin las palabras)."""
    return {"fuente": found.fuente, "version": found.version, "categorias": len(found.categorias),
            "completa": found.completa}
