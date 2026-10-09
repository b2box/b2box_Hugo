"""Reglas de texto SIN IA para auditar los nombres y descripciones del catálogo.

Todo acá son funciones puras: reciben texto y listas, devuelven hallazgos. No
leen Vendure ni la base. Cada regla tiene un identificador estable (`RULES`) y,
cuando salta, un detalle corto en español para mostrar en el dashboard.

Sobre los datos del proveedor (`SupplierRefs`): se usan SOLO para comparar contra
los textos del mismo producto. El detalle de la regla FAB dice que hubo
coincidencia ("coincide con nombre de fábrica: sí"), nunca cuál fue el valor, y
los objetos que los guardan ocultan los valores en `repr`.
"""

from __future__ import annotations

import functools
import html
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.seo.lists import TextLists

# ─── Umbrales ──────────────────────────────────────────────────────

TITLE_MAX = 60            # el título de Google corta cerca de 60
META_MAX = 160            # largo máximo recomendado de una meta description
MIN_DESCRIPTION_CHARS = 20
# SLUG_NO_COINCIDE salta si, de las palabras con contenido, a lo sumo esta
# fracción del nombre está en la URL Y a lo sumo esta fracción de la URL está en
# el nombre. Un nombre acortado (todo en la URL) o ampliado (todo el slug en el
# nombre) no es un desacuerdo; uno reescrito de cero sí.
SLUG_MAX_OVERLAP = 0.5
# DUP_CASI: parecido mínimo (Jaccard sobre palabras con contenido) y mínimo de
# palabras con contenido en cada nombre para comparar.
DUP_CASI_JACCARD = 0.6
DUP_CASI_MIN_TOKENS = 3
DUP_MAX_LISTED = 5        # cuántos productos parecidos se nombran en el detalle


@dataclass(frozen=True, slots=True)
class RuleInfo:
    id: str
    label: str
    group: str
    help: str


RULES: tuple[RuleInfo, ...] = (
    RuleInfo("LARGO", "Título largo", "titulo",
             f"El nombre pasa de {TITLE_MAX} caracteres: Google lo corta."),
    RuleInfo("RELLENO", "Relleno de marketing", "titulo",
             "Frases que no aportan palabras de búsqueda («Iluminá tus espacios con magia», «súper práctico»)."),
    RuleInfo("COD", "Código o formato raro", "titulo",
             "Código de modelo (C64, H6S), sigla suelta (SG), cantidad pegada (x4u), medida con asterisco (98*56cm)."),
    RuleInfo("MAR", "Marca o personaje de terceros", "marcas",
             "Aparece una marca, personaje o nombre comercial de la lista (editable)."),
    RuleInfo("FAB", "Fábrica o código de proveedor", "marcas",
             "El texto coincide con el nombre de fábrica, el modelo o el link del proveedor de ESTE producto, o nombra un sitio de proveedor. No se muestra cuál."),
    RuleInfo("SLUG_NO_COINCIDE", "El nombre no coincide con la URL", "titulo",
             "Se reescribió el nombre y quedó la URL vieja (o al revés)."),
    RuleInfo("NOMBRE_ES_CODIGO", "El nombre es el código interno", "titulo",
             "El nombre es BX… o PA… (o el código del producto): falta un nombre real."),
    RuleInfo("ESPACIOS", "Espacios de más", "titulo",
             "Espacio al inicio o al final, espacios dobles o saltos de línea en el nombre."),
    RuleInfo("DUP_EXACTO", "Mismo nombre que otro producto", "duplicados",
             "Dos productos habilitados con el mismo nombre."),
    RuleInfo("DUP_CASI", "Casi igual a otro producto", "duplicados",
             "Nombre muy parecido al de otro producto habilitado: compiten entre sí en Google."),
    RuleInfo("SIN_DESCRIPCION", "Sin descripción", "descripcion",
             f"La descripción está vacía o tiene menos de {MIN_DESCRIPTION_CHARS} caracteres."),
    RuleInfo("DESC_CON_HTML_EN_META", "HTML o emojis en la meta description", "descripcion",
             "La ficha copia la descripción entera a la meta description: las etiquetas HTML y los emojis llegan a Google."),
    RuleInfo("META_LARGA", "Meta description demasiado larga", "descripcion",
             f"La descripción completa se copia a la meta description y pasa de {META_MAX} caracteres."),
    RuleInfo("SIN_ES_AR", "Falta la traducción es_AR", "traduccion",
             "El canal Argentina pide es_AR y Vendure no cae a otro idioma."),
)
RULE_IDS: tuple[str, ...] = tuple(r.id for r in RULES)
_RULE_ORDER = {rule_id: i for i, rule_id in enumerate(RULE_IDS)}


def sort_rules(rule_ids: Iterable[str]) -> list[str]:
    return sorted(rule_ids, key=lambda r: _RULE_ORDER.get(r, 999))


# ─── Normalización ─────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def fold(text: str | None) -> str:
    """Minúsculas, sin tildes, comillas rectas y espacios colapsados: la forma en
    que se comparan los textos."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'").replace("\u00b4", "'")
    return _WS_RE.sub(" ", t.casefold()).strip()


_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|li|ul|ol|div|h[1-6]|tr|td|table)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_HAS_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_HAS_ENTITY_RE = re.compile(r"&(?:[A-Za-z]{2,8}|#\d{1,6}|#[xX][0-9A-Fa-f]{1,6});")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u200D]"
)


def plain_text(raw: str | None) -> str:
    """Descripción HTML → texto plano (sin etiquetas, con entidades resueltas)."""
    t = _BLOCK_TAG_RE.sub(" ", raw or "")
    t = _TAG_RE.sub("", t)
    return _WS_RE.sub(" ", html.unescape(t)).strip()


def html_problems(raw: str | None) -> list[str]:
    """Qué de la descripción llega sucio a la meta description."""
    text = raw or ""
    out: list[str] = []
    if _HAS_TAG_RE.search(text):
        out.append("etiquetas HTML")
    if _HAS_ENTITY_RE.search(text):
        out.append("entidades HTML (&nbsp;, &amp;…)")
    if _EMOJI_RE.search(html.unescape(text)):
        out.append("emojis")
    return out


def normalize_lang(code: str | None) -> str:
    """`es-ar`, `ES_AR` → `es_AR`; el resto queda como viene (sin espacios)."""
    c = (code or "").strip()
    if re.fullmatch(r"(?i)es[_-]ar", c):
        return "es_AR"
    return c


# ─── Palabras con contenido (slug y duplicados) ────────────────────

_STOPWORDS = frozenset(
    "de del la las el los un una unos unas y e o u para con sin por en a al que se su sus "
    "tu tus mi mis lo como mas muy x".split()
)


def _stem(token: str) -> str:
    if len(token) > 5 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _content_tokens(parts: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if not p or p in _STOPWORDS:
            continue
        if len(p) == 1 and not p.isdigit():
            continue
        out.append(_stem(p))
    return out


def name_tokens(name: str | None) -> list[str]:
    return _content_tokens(re.findall(r"[a-z0-9]+", fold(name)))


def slug_tokens(slug: str | None) -> list[str]:
    parts = [p for p in re.split(r"[^a-z0-9]+", fold(slug)) if p]
    # Sufijo numérico que se agrega cuando el slug ya existía (`-184`).
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    return _content_tokens(parts)


# ─── Reglas sobre el nombre ────────────────────────────────────────

def check_largo(name: str) -> str | None:
    n = len(_WS_RE.sub(" ", name).strip())
    return f"{n} caracteres (tope {TITLE_MAX})" if n > TITLE_MAX else None


def check_espacios(name: str) -> str | None:
    if not name:
        return None
    problems: list[str] = []
    if name != name.strip():
        problems.append("espacio al inicio o al final")
    inner = name.strip()
    if re.search(r"[ \u00a0]{2,}", inner) or re.search(r"[\t\r\n\u00a0]", inner):
        problems.append("espacios dobles o saltos de línea")
    return ", ".join(problems) or None


_INTERNAL_CODE_NAME_RE = re.compile(r"^\s*(?:BX|PA)[\s\-_]?\d{2,}(?:[\s\-_]\d+)?\s*$", re.I)
_INTERNAL_CODE_IN_TEXT_RE = re.compile(r"(?<!\w)(?:BX|PA)\d{3,}(?!\w)", re.I)


def check_nombre_es_codigo(name: str, product_code: str | None) -> str | None:
    clean = name.strip()
    if not clean:
        return None
    if _INTERNAL_CODE_NAME_RE.match(clean):
        return f"el nombre es el código interno ({clean})"
    if product_code and fold(clean) == fold(product_code):
        return f"el nombre es el código del producto ({clean})"
    return None


@functools.lru_cache(maxsize=32)
def _compile_terms(terms: tuple[str, ...], plural: bool) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Cada término (ya sin tildes ni mayúsculas) → patrón de palabra entera. Los
    espacios y guiones del término aceptan cualquiera de los dos o ninguno
    («hello kitty» = «hello-kitty» = «hellokitty»)."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for display in terms:
        parts = [re.escape(p) for p in re.split(r"[\s\-]+", fold(display)) if p]
        if not parts:
            continue
        body = r"[\s\-]*".join(parts)
        tail = "s?" if plural else ""
        out.append((display, re.compile(rf"(?<![a-z0-9]){body}{tail}(?![a-z0-9])")))
    return tuple(out)


def check_relleno(name: str, lists: TextLists) -> str | None:
    folded = fold(name)
    hits: list[str] = []
    for display, pattern in _compile_terms(lists.filler, True):
        if pattern.search(folded) and display not in hits:
            hits.append(display)
    return ("; ".join(hits[:4]) + (f" (+{len(hits) - 4})" if len(hits) > 4 else "")) if hits else None


_COMPAT_PREFIX_RE = re.compile(r"(?:para|compatible con|compatible|apto para|apta para)\s+(?:el |la |un |una )?$")


def brand_hits(text: str, lists: TextLists) -> list[tuple[str, bool]]:
    """Marcas de la lista que aparecen en el texto: (marca, es_de_compatibilidad).
    «Funda para iPhone» es compatibilidad; «Funda iPhone Efecto Líquido» no."""
    folded = fold(text)
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for display, pattern in _compile_terms(lists.brands, True):
        m = pattern.search(folded)
        if not m or display in seen:
            continue
        seen.add(display)
        out.append((display, bool(_COMPAT_PREFIX_RE.search(folded[: m.start()]))))
    return out


def check_marcas(name: str, description_plain: str, lists: TextLists) -> str | None:
    parts: list[str] = []
    in_name = {b: compat for b, compat in brand_hits(name, lists)}
    in_desc = {b for b, _ in brand_hits(description_plain, lists)}
    for brand in list(in_name) + [b for b in sorted(in_desc) if b not in in_name]:
        where: list[str] = []
        if brand in in_name:
            where.append("título, solo como «para…»" if in_name[brand] else "título")
        if brand in in_desc:
            where.append("descripción")
        parts.append(f"{brand} ({' y '.join(where)})")
    return "; ".join(parts[:5]) + (f" (+{len(parts) - 5})" if len(parts) > 5 else "") if parts else None


# Códigos de modelo: 1 a 3 letras, 1 a 3 dígitos, una letra opcional (C64, H6S).
_MODEL_RE = re.compile(r"(?<!\w)([A-Z]{1,3}\d{1,3}[A-Z]?)(?!\w)")
# Con guion (ZK-7731, X-200): el código entero, no la sigla suelta.
_HYPHEN_CODE_RE = re.compile(r"(?<!\w)([A-Z]{1,4}-\d{1,5}[A-Z]?)(?!\w)")
_CAPS_RE = re.compile(r"(?<!\w)([A-Z]{2,4})(?!\w)")
_QTY_RE = re.compile(r"(?<!\w)x\d{1,3}(?:u|ud|uds|un|unid)(?!\w)", re.I)
_ASTERISK_RE = re.compile(r"\S*\*\S*")
_SCALE_RE = re.compile(r"(?<!\w)escala\s+(\d{2,})(?![\w:/])", re.I)
# Datos técnicos que parecen código y no lo son (no se editan: familias por patrón).
TECH_PATTERN = re.compile(
    r"^(?:IPX?\d{1,2}|USB\d?|UV\d{0,3}|SPF\d{1,3}|UPF\d{1,3}|N95|KN95|FFP[123]|[AB][0-6]|"
    r"E(?:14|27|40)|GU\d{1,2}|G\d{1,2}|T[1-9]|MR\d{2}|M\d{1,2}|H\d{1,2}|RJ\d{1,2}|LR\d{2,4}|"
    r"CR\d{1,4}|AG\d{1,2}|SR\d{2,3}|DC\d{1,3}V?|AC\d{1,3}V?|RGB\d?|HD\d{0,2}|MP\d|[2-5]?XL|"
    r"\d?[XS]{0,3}[SML])$"
)
_CAPS_STOPWORDS = frozenset(
    "DE LA EL EN CON SIN POR PARA LOS LAS UNA UNO MAS MUY II III IV VI VII VIII IX OK".split()
)


def _is_technical(token: str, technical: frozenset[str]) -> bool:
    up = token.upper()
    return up in technical or bool(TECH_PATTERN.match(up))


def _mostly_upper(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) >= 0.6


def check_cod(name: str, lists: TextLists) -> str | None:
    technical = frozenset(t.upper() for t in lists.technical)
    found: list[str] = []

    def add(label: str) -> None:
        if label not in found:
            found.append(label)

    masked = name
    for regex in (_HYPHEN_CODE_RE, _MODEL_RE):
        for m in regex.finditer(name):
            masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
            if not _is_technical(m.group(1).replace("-", ""), technical):
                add(m.group(1))
    if not _mostly_upper(name):
        for m in _CAPS_RE.finditer(masked):
            tok = m.group(1)
            if tok not in _CAPS_STOPWORDS and not _is_technical(tok, technical):
                add(tok)
    for m in _QTY_RE.finditer(name):
        add(f"{m.group(0)} (cantidad pegada)")
    for m in _ASTERISK_RE.finditer(name):
        add(f"{m.group(0)} (asterisco)")
    for m in _SCALE_RE.finditer(name):
        add(f"escala {m.group(1)} (¿1:{m.group(1)[-2:]}?)")
    if not _INTERNAL_CODE_NAME_RE.match(name.strip()):
        for m in _INTERNAL_CODE_IN_TEXT_RE.finditer(name):
            add(f"{m.group(0)} (código interno)")
    return ", ".join(found[:6]) + (f" (+{len(found) - 6})" if len(found) > 6 else "") if found else None


def check_slug(name: str, slug: str) -> str | None:
    n_tokens = set(name_tokens(name))
    s_tokens = set(slug_tokens(slug))
    if not n_tokens or not s_tokens:
        return None
    common = len(n_tokens & s_tokens)
    if common / len(n_tokens) <= SLUG_MAX_OVERLAP and common / len(s_tokens) <= SLUG_MAX_OVERLAP:
        return f"comparten {common} de {len(n_tokens)} palabras del nombre (URL: {slug.strip()[:70]})"
    return None


# ─── Datos del proveedor (regla FAB) ───────────────────────────────

@dataclass(frozen=True, slots=True)
class SupplierRefs:
    """Los tres campos de proveedor del producto. Los valores no salen en `repr`."""

    business: str | None = field(default=None, repr=False)
    size_model: str | None = field(default=None, repr=False)
    link: str | None = field(default=None, repr=False)

    def is_empty(self) -> bool:
        return not any((v or "").strip() for v in (self.business, self.size_model, self.link))


# Palabras de un nombre de empresa que no la distinguen (forma legal, rubros,
# ciudades fabriles, adjetivos comerciales): no sirven para reconocerla.
_BUSINESS_GENERIC = frozenset(
    "co ltd limited llc inc corp corporation company gmbh sa srl sl pte pvt factory trading "
    "technology technologies tech industry industrial industries electronic electronics "
    "commerce ecommerce import export imp exp manufacturing manufacture mfg group "
    "international intl enterprise enterprises store shop online china shenzhen guangzhou "
    "yiwu ningbo hangzhou dongguan foshan shanghai zhejiang guangdong city province the and "
    "of product products home household kitchen garden toy toys gift gifts craft crafts "
    "fashion beauty pet pets auto sport sports outdoor led lighting light plastic metal "
    "silicone glass paper packaging pack hardware tool tools apparel garment textile bag "
    "bags shoes digital smart global world new great best good super golden gold star sun "
    "dragon sky".split()
)
_HOST_GENERIC = frozenset(
    "www detail m item items shop store wap s world es pt ru com cn net org alibaba aliexpress "
    "taobao tmall offer product products page html htm made in china".split()
)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
# Lo que en un nombre de empresa china es forma legal, rubro o ciudad: no la distingue.
_CJK_GENERIC = (
    "股份有限公司", "有限责任公司", "有限公司", "电子商务", "贸易", "商贸", "科技", "实业", "工贸",
    "公司", "义乌市", "深圳市", "广州市", "东莞市", "宁波市", "杭州市", "佛山市", "上海市",
)
_MEASURE_RE = re.compile(
    r"^\d+(?:[.,]\d+)?(?:mm|cm|m|kg|g|mg|ml|l|lt|w|kw|v|a|ma|mah|pcs|pc|pzs|u|un|und|uds|ud|"
    r"in|oz|lb|hz|khz|mhz|ghz|gb|mb|tb|k|x|h|min|s)$",
    re.I,
)
_SUPPLIER_SITE_RE = re.compile(
    r"(?<![a-z0-9])(?:1688|alibaba|aliexpress|taobao|tmall|dhgate|made-in-china)(?![a-z0-9])"
)


def _word_pattern(folded_phrase: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.findall(r"[a-z0-9]+", folded_phrase)]
    return re.compile(r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(parts) + r"(?![a-z0-9])")


def _business_needles(value: str) -> tuple[list[re.Pattern[str]], list[str]]:
    """(patrones sobre texto plegado, tiras CJK) que delatan al nombre de fábrica."""
    folded = fold(value)
    tokens = re.findall(r"[a-z0-9]+", folded)
    distinctive = [t for t in tokens if t not in _BUSINESS_GENERIC and not t.isdigit() and len(t) > 1]
    patterns: list[re.Pattern[str]] = []
    if len(distinctive) >= 2:
        patterns.append(_word_pattern(" ".join(distinctive)))
        for a, b in zip(distinctive, distinctive[1:]):
            patterns.append(_word_pattern(f"{a} {b}"))
    for t in distinctive:
        if len(t) >= 6 or (len(distinctive) == 1 and len(t) >= 4):
            patterns.append(_word_pattern(t))
    if not patterns and not distinctive and len(folded) >= 6 and re.search(r"[a-z]", folded):
        patterns.append(_word_pattern(folded))
    cjk = value
    for generic in _CJK_GENERIC:
        cjk = cjk.replace(generic, " ")
    return patterns, _CJK_RUN_RE.findall(cjk)


def _model_needles(value: str, technical: frozenset[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for piece in re.split(r"[\s,;/|:()\[\]\u3001\uff0c\uff1b]+", value):
        piece = piece.strip(".-_")
        if len(piece) < 2 or len(piece) > 30 or _MEASURE_RE.match(piece):
            continue
        if _is_technical(piece, technical):
            continue
        has_digit = any(c.isdigit() for c in piece)
        has_alpha = any(c.isalpha() for c in piece)
        if has_digit and has_alpha:
            body = r"[\s\-_]?".join(re.escape(p) for p in re.split(r"[\-_]", piece) if p)
            out.append(re.compile(rf"(?<!\w){body}(?!\w)", re.I))
        elif piece.isalpha() and piece.isupper() and piece.isascii() and 2 <= len(piece) <= 6:
            out.append(re.compile(rf"(?<!\w){re.escape(piece)}(?!\w)"))
    return out


def _link_needles(value: str) -> tuple[list[re.Pattern[str]], list[str]]:
    """(patrones sobre texto plegado, subcadenas literales) del link del proveedor."""
    v = value.strip()
    if not v:
        return [], []
    try:
        parts = urlsplit(v if "//" in v else "//" + v)
        host = (parts.hostname or "").lower()
    except ValueError:
        return [], []
    patterns: list[re.Pattern[str]] = []
    for ident in dict.fromkeys(re.findall(r"\d{8,}", f"{parts.path} {parts.query}")):
        patterns.append(re.compile(rf"(?<!\d){ident}(?!\d)"))
    for label in host.split("."):
        if label and label not in _HOST_GENERIC and len(label) >= 5 and not label.isdigit():
            patterns.append(_word_pattern(label))
    literals: list[str] = []
    bare = fold(v)
    bare = re.sub(r"^[a-z]+://", "", bare)
    if len(bare) >= 12:
        literals.append(bare)
    return patterns, literals


class SupplierMatcher:
    """Compara textos contra los datos del proveedor de UN producto. Guarda solo
    patrones compilados; nada de esto se persiste ni se loguea."""

    __slots__ = ("_biz", "_cjk", "_models", "_link_pat", "_link_lit")

    def __init__(self, refs: SupplierRefs | None, technical: frozenset[str] = frozenset()) -> None:
        refs = refs or SupplierRefs()
        self._biz, self._cjk = _business_needles(refs.business) if (refs.business or "").strip() else ([], [])
        self._models = _model_needles(refs.size_model, technical) if (refs.size_model or "").strip() else []
        self._link_pat, self._link_lit = _link_needles(refs.link) if (refs.link or "").strip() else ([], [])

    def __repr__(self) -> str:  # nunca los valores
        return "SupplierMatcher(…)"

    def _cjk_in(self, text: str) -> bool:
        """¿Aparece el nombre chino de la fábrica, entero o en un tramo de 3 o más caracteres?"""
        if not self._cjk:
            return False
        if any(run in text for run in self._cjk):
            return True
        return any(len(t) >= 3 and any(t in run for run in self._cjk) for t in _CJK_RUN_RE.findall(text))

    def kinds_in(self, text: str) -> set[str]:
        """Qué tipos de dato del proveedor aparecen en `text`: {fabrica, codigo, link}."""
        if not text:
            return set()
        folded = fold(text)
        kinds: set[str] = set()
        if any(p.search(folded) for p in self._biz) or self._cjk_in(text):
            kinds.add("fabrica")
        if any(p.search(text) for p in self._models):
            kinds.add("codigo")
        if any(p.search(folded) for p in self._link_pat) or any(lit in folded for lit in self._link_lit):
            kinds.add("link")
        return kinds


_KIND_LABEL = {
    "fabrica": "coincide con nombre de fábrica: sí",
    "codigo": "coincide con código de proveedor: sí",
    "link": "coincide con link de proveedor: sí",
}
_PLACE_ORDER = ("título", "URL", "descripción")


def check_proveedor(name: str, slug: str, description_plain: str, matcher: SupplierMatcher) -> str | None:
    by_kind: dict[str, list[str]] = {}
    for place, text in (("título", name), ("URL", slug), ("descripción", description_plain)):
        for kind in matcher.kinds_in(text):
            by_kind.setdefault(kind, []).append(place)
    parts = [
        f"{_KIND_LABEL[kind]} ({', '.join(sorted(places, key=_PLACE_ORDER.index))})"
        for kind, places in sorted(by_kind.items(), key=lambda kv: list(_KIND_LABEL).index(kv[0]))
    ]
    folded = fold(f"{name} {description_plain}")
    if _SUPPLIER_SITE_RE.search(folded):
        parts.append("menciona un sitio de proveedor")
    return "; ".join(parts) or None


# ─── Descripción y meta description ────────────────────────────────

def check_descripcion(description_raw: str) -> dict[str, str]:
    """SIN_DESCRIPCION, DESC_CON_HTML_EN_META y META_LARGA."""
    out: dict[str, str] = {}
    plain = plain_text(description_raw)
    if len(plain) < MIN_DESCRIPTION_CHARS:
        out["SIN_DESCRIPCION"] = "sin texto" if not plain else f"solo {len(plain)} caracteres"
        return out
    problems = html_problems(description_raw)
    if problems:
        out["DESC_CON_HTML_EN_META"] = (
            "trae " + " y ".join(problems) + ": la ficha lo copia tal cual a la meta description"
        )
    if len(plain) > META_MAX:
        out["META_LARGA"] = f"{len(plain)} caracteres (Google muestra unos {META_MAX})"
    return out


# ─── Todo junto, por traducción ────────────────────────────────────

def audit_translation(
    name: str | None,
    slug: str | None,
    description: str | None,
    *,
    product_code: str | None,
    lists: TextLists,
    matcher: SupplierMatcher | None = None,
) -> dict[str, str]:
    """Reglas que se evalúan con los textos de UNA traducción de UN producto.
    Devuelve {regla: detalle} en el orden de `RULES`. Las que comparan entre
    productos (DUP_*) y SIN_ES_AR las agrega quien llama."""
    n = name or ""
    s = slug or ""
    desc_plain = plain_text(description)
    found: dict[str, str] = {}

    def put(rule: str, detail: str | None) -> None:
        if detail:
            found[rule] = detail

    if n.strip():
        put("LARGO", check_largo(n))
        put("RELLENO", check_relleno(n, lists))
        put("NOMBRE_ES_CODIGO", check_nombre_es_codigo(n, product_code))
        put("ESPACIOS", check_espacios(n))
        put("COD", check_cod(n.strip(), lists))
        put("SLUG_NO_COINCIDE", check_slug(n, s))
    put("MAR", check_marcas(n, desc_plain, lists))
    put("FAB", check_proveedor(n, s, desc_plain, matcher or SupplierMatcher(None)))
    found.update(check_descripcion(description or ""))
    return {rule: found[rule] for rule in sort_rules(found)}


def missing_es_ar_detail(languages: Iterable[str]) -> str:
    langs = sorted({normalize_lang(x) for x in languages if x})
    return f"sin traducción es_AR (tiene: {', '.join(langs)})" if langs else "sin ninguna traducción"


# ─── Duplicados entre productos ────────────────────────────────────

def _name_key(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold(name)))


def id_sort_key(pid: str) -> tuple[int, int | str]:
    return (0, int(pid)) if pid.isdigit() else (1, pid)


def _listed(ids: list[str]) -> str:
    ids = sorted(set(ids), key=id_sort_key)
    shown = ", ".join(ids[:DUP_MAX_LISTED])
    return shown + (f" (+{len(ids) - DUP_MAX_LISTED})" if len(ids) > DUP_MAX_LISTED else "")


def _prefix_len(size: int) -> int:
    """Largo del prefijo a indexar para un nombre de `size` palabras: dos nombres
    con Jaccard >= DUP_CASI_JACCARD comparten al menos una palabra de sus prefijos."""
    return size - math.ceil(DUP_CASI_JACCARD * size - 1e-9) + 1


def find_duplicates(entries: Iterable[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    """`entries`: (clave_de_fila, id_de_producto, nombre) de UN idioma, solo de
    productos habilitados. Devuelve {clave_de_fila: {DUP_EXACTO|DUP_CASI: detalle}}.

    DUP_CASI usa Jaccard sobre palabras con contenido y el filtro por prefijo
    (se indexa solo lo imprescindible de las palabras más raras): exacto, sin
    comparar todos contra todos."""
    rows = [(rk, pid, name) for rk, pid, name in entries if (name or "").strip()]
    out: dict[str, dict[str, str]] = {}

    # Exactos: mismo nombre normalizado en productos distintos.
    by_key: dict[str, list[tuple[str, str]]] = {}
    for rk, pid, name in rows:
        key = _name_key(name)
        if len(key) >= 4:
            by_key.setdefault(key, []).append((rk, pid))
    exact_key_of: dict[str, str] = {}
    for key, members in by_key.items():
        pids = {pid for _, pid in members}
        if len(pids) < 2:
            continue
        for rk, pid in members:
            exact_key_of[rk] = key
            others = [p for _, p in members if p != pid]
            out.setdefault(rk, {})["DUP_EXACTO"] = f"mismo nombre que el producto {_listed(others)}"

    # Casi iguales.
    sets: list[tuple[str, str, frozenset[str]]] = []
    for rk, pid, name in rows:
        toks = frozenset(name_tokens(name))
        if len(toks) >= DUP_CASI_MIN_TOKENS:
            sets.append((rk, pid, toks))
    df: dict[str, int] = {}
    for _, _, toks in sets:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    index: dict[str, list[int]] = {}
    ordered: list[list[str]] = []
    for i, (_, _, toks) in enumerate(sets):
        ranked = sorted(toks, key=lambda t: (df[t], t))
        ordered.append(ranked)
        for t in ranked[:_prefix_len(len(ranked))]:
            index.setdefault(t, []).append(i)
    similar: dict[int, dict[int, float]] = {}
    for i, (rk_i, pid_i, toks_i) in enumerate(sets):
        candidates: set[int] = set()
        for t in ordered[i][:_prefix_len(len(ordered[i]))]:
            candidates.update(index.get(t, ()))
        for j in candidates:
            if j <= i:
                continue
            rk_j, pid_j, toks_j = sets[j]
            if pid_i == pid_j:
                continue
            if exact_key_of.get(rk_i) is not None and exact_key_of.get(rk_i) == exact_key_of.get(rk_j):
                continue
            jac = len(toks_i & toks_j) / len(toks_i | toks_j)
            if jac >= DUP_CASI_JACCARD:
                similar.setdefault(i, {})[j] = jac
                similar.setdefault(j, {})[i] = jac
    for i, neighbours in similar.items():
        rk, _, _ = sets[i]
        best = max(neighbours.values())
        ids = [sets[j][1] for j in neighbours]
        out.setdefault(rk, {})["DUP_CASI"] = f"parecido al producto {_listed(ids)} (hasta {round(best * 100)} %)"
    return out
