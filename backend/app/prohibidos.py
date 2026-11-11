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

`prohibidos.yaml` es una COPIA de la lista canónica del radar de productos: no se edita a mano acá (se vuelve a copiar).

Con el mínimo (3) el filtro sigue frenando lo que conoce, pero la lista NO está completa (`Lista.completa` es False): las
activaciones automáticas se niegan hasta que vuelva una lista de verdad. Dos categorías no alcanzan para prender productos
sin que nadie mire.

Comparación (la misma lógica que el radar, con sus excepciones):

  * todo en minúsculas, sin tildes y con los separadores vueltos espacio;
  * español / inglés: PALABRA ENTERA («acido» no frena «placido», «crema» no frena «cremallera»); una frase de varias
    palabras tiene que estar entera;
  * chino: pedazo de texto (no se separa con espacios);
  * `excepto` DESACTIVA la categoría para ese producto: «pistola de silicona» es una encoladora, «molinillo de café» es un
    accesorio. Una palabra suelta ahí vale como comienzo de palabra («organizador» cubre «organizadores»).

`revisar` es pura (sin red ni base). `lista()` es la única que sale a la red.
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


# ─── Comparación (pura) ────────────────────────────────────────────


def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def normalizar(texto: str | None) -> str:
    """minúsculas, sin tildes, con los separadores vueltos espacio."""
    t = _sin_tildes(str(texto or "").lower())
    return re.sub(r"[^0-9a-z一-鿿]+", " ", t).strip()


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


_CJK = re.compile(r"[一-鿿]")


def _exceptuado(texto_norm: str, cat: Categoria) -> bool:
    """¿Alguna excepción desactiva la categoría para este producto? Frase de varias palabras: entera. Palabra suelta:
    alcanza con que una palabra del texto EMPIECE así. Chino: pedazo de texto."""
    espaciado = f" {texto_norm} "
    tokens = texto_norm.split()
    for e in cat.excepto:
        if not e:
            continue
        if _CJK.search(e):
            if e in texto_norm:
                return True
        elif " " in e:
            if f" {e} " in espaciado:
                return True
        elif any(t.startswith(e) for t in tokens):
            return True
    return False


def _matchea(texto_norm: str, cat: Categoria) -> str | None:
    if _exceptuado(texto_norm, cat):
        return None
    tokens = set(texto_norm.split())
    for p in cat.es:
        partes = p.split()
        if len(partes) == 1:
            if p in tokens:
                return p
        elif f" {p} " in f" {texto_norm} ":
            return p
    for p in cat.zh:
        if p and p in texto_norm:
            return p
    return None


def revisar(*textos: str | None, categorias: Iterable[Categoria] | None = None) -> Veredicto | None:
    """El primer motivo por el que este producto no se vende, o None. Mira todos los textos juntos (el nombre en español
    y el original en chino: uno dice cosas que el otro se come). Sin `categorias` usa la lista local (archivo o mínimo)."""
    cats = lista_local().categorias if categorias is None else categorias
    texto = normalizar(" ".join(str(t or "") for t in textos))
    if not texto:
        return None
    for cat in cats:
        palabra = _matchea(texto, cat)
        if palabra:
            return Veredicto(cat.categoria, cat.porque, palabra)
    return None


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
