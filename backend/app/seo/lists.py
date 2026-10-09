"""Listas editables de la auditoría de textos (marcas, relleno, datos técnicos).

Los valores de fábrica viven acá; una persona los puede cambiar desde el
dashboard sin redeploy. El cambio se guarda en la tabla `settings` (clave
`seo:lista:<nombre>`, un arreglo JSON) y pisa al default completo: la lista
guardada reemplaza a la de fábrica, no se suma. "Restablecer" borra la fila.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlmodel import Session

from app.clock import utcnow
from app.db.models import Setting

log = logging.getLogger(__name__)

# «One Piece» no está: es también el nombre de la malla/traje de baño enteriza (producto, no marca).
# «B2BOX» tampoco: tiene su propia regla informativa (MARCA_PROPIA).
# Marcas, personajes y nombres comerciales de terceros. Siembra: los casos de la
# sección 2.3 del diseño (iPhone, Kuromi, Guide, Let's Slim, Nespresso) más las
# marcas y personajes que más se cuelan en fichas de 1688. Se compara sin tildes
# ni mayúsculas y por palabra entera ("guide" no salta en "guiderail").
DEFAULT_BRANDS: tuple[str, ...] = (
    # De los casos reales del diseño
    "iPhone", "Kuromi", "Guide", "Let's Slim", "Nespresso",
    # Tecnología
    "Apple", "iPad", "AirPods", "MacBook", "Samsung", "Xiaomi", "Redmi", "Huawei",
    "Motorola", "Sony", "PlayStation", "Xbox", "Nintendo", "GoPro", "DJI", "Garmin",
    "Fitbit", "Lenovo", "Logitech", "Kingston", "SanDisk", "JBL", "Bose", "Alexa",
    "TikTok", "everyU", "Rosen",
    # Juguetes, personajes y licencias
    "Lego", "Barbie", "Hot Wheels", "Disney", "Marvel", "Spiderman",
    "Batman", "Superman", "Avengers", "Mickey", "Minnie", "Peppa Pig", "Paw Patrol",
    "Pokemon", "Pikachu", "Hello Kitty", "Sanrio", "Cinnamoroll", "My Melody",
    "Naruto", "Dragon Ball", "Minecraft", "Fortnite", "Roblox", "Otamatone",
    "Super Mario", "Star Wars", "Harry Potter",
    # Indumentaria y accesorios
    "Nike", "Adidas", "Reebok", "Crocs", "Havaianas", "Lacoste", "Ray-Ban",
    "Rolex", "Casio", "Swarovski", "Louis Vuitton", "Gucci", "Chanel",
    # Hogar, cocina y herramientas
    "Stanley", "Tramontina", "Dolce Gusto", "Philips", "Oster", "Black Decker",
    "Bosch", "DeWalt", "Makita", "Dyson", "Tupperware", "Thermos", "Hydro Flask",
    # Consumo masivo
    "Coca-Cola", "Pepsi", "Red Bull", "Gillette", "Colgate", "Pampers", "Nivea",
)
# Los sitios de proveedor (1688, Alibaba, AliExpress, Taobao...) no van acá: los
# cubre la regla FAB, que además compara contra los datos del propio producto.

# Frases y palabras de relleno de marketing. Salen de la sección 2.3 del diseño
# ("Iluminá tus espacios con magia", "según tu vibe", "súper práctico", "Tu oasis
# de confort"...). Se compara sin tildes ni mayúsculas, por palabra entera y con
# o sin "s" final.
DEFAULT_FILLER: tuple[str, ...] = (
    "oasis de confort", "con magia", "mágico", "mágica", "según tu", "vibe", "disfrutá",
    "disfrutalo", "iluminá tus", "descubrí", "descubre", "transformá", "súper",
    "increíble", "espectacular", "perfecto", "perfecta", "elegante",
    "divertido", "divertida", "eficiente", "versátil", "ambiente relajante",
    "sin límites", "en cualquier lugar", "el mejor", "la mejor", "premium",
    "revolucionario", "innovador",
    "práctico", "práctica", "practicidad", "comodidad", "elegancia", "cautivador",
    "cautivadora", "impactante", "novedoso", "novedosa", "sin igual",
    "solución ideal", "solución perfecta", "la solución", "tu solución",
)
# «elegante», «mágico» y «mágica» solo cuentan si encabezan el título («Elegante Reloj…»): en el
# medio suelen ser el producto o su tipo («Traje Elegante», «Cubo Mágico»). «Solución» a secas
# tampoco está: «solución salina» o «solución limpiadora» son productos.

# Datos técnicos que parecen código pero no lo son: se aceptan en mayúsculas en
# un título (regla COD). Aparte hay familias que se reconocen por patrón (IP67,
# SPF50, E27, M8, A4...), ver text_rules.TECH_PATTERN.
DEFAULT_TECHNICAL: tuple[str, ...] = (
    "LED", "USB", "COB", "3D", "UV", "UV400", "PVC", "ABS", "EVA", "PET", "TPU", "TPE",
    "PU", "PP", "PE", "HDMI", "LCD", "OLED", "TV", "PC", "GPS", "RGB", "NFC", "DIY",
    "SOS", "FM", "AM", "AA", "AAA", "XL", "XXL", "XS", "SMD", "DVD", "CD", "MP3",
    "MP4", "WIFI", "SIM", "SD", "SSD", "RAM", "LTE", "BLE", "OTG", "CPU", "GSM", "FPV",
    "VR", "HD", "FHD", "UHD", "HDR", "ECG", "SPF", "UPF", "RFID", "EAN", "QR", "TWS",
    "IR", "OBD", "ANC", "PWM",
    "IA", "AI", "VESA", "USBC", "USB-C", "SK5", "V8", "DPI", "PIR", "HSS", "SDS", "MDF", "BPA",
    "FDA", "BBQ", "ISO", "TPR", "BMX", "PD", "QC", "RC", "SPA", "DC", "B22", "HB", "ZIP", "PS4", "PS5",
)

LIST_NAMES: tuple[str, ...] = ("marcas", "relleno", "tecnicos")
_DEFAULTS: dict[str, tuple[str, ...]] = {
    "marcas": DEFAULT_BRANDS,
    "relleno": DEFAULT_FILLER,
    "tecnicos": DEFAULT_TECHNICAL,
}

MAX_ITEMS = 500
MAX_ITEM_LEN = 60
# Palabras (tiras de letras o de dígitos) de un término: la ventana de búsqueda crece con la
# más larga, así que un término de 30 palabras de una letra encarecería todo el recorrido.
MAX_TERM_WORDS = 6
_WORD_RE = re.compile(r"[^\W\d_]+|\d+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _key(name: str) -> str:
    return f"seo:lista:{name}"


@dataclass(frozen=True, slots=True)
class TextLists:
    """Las tres listas vigentes (inmutables: se arman una vez por corrida)."""

    brands: tuple[str, ...] = DEFAULT_BRANDS
    filler: tuple[str, ...] = DEFAULT_FILLER
    technical: tuple[str, ...] = DEFAULT_TECHNICAL


def defaults() -> dict[str, list[str]]:
    return {name: list(items) for name, items in _DEFAULTS.items()}


def sanitize_items(items: object) -> list[str]:
    """Normaliza lo que manda el dashboard: sin vacíos ni duplicados (sin
    distinguir mayúsculas), con tope de cantidad y de largo. Lanza ValueError con
    el motivo si algo no es válido."""
    if not isinstance(items, list):
        raise ValueError("La lista tiene que ser un arreglo de textos")
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("Cada elemento de la lista tiene que ser un texto")
        item = " ".join(raw.split())
        if not item:
            continue
        if _CONTROL_RE.search(raw.replace("\n", " ").replace("\t", " ")):
            raise ValueError("Hay caracteres de control en la lista")
        if len(item) > MAX_ITEM_LEN:
            raise ValueError(f"«{item[:20]}…» pasa de {MAX_ITEM_LEN} caracteres")
        if len(_WORD_RE.findall(item.replace("'", ""))) > MAX_TERM_WORDS:
            raise ValueError(f"«{item[:20]}…» tiene más de {MAX_TERM_WORDS} palabras")
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(item)
    if len(out) > MAX_ITEMS:
        raise ValueError(f"La lista pasa de {MAX_ITEMS} elementos")
    return out


def _stored(session: Session, name: str) -> list[str] | None:
    row = session.get(Setting, _key(name))
    if row is None:
        return None
    try:
        return sanitize_items(json.loads(row.value))
    except (TypeError, ValueError) as exc:
        log.warning("La lista %s guardada es inválida (%s): uso la de fábrica", name, exc)
        return None


def current(session: Session) -> dict[str, dict[str, object]]:
    """{nombre: {items, modified}} con lo vigente (lo guardado o la de fábrica)."""
    out: dict[str, dict[str, object]] = {}
    for name in LIST_NAMES:
        stored = _stored(session, name)
        out[name] = {
            "items": stored if stored is not None else list(_DEFAULTS[name]),
            "modified": stored is not None,
        }
    return out


def load(session: Session) -> TextLists:
    vigentes = current(session)
    return TextLists(
        brands=tuple(vigentes["marcas"]["items"]),  # type: ignore[arg-type]
        filler=tuple(vigentes["relleno"]["items"]),  # type: ignore[arg-type]
        technical=tuple(vigentes["tecnicos"]["items"]),  # type: ignore[arg-type]
    )


def save(session: Session, name: str, items: object) -> list[str]:
    if name not in LIST_NAMES:
        raise ValueError(f"No existe la lista «{name}»")
    clean = sanitize_items(items)
    row = session.get(Setting, _key(name))
    payload = json.dumps(clean, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=_key(name), value=payload))
    else:
        row.value = payload
        row.updated_at = utcnow()
        session.add(row)
    session.commit()
    return clean


def reset(session: Session, name: str) -> list[str]:
    if name not in LIST_NAMES:
        raise ValueError(f"No existe la lista «{name}»")
    row = session.get(Setting, _key(name))
    if row is not None:
        session.delete(row)
        session.commit()
    return list(_DEFAULTS[name])
