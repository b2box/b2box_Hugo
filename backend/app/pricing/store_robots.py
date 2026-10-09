"""robots.txt de una tienda, como lo lee un bot que quiere portarse bien.

`urllib.robotparser` no sirve acá: ignora los comodines (`*`) y el ancla (`$`)
que usan las tiendas de verdad (`Disallow: /*?`, `Disallow: /*?*srsltid=*`),
así que dejaría pasar justo las URLs que la tienda prohíbe. Esta versión sigue
la regla de Google / RFC 9309:

  * se elige el grupo cuyo `User-agent` coincide con nuestro token ("hugopricebot");
    si no hay, el grupo `*`. Varias líneas `User-agent` seguidas comparten reglas;
  * dentro del grupo manda la regla MÁS LARGA que coincide con el path (más la
    query); si empatan, gana Allow;
  * `*` es cualquier cosa y `$` ancla el final del patrón;
  * `Disallow:` vacío no prohíbe nada.

Sin red: parsea texto. El llamador decide qué hacer si no pudo bajar el archivo
(ver `from_status`): 404/410 = todo permitido, 5xx o sin respuesta = nada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Google lee los primeros 500 KiB: lo que viene después se ignora.
MAX_ROBOTS_BYTES = 500 * 1024
# Un robots.txt razonable tiene decenas de reglas; esto frena uno malicioso.
MAX_RULES = 2000
_MAX_PATTERN_LEN = 400
# Un Crawl-delay absurdo no puede dejar al indexador esperando horas.
MAX_CRAWL_DELAY_S = 30.0


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str
    # El patrón partido por `*` (sin el `$` final) y si exigía terminar ahí. Se compara con
    # `str.startswith` / `str.find`, nunca con una regex: `/*a*a*a*a*b` contra una URL larga
    # tardaba segundos en el event loop (cada `*` era un `.*` y el motor retrocede sin fin).
    parts: tuple[str, ...]
    anchored: bool

    @property
    def length(self) -> int:
        return len(self.pattern)

    def matches(self, target: str) -> bool:
        """¿El path (con su query) cae bajo esta regla? Anclado al inicio; con `$`, también al final."""
        parts = self.parts
        if len(parts) == 1:
            return target == parts[0] if self.anchored else target.startswith(parts[0])
        if not target.startswith(parts[0]):
            return False
        pos = len(parts[0])
        for middle in parts[1:-1]:
            at = target.find(middle, pos)
            if at < 0:
                return False
            pos = at + len(middle)
        last = parts[-1]
        if self.anchored:
            return len(target) - len(last) >= pos and target.endswith(last)
        return target.find(last, pos) >= 0


@dataclass(frozen=True, slots=True)
class Robots:
    rules: tuple[Rule, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()
    # "all" = no se pudo confirmar nada y se prohíbe todo (robots caído, 5xx).
    blocked_all: bool = False

    def allows(self, url_or_path: str) -> bool:
        """¿Puede un bot pedir esta URL (o este `path?query`)?"""
        if self.blocked_all:
            return False
        target = _path_and_query(url_or_path)
        best: Rule | None = None
        for rule in self.rules:
            if not rule.matches(target):
                continue
            if best is None or rule.length > best.length or (rule.length == best.length and rule.allow):
                best = rule
        return True if best is None else best.allow


def _path_and_query(url_or_path: str) -> str:
    raw = (url_or_path or "").strip()
    if "://" in raw:
        parts = urlsplit(raw)
        raw = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return raw if raw.startswith("/") else "/" + raw


def _compile(allow: bool, pattern: str) -> Rule:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return Rule(allow, pattern, tuple(body.split("*")), anchored)


def allow_all() -> Robots:
    return Robots()


def disallow_all() -> Robots:
    return Robots(blocked_all=True)


# 401/403 (no nos dejan leerlo) y 429 (nos pidieron frenar) no son «no existe»: no se rastrea.
_NO_CRAWL_STATUSES = frozenset({401, 403, 429})


def from_status(status: int | None, text: str = "", agent: str = "HugoPriceBot") -> Robots:
    """Qué hacer con la respuesta de `/robots.txt` (RFC 9309): 2xx se parsea; 404 y demás 4xx
    (no existe) = sin restricciones; 401/403/429, 5xx o sin respuesta = no se rastrea nada."""
    if status is not None and 200 <= status < 300:
        return parse(text, agent)
    if status is not None and 400 <= status < 500 and status not in _NO_CRAWL_STATUSES:
        return allow_all()
    return disallow_all()


# Token de producto: letras al principio y después nada, o un separador (`/1.0`, ` (+url)`, `;`). `HugoPriceBot2` no.
_TOKEN_RE = re.compile(r"([a-z_-]+)(?:[/\s(;].*)?$", re.S)


def _agent_matches(declared: str, token: str) -> int:
    """Especificidad del grupo para nosotros: 0 = no aplica, 1 = `*`, 2 = nuestro token. Se compara el
    token de producto de lo declarado (sus letras iniciales: `HugoPriceBot/1.0` y `HugoPriceBot (+url)`
    son `hugopricebot`) por igualdad, no por «contiene»: `User-agent: o` no es nuestro."""
    declared = declared.strip().lower()
    if declared == "*":
        return 1
    m = _TOKEN_RE.match(declared)
    return 2 if m and m.group(1) == token else 0


def parse(text: str, agent: str = "HugoPriceBot") -> Robots:
    """Texto de robots.txt → reglas que valen para `agent`."""
    # "HugoPriceBot/1.0 (+https://…)" → "hugopricebot": el token de producto del User-Agent.
    m = _TOKEN_RE.match(agent.strip().lower())
    token = m.group(1) if m else ""
    groups: list[tuple[list[str], list[tuple[bool, str]], list[float]]] = []
    sitemaps: list[str] = []
    total_rules = 0
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    delays: list[float] = []
    reading_agents = False

    for line in (text or "")[:MAX_ROBOTS_BYTES].splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not reading_agents:
                agents, rules, delays = [], [], []
                groups.append((agents, rules, delays))
                reading_agents = True
            agents.append(value)
        elif field in ("allow", "disallow"):
            reading_agents = False
            # Tope por grupo y TOTAL: un robots con miles de grupos de una regla no puede crecer sin techo.
            if groups and len(rules) < MAX_RULES and total_rules < MAX_RULES and len(value) <= _MAX_PATTERN_LEN:
                rules.append((field == "allow", value))
                total_rules += 1
        elif field == "crawl-delay":
            reading_agents = False
            try:
                delays.append(float(value))
            except ValueError:
                pass
        elif field == "sitemap" and value:
            sitemaps.append(value)

    best = 0
    for declared, _, _ in groups:
        best = max(best, max((_agent_matches(a, token) for a in declared), default=0))
    chosen_rules: list[tuple[bool, str]] = []
    chosen_delays: list[float] = []
    if best:
        for declared, group_rules, group_delays in groups:
            if max((_agent_matches(a, token) for a in declared), default=0) == best:
                chosen_rules += group_rules
                chosen_delays += group_delays

    compiled = tuple(
        _compile(allow, pattern)
        for allow, pattern in chosen_rules
        if pattern  # `Disallow:` vacío = nada prohibido; `Allow:` vacío no dice nada
    )
    delay = min(max(chosen_delays), MAX_CRAWL_DELAY_S) if chosen_delays else None
    return Robots(rules=compiled, crawl_delay=delay, sitemaps=tuple(sitemaps))
