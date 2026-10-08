"""Renderizar una URL en un browser real cuando el fetch plano no alcanza.

`image_from_url.extract()` pide el HTML con httpx y lo parsea. Eso falla en dos
casos que son justo los más frecuentes:

  - **Anti-bot**: MercadoLibre y Alibaba devuelven su interstitial de "tráfico
    sospechoso" a cualquier request que salga de una IP de datacenter. HTTP 200,
    sin og:image ni JSON-LD → hoy termina en `site_blocked` y el app le pide una
    foto al cliente.
  - **Galería por JS**: 1688 y AliExpress arman las fotos desde JavaScript. El
    HTML plano no las trae y el regex sobre el CDN pesca lo que puede.

Camoufox es Firefox parcheado para no declararse automatizado (fingerprint,
canvas, WebGL, navigator.*). Levantamos la página de verdad, dejamos que corra
el JS, y leemos las fotos del DOM ya renderizado.

Degrada elegante, igual que `image_embed`: si camoufox no está instalado o
`BROWSER_FETCH_ENABLED=false`, `available()` devuelve False y `extract()` sigue
con el camino de siempre. El backend arranca lo mismo sin el paquete.

## Seguridad

La URL viene de un usuario final. Un browser real es MUCHO más peligroso que un
httpx: sigue redirects solo, carga subrecursos, ejecuta JS que puede hacer fetch.
`safe_get` no protege nada de eso. Entonces:

  - `assert_public_url` sobre la URL de entrada (scheme + DNS → IP pública).
  - `page.route("**")`: CADA request que el browser intenta —navegación,
    redirect, XHR, imagen, iframe— se valida contra el mismo guard y se aborta
    si apunta a una red interna. Es el control que hace segura la navegación,
    no el chequeo de entrada.
  - Sin `file://`, sin `data:` de navegación, sin descargas.

## Dos formas de usarlo

  - `render(url)`: UNA ficha, browser nuevo por llamada (lo que usan /verify y
    /app/lookup mientras un cliente espera).
  - `ListingBrowser`: UN browser para MUCHAS páginas de listado seguidas (el
    semáforo busca ~1.000 títulos por noche en la web de ML). Mismo lanzamiento
    (`_launch_kwargs`: headless, humanize, geoip, BROWSER_PROXY), mismo guard
    anti-SSRF (`_install_guard`) y mismo `available()`; suma el corte de
    imágenes/fuentes/media (y de scripts y estilos) y el conteo de bytes, porque
    cada byte pasa por un proxy residencial que se paga por GB.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

from app.config import get_settings
from app.net_guard import SsrfBlocked, assert_public_url
from app.net_guard import _ip_is_public  # el mismo criterio que el resto de Hugo (CGNAT, mapeadas, site-local…)

log = logging.getLogger(__name__)

# Schemes que el browser puede pedir. Todo lo demás se aborta.
_ALLOWED_SCHEMES = {"http", "https"}
# about:blank y blob:/data: internos de la página no son navegación a red;
# dejarlos pasar evita romper páginas legítimas sin abrir superficie.
_PASSTHROUGH_SCHEMES = {"about", "blob", "data"}

# Cache de resolución DNS por host, para no pagar un getaddrinfo por subrecurso
# (una ficha de ML dispara 100+ requests a 4-5 hosts). Con vencimiento: un host
# que hoy resuelve a una IP pública puede dejar de hacerlo (rebinding), y uno que
# dio una IP privada puede arreglarse. Positivos 5 min, negativos 30 s, tope de
# tamaño, y un fallo de DNS NO se cachea (es transitorio: no hay que castigar
# 30 s a un host por un timeout de resolución).
DNS_TTL_PUBLIC_S = 300.0
DNS_TTL_PRIVATE_S = 30.0
DNS_CACHE_MAX = 512
_host_cache: dict[str, tuple[bool, float]] = {}


def _dns_cached(host: str) -> bool | None:
    entry = _host_cache.get(host)
    if entry is None:
        return None
    ok, expires = entry
    if time.monotonic() >= expires:
        _host_cache.pop(host, None)
        return None
    return ok


def _dns_store(host: str, ok: bool) -> None:
    if len(_host_cache) >= DNS_CACHE_MAX:
        now = time.monotonic()
        for h in [h for h, (_, exp) in _host_cache.items() if exp <= now]:
            _host_cache.pop(h, None)
        while len(_host_cache) >= DNS_CACHE_MAX:
            _host_cache.pop(next(iter(_host_cache)))    # el más viejo (el dict conserva el orden)
    _host_cache[host] = (ok, time.monotonic() + (DNS_TTL_PUBLIC_S if ok else DNS_TTL_PRIVATE_S))


def reset_dns_cache() -> None:
    _host_cache.clear()


@dataclass(slots=True)
class RenderedPage:
    html: str = ""
    # Fotos del DOM renderizado, en orden de aparición.
    image_urls: list[str] = field(default_factory=list)
    title: str = ""
    final_url: str = ""
    # Requests abortados por el guard. Vacío es lo normal; con algo adentro
    # conviene mirar qué pidió esa página.
    blocked: list[str] = field(default_factory=list)


class BrowserUnavailable(RuntimeError):
    """Camoufox no está instalado, está deshabilitado o no pudo cargar la página.

    El mensaje pasa por `redact`: termina en logs, en `WebSearch.reason` y en
    columnas de la DB, y un error de lanzamiento puede repetir el proxy."""

    def __init__(self, message: object = "", *args: object) -> None:
        super().__init__(redact(message), *args)


def _camoufox():
    """Import perezoso: el paquete es opcional y pesa (Firefox + fingerprints)."""
    from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

    return AsyncCamoufox


def ensure_browser_installed() -> bool:
    """Si el Firefox de Camoufox no está en la imagen, lo baja acá (fail-soft).

    El Dockerfile lo intenta con `camoufox fetch`, pero la descarga pasa por la
    API de GitHub, que limita por IP, y el deploy del 07-oct-2026 se cayó por
    eso. El build ya no falla: si faltó el binario, se completa al arrancar.
    Nunca tira. Devuelve True si el binario quedó disponible.
    """
    try:
        from camoufox import pkgman  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.debug("camoufox no importable, no hay nada que instalar: %s", exc)
        return False
    try:
        pkgman.launch_path(pkgman.camoufox_path(download_if_missing=False))
        return True
    except Exception:  # noqa: BLE001
        pass
    log.warning("Camoufox no está instalado en la imagen: descargando al arrancar")
    try:
        pkgman.camoufox_path(download_if_missing=True)
        pkgman.launch_path()
    except Exception as exc:  # noqa: BLE001
        log.warning("no pude descargar Camoufox (el render por browser queda apagado): %s", exc)
        return False
    log.info("Camoufox descargado y listo")
    return True


_PROXY_SCHEMES = ("http", "https", "socks4", "socks5")


def _proxy_config() -> dict[str, str] | None:
    """Traduce settings.browser_proxy al dict que espera Camoufox/Playwright.

    Playwright quiere el server SIN credenciales embebidas y user/pass en campos
    aparte: "http://u:p@host:port" embebido no siempre autentica. Usuario y
    clave se decodifican (en una URL los caracteres especiales van con %XX).

    Devuelve None si no hay proxy o si está mal formado (clave con "/", "#" o
    "?" sin codificar, URL sin esquema, puerto raro). NUNCA loguea ni lanza con
    el valor: lleva usuario y clave, y `urlparse(...).port` tira ValueError.
    """
    raw = (get_settings().browser_proxy or "").strip()
    if not raw:
        return None
    try:
        p = urlparse(raw)
        scheme, host, port = (p.scheme or "").lower(), p.hostname, p.port
        username, password = p.username, p.password
    except ValueError:
        log.warning("BROWSER_PROXY mal formado (%d caracteres): se ignora", len(raw))
        return None
    if not host or scheme not in _PROXY_SCHEMES:
        log.warning("BROWSER_PROXY mal formado (%d caracteres): se ignora", len(raw))
        return None
    server = f"{scheme}://{host}" if ":" not in host else f"{scheme}://[{host}]"
    if port:
        server += f":{port}"
    cfg: dict[str, str] = {"server": server}
    if username:
        cfg["username"] = unquote(username)
    if password:
        cfg["password"] = unquote(password)
    return cfg


def proxy_problem() -> str | None:
    """Por qué hay un BROWSER_PROXY pero no sirve ("mal formado"), o None si no
    hay nada que decir (sin configurar o válido). Sin el valor."""
    if (get_settings().browser_proxy or "").strip() and _proxy_config() is None:
        return "BROWSER_PROXY mal formado (revisá el formato: http://usuario:clave@host:puerto, con la clave codificada)"
    return None


def _secret_tokens() -> list[str]:
    """Todo lo que identifica al proxy y no tiene que aparecer en un mensaje:
    el valor entero, usuario, clave y host (también en su forma codificada)."""
    raw = (get_settings().browser_proxy or "").strip()
    if not raw:
        return []
    found: set[str] = {raw}
    try:
        p = urlparse(raw)
        for part in (p.username, p.password, p.hostname):
            if part:
                found.update({part, unquote(part), quote(part, safe="")})
        if p.hostname and p.port:
            found.add(f"{p.hostname}:{p.port}")
    except ValueError:
        # Mal formado (clave con "/"): lo que hay entre "://" y la última "@"
        # es credencial, y lo de después es el host.
        rest = raw.split("://", 1)[-1]
        creds, _, hostpart = rest.rpartition("@")
        found.update(t for t in (creds, hostpart, *creds.split(":")) if t)
        found.update(hostpart.split(":")[0:1])
    return sorted((t for t in found if len(t) >= 3), key=len, reverse=True)


def redact(text: object) -> str:
    """Tapa usuario, clave y host de BROWSER_PROXY en un texto (mensajes de
    error, motivos que se guardan en la DB, logs). Un error de Playwright puede
    repetir el proxy con el que se lanzó."""
    out = str(text)
    for token in _secret_tokens():
        out = out.replace(token, "***")
    return out


def _launch_kwargs() -> dict:
    """Argumentos de lanzamiento de Camoufox, los MISMOS para toda la app.

    humanize: mueve el mouse con curvas realistas. geoip: alinea timezone,
    locale y coordenadas con la IP de salida — un fingerprint que se contradice
    con la IP es justamente lo que detectan los anti-bot. proxy: salida por una
    IP residencial cuando está configurado (ML bloquea las de datacenter).
    """
    kwargs: dict = {"headless": True, "humanize": True, "geoip": True}
    proxy = _proxy_config()
    if proxy:
        kwargs["proxy"] = proxy
        log.info("browser_fetch: saliendo por el proxy configurado (%s)", proxy["server"].split("://")[0])
    return kwargs


def proxy_configured() -> bool:
    """¿Hay BROWSER_PROXY válido? Lo usan las fuentes que NO deben salir desde
    la IP del datacenter (ML la bloquea): sin proxy, ni lo intentan."""
    return _proxy_config() is not None


def available() -> bool:
    """True si se puede renderizar con browser. Nunca tira."""
    s = get_settings()
    if not getattr(s, "browser_fetch_enabled", False):
        return False
    try:
        _camoufox()
    except Exception as exc:  # noqa: BLE001
        log.debug("camoufox no disponible: %s", exc)
        return False
    return True


async def _host_is_public(host: str) -> bool:
    """Resuelve el host y exige que TODAS sus IPs sean públicas. Fail-closed."""
    cached = _dns_cached(host)
    if cached is not None:
        return cached
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP
        )
    except Exception:  # noqa: BLE001
        return False  # DNS falla → no pasa, pero NO se cachea (transitorio)
    if not infos:
        return False
    ok = all(_ip_is_public(i[4][0]) for i in infos)
    _dns_store(host, ok)
    return ok


# Hosts a los que puede ir un listado de ML: la web, sus subdominios y el CDN de
# fotos. Cualquier otro (trackers, google, un redirect raro) se aborta.
ML_ALLOWED_HOSTS = ("mercadolibre.com.ar", "mercadolibre.com", "mlstatic.com")


def host_matches(host: str, domains: Iterable[str]) -> bool:
    """¿`host` es uno de `domains` o un subdominio suyo? Con chequeo de borde:
    `evilmercadolibre.com.ar` NO es `mercadolibre.com.ar`."""
    h = (host or "").strip().lower().rstrip(".")
    return bool(h) and any(h == d or h.endswith("." + d) for d in domains)


def url_host_allowed(url: object, domains: Iterable[str]) -> bool:
    """¿El host de la URL es uno de `domains` (o un subdominio)? Fail-closed:
    sin URL, sin host o URL ilegible → False."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        return host_matches(urlparse(url.strip()).hostname or "", domains)
    except ValueError:
        return False


async def _guard_route(route, request, blocked: list[str],
                       allow_hosts: Iterable[str] | None = None) -> None:
    """Handler de `page.route`: valida cada request del browser contra el guard.

    Sin esto, el browser seguiría un redirect a 169.254.169.254 o cargaría un
    <img src="http://10.0.0.5/..."> sin que `assert_public_url` se entere: ese
    chequeo solo vio la URL de entrada.

    `allow_hosts`: si viene, además solo pasan esos dominios (y subdominios): un
    listado de ML no tiene por qué hablar con ningún otro sitio.

    Nunca propaga: un handler que tira deja el request colgado hasta el timeout.
    Ante cualquier error, aborta (fail-closed).
    """
    url = request.url
    try:
        scheme = (urlparse(url).scheme or "").lower()

        if scheme in _PASSTHROUGH_SCHEMES:
            await route.continue_()
            return
        if scheme not in _ALLOWED_SCHEMES:
            blocked.append(url)
            await route.abort()
            return

        host = urlparse(url).hostname or ""
        if allow_hosts is not None and not host_matches(host, allow_hosts):
            blocked.append(url)
            log.info("browser_fetch: request fuera de los hosts permitidos: %s", url[:200])
            await route.abort()
            return
        if not host or not await _host_is_public(host):
            blocked.append(url)
            log.warning("browser_fetch: request bloqueado por el guard: %s", url[:200])
            await route.abort()
            return

        await route.continue_()
    except Exception as exc:  # noqa: BLE001
        log.debug("browser_fetch: route handler falló para %s: %s", url[:120], exc)
        try:
            await route.abort()
        except Exception as abort_exc:  # noqa: BLE001
            # El request ya se resolvió o la página se cerró: no hay nada que abortar.
            log.debug("browser_fetch: abort falló para %s: %s", url[:120], abort_exc)


# Tipos de recurso que NO hacen falta para leer el estado embebido de un
# listado: imágenes, fuentes y media. Son la mayor parte de los bytes de una
# página de ML y se pagan por GB de proxy.
HEAVY_RESOURCE_TYPES = frozenset({"image", "imageset", "font", "media"})
# Además, scripts y hojas de estilo. Medido el 08-oct-2026 contra una búsqueda
# real de ML (Camoufox, sin proxy): con scripts ~1,7 MB por búsqueda, y solo el
# documento ~0,2 MB. El estado que se lee viene en el HTML (SSR), no lo arma el
# JS. Playwright desactiva la cache HTTP cuando hay interceptación, así que los
# bundles no se reutilizan entre páginas: se bajan completos cada vez.
LEAN_RESOURCE_TYPES = HEAVY_RESOURCE_TYPES | {"script", "stylesheet"}


async def _guard_and_slim_route(route, request, blocked: list[str], skip: frozenset[str],
                                allow_hosts: Iterable[str] | None = None) -> None:
    """`_guard_route` + aborta los tipos de recurso de `skip` (sin contarlos como
    "bloqueados por el guard": no son un intento de SSRF sino ahorro)."""
    if getattr(request, "resource_type", "") in skip:
        try:
            await route.abort()
        except Exception as exc:  # noqa: BLE001
            log.debug("browser_fetch: abort falló para %s: %s", request.url[:120], exc)
        return
    await _guard_route(route, request, blocked, allow_hosts)


async def _install_guard(target, blocked: list[str], *, skip: frozenset[str] = frozenset(),
                         allow_hosts: Iterable[str] | None = None) -> None:
    """Instala el guard anti-SSRF en una página o en un contexto (`target.route`).
    `skip` son los tipos de recurso que además se cortan sin bajarlos y
    `allow_hosts` limita los dominios a los que puede salir el browser. Es la
    pieza reutilizable: cualquier lanzador de Camoufox pasa por acá."""
    hosts = tuple(allow_hosts) if allow_hosts is not None else None
    if skip:
        await target.route(
            "**/*", lambda route, request: _guard_and_slim_route(route, request, blocked, skip, hosts))
    else:
        await target.route("**/*", lambda route, request: _guard_route(route, request, blocked, hosts))


# JS que corre en la página ya renderizada. Junta las fotos del producto en
# orden de aparición: `currentSrc` primero (lo que el browser realmente cargó,
# ya resuelto el srcset), después los atributos de lazy-load.
_COLLECT_JS = """
() => {
  const out = [];
  const push = (u) => {
    if (!u) return;
    const s = String(u).trim();
    if (!s || s.startsWith('data:')) return;
    try { out.push(new URL(s, document.baseURI).href); } catch (e) {}
  };
  for (const el of document.querySelectorAll('img')) {
    // Descarta íconos y sprites: no son la foto del producto.
    if (el.naturalWidth && el.naturalWidth < 120) continue;
    push(el.currentSrc || el.src || el.getAttribute('data-src')
         || el.getAttribute('data-lazy-src') || el.getAttribute('data-original'));
  }
  // Fotos puestas como background-image (galerías de 1688 lo hacen).
  for (const el of document.querySelectorAll('[style*="background-image"]')) {
    const m = /url\\((['"]?)(.*?)\\1\\)/.exec(el.style.backgroundImage || '');
    if (m) push(m[2]);
  }
  return out;
}
"""


# ─── Corta-circuitos por host ──────────────────────────────────────
#
# Un render cuesta ~50 s de reloj (launch con geoip por proxy, scroll, teardown).
# Cuando un sitio nos viene tirando el interstitial anti-bot, ese medio minuto se
# paga entero para terminar con 0 fotos, y encima con el cliente esperando. Si un
# host falla N veces seguidas, lo salteamos por un rato: el llamador se cae al
# camino que ya tenía (fotos del catálogo, marcadas approximate) sin pagar la
# espera. Un solo éxito lo reabre.
#
# En memoria a propósito: se reinicia con el deploy, que es justo cuando cambia
# la configuración de proxy que podría hacerlo andar de nuevo.
_zero_streak: dict[str, int] = {}
_skip_until: dict[str, float] = {}


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def circuit_open_for(url: str) -> bool:
    """True si a este host le toca descanso: no vale la pena ni lanzar el browser."""
    host = _host_of(url)
    if not host:
        return False
    until = _skip_until.get(host)
    if until is None:
        return False
    if time.monotonic() >= until:
        # Se cumplió el descanso: le damos otra oportunidad desde cero.
        _skip_until.pop(host, None)
        _zero_streak.pop(host, None)
        return False
    return True


def note_render_result(url: str, *, images_found: int) -> None:
    """Le avisa al corta-circuitos cómo le fue al render de este host."""
    host = _host_of(url)
    if not host:
        return
    if images_found > 0:
        _zero_streak.pop(host, None)
        _skip_until.pop(host, None)
        return
    s = get_settings()
    streak = _zero_streak.get(host, 0) + 1
    _zero_streak[host] = streak
    if streak >= s.browser_fetch_zero_streak:
        _skip_until[host] = time.monotonic() + s.browser_fetch_cooldown_seconds
        log.warning(
            "browser_fetch: %s viene de %d renders con 0 fotos — lo salteo por %d s",
            host, streak, s.browser_fetch_cooldown_seconds,
        )


def reset_circuit() -> None:
    """Para los tests y para poder reabrirlo a mano desde el dashboard."""
    _zero_streak.clear()
    _skip_until.clear()


# ─── Un solo Firefox a la vez ──────────────────────────────────────
#
# El container es de 3 GB y lo comparte CLIP: dos Firefox al mismo tiempo (una
# ficha del app y la búsqueda nocturna del semáforo) son un OOM. Todo
# lanzamiento pasa por este lugar único. Un `render()` (hay un cliente
# esperando) tiene prioridad: pide a los ListingBrowser que suelten su Firefox y
# el listado se relanza después, en vez de dejar al cliente horas esperando.

_slot_lock: asyncio.Lock | None = None
_slot_loop: asyncio.AbstractEventLoop | None = None
_slot_waiting = 0
# Cuánto espera un render() por el lugar antes de rendirse (el llamador ya tiene
# su propio techo de reloj) y un listado, que puede esperar a que termine un render.
_SLOT_WAIT_RENDER_S = 30.0
_SLOT_WAIT_LISTING_S = 180.0
_listing_browsers: "weakref.WeakSet[ListingBrowser]" = weakref.WeakSet()


def _slot() -> asyncio.Lock:
    """El lock del lugar, atado al loop en uso (los tests cambian de loop)."""
    global _slot_lock, _slot_loop
    loop = asyncio.get_running_loop()
    if _slot_lock is None or _slot_loop is not loop:
        _slot_lock, _slot_loop = asyncio.Lock(), loop
    return _slot_lock


async def _take_slot(timeout: float | None, *, interactive: bool) -> None:
    """Espera el único lugar de Firefox. `interactive=True` (un render con un
    cliente esperando) hace que los listados lo suelten."""
    global _slot_waiting
    lock = _slot()
    if interactive:
        _slot_waiting += 1
        for lb in list(_listing_browsers):
            lb._request_yield()
    try:
        if timeout is None:
            await lock.acquire()
        else:
            await asyncio.wait_for(lock.acquire(), timeout)
    except asyncio.TimeoutError as exc:
        raise BrowserUnavailable("hay otro navegador en uso (un solo Firefox a la vez)") from exc
    finally:
        if interactive:
            _slot_waiting -= 1


def _give_slot() -> None:
    lock = _slot()
    if lock.locked():
        lock.release()


def slot_busy() -> bool:
    """¿Hay un Firefox corriendo ahora? (para diagnóstico y tests)"""
    return _slot_lock is not None and _slot_lock.locked()


async def render(url: str, *, max_images: int | None = None) -> RenderedPage:
    """Abre `url` en Camoufox, deja correr el JS y devuelve HTML + fotos del DOM.

    Lanza BrowserUnavailable si no se puede usar el browser (o hay otro Firefox
    en uso y no se liberó a tiempo), SsrfBlocked si la URL apunta a una red no
    pública.
    """
    if not available():
        raise BrowserUnavailable("camoufox no está instalado o BROWSER_FETCH_ENABLED=false")

    s = get_settings()
    limit = max_images or s.browser_fetch_max_images
    timeout = s.browser_fetch_timeout_ms

    # falla temprano y barato; el route guard hace el resto. getaddrinfo bloquea:
    # va a un thread.
    await asyncio.to_thread(assert_public_url, url)

    AsyncCamoufox = _camoufox()
    page_data = RenderedPage()
    blocked: list[str] = []

    await _take_slot(_SLOT_WAIT_RENDER_S, interactive=True)
    try:
        async with AsyncCamoufox(**_launch_kwargs()) as browser:
            page = await browser.new_page()
            await _install_guard(page, blocked)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                # La galería carga lazy con el scroll. Con JS y no con mouse.wheel:
                # la ruta de input nativo de Firefox es donde Camoufox segfaultea.
                #
                # Esperas generosas a propósito: por un proxy (necesario para saltear
                # el anti-bot de ML por IP) la latencia sube y con 3×600ms solo
                # cargaba la foto principal — 1 de 10. Con 6×900ms + un networkidle
                # best-effort entra la galería entera, con o sin proxy.
                for _ in range(6):
                    await page.evaluate("window.scrollBy(0, 1400)")
                    await page.wait_for_timeout(900)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001
                    # Trackers/websockets colgados nunca dejan la red "idle"; la
                    # galería ya cargó con el scroll, así que no bloqueamos por eso.
                    pass

                page_data.final_url = page.url
                page_data.title = (await page.title()) or ""
                page_data.html = await page.content()
                raw_images = await page.evaluate(_COLLECT_JS)
            except Exception as exc:  # noqa: BLE001
                raise BrowserUnavailable(
                    f"El browser no pudo renderizar la página: {type(exc).__name__}: {exc}"
                ) from exc
    except (BrowserUnavailable, SsrfBlocked):
        raise
    except Exception as exc:  # noqa: BLE001  (el lanzamiento: Firefox, geoip, proxy)
        raise BrowserUnavailable(f"No se pudo lanzar el browser: {type(exc).__name__}: {exc}") from exc
    finally:
        _give_slot()

    seen: set[str] = set()
    images: list[str] = []
    for candidate in raw_images or []:
        if candidate in seen:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            continue
        seen.add(candidate)
        images.append(candidate)
        if len(images) >= limit:
            break

    page_data.image_urls = images
    page_data.blocked = blocked
    if blocked:
        log.info("browser_fetch: %d requests bloqueados en %s", len(blocked), url[:120])
    return page_data


# ─── Listados: un browser para muchas búsquedas ────────────────────


@dataclass(slots=True)
class ListingPage:
    """Una página de listado ya cargada (el HTML trae el estado embebido)."""
    html: str = ""
    final_url: str = ""
    # Status HTTP del documento principal (None si el browser no lo informó).
    status: int | None = None
    # Bytes que bajó la página por el proxy: cabeceras + cuerpo de las
    # respuestas COMPLETADAS (Request.sizes). Lo que se cortó al cerrar la
    # página no está, así que es un piso del consumo real.
    bytes: int = 0
    # Requests que el guard anti-SSRF abortó mientras se cargaba.
    blocked: int = 0
    elapsed_s: float = 0.0


class CircuitOpen(BrowserUnavailable):
    """El host viene fallando y está en descanso (ver `circuit_open_for`)."""


# Tope para levantar Firefox (con geoip por proxy ronda los 10-50 s).
_LAUNCH_TIMEOUT_S = 90.0
# Margen sobre el timeout del goto para el resto de la operación.
_PAGE_MARGIN_S = 15.0
# Cerrar una página o el navegador no puede colgar la corrida: si Firefox no
# contesta se sigue y el browser se da por roto (se relanza).
_CLOSE_PAGE_TIMEOUT_S = 10.0
_CLOSE_BROWSER_TIMEOUT_S = 15.0


class ListingBrowser:
    """UN Camoufox para MUCHAS páginas de listado seguidas.

    `render()` abre un browser por llamada (~50 s de reloj): para 1.000
    búsquedas por noche eso son 14 horas. Acá se lanza una vez (mismos
    argumentos y mismo proxy que `render()`), todas las páginas comparten un
    contexto (cookies y sesión) y se relanza cada `browser_listing_recycle_after`
    páginas (75) para no acumular memoria.

    Seguridad: el contexto lleva el mismo guard que `render()` (cada request,
    redirects y subrecursos incluidos, se valida contra red pública) y solo puede
    hablar con `allow_hosts` (por default los de ML).

    Ahorro: corta imágenes, fuentes y media y, con `block_scripts` (default),
    también scripts y estilos: el estado de la búsqueda viene en el HTML. Sin
    eso cada búsqueda baja ~1,7 MB por el proxy en vez de ~0,2 MB.

    Memoria: hay un solo Firefox a la vez en todo el proceso (`_take_slot`). Un
    `render()` con un cliente esperando le pide a este browser que lo suelte; se
    relanza en la próxima búsqueda.

    Uso:
        async with ListingBrowser() as lb:
            page = await lb.fetch("https://listado.mercadolibre.com.ar/...")
    """

    def __init__(self, *, recycle_after: int | None = None, block_scripts: bool = True,
                 allow_hosts: Iterable[str] | None = ML_ALLOWED_HOSTS) -> None:
        self._recycle_after = max(1, int(recycle_after or get_settings().browser_listing_recycle_after))
        self._skip = LEAN_RESOURCE_TYPES if block_scripts else HEAVY_RESOURCE_TYPES
        self._allow_hosts = tuple(allow_hosts) if allow_hosts is not None else None
        self._cond = asyncio.Condition()
        self._cm = None
        self._context = None
        self._blocked: list[str] = []
        self._active = 0
        self._pages = 0
        self._broken = False
        # Se están esperando las páginas en vuelo para relanzar: no arrancan más.
        self._draining = False
        self._holds_slot = False
        self._yield_requested = False
        self._tasks: set[asyncio.Future] = set()
        self.launches = 0
        _listing_browsers.add(self)

    async def __aenter__(self) -> "ListingBrowser":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- ciclo de vida -------------------------------------------------------

    async def _launch(self) -> None:
        await _take_slot(_SLOT_WAIT_LISTING_S, interactive=False)
        self._holds_slot = True
        AsyncCamoufox = _camoufox()
        cm = AsyncCamoufox(**_launch_kwargs())
        try:
            browser = await asyncio.wait_for(cm.__aenter__(), timeout=_LAUNCH_TIMEOUT_S)
            context = await browser.new_context()
            await _install_guard(context, self._blocked, skip=self._skip, allow_hosts=self._allow_hosts)
        except Exception as exc:  # noqa: BLE001
            await self._close_cm(cm)
            self._release_slot()
            raise BrowserUnavailable(
                f"No se pudo lanzar el browser: {type(exc).__name__}: {exc}"
            ) from exc
        self._cm, self._context = cm, context
        self._pages = 0
        self._broken = False
        self.launches += 1

    async def _close_cm(self, cm) -> None:
        """Cierra Camoufox sin colgarse: si no contesta a tiempo se sigue."""
        try:
            await asyncio.wait_for(cm.__aexit__(None, None, None), _CLOSE_BROWSER_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("browser_fetch: Firefox no cerró en %.0f s, sigo igual", _CLOSE_BROWSER_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            log.debug("browser_fetch: cierre del listing browser falló: %s", redact(exc))

    def _release_slot(self) -> None:
        if self._holds_slot:
            self._holds_slot = False
            _give_slot()

    async def _shutdown(self) -> None:
        cm, self._cm, self._context = self._cm, None, None
        try:
            if cm is not None:
                await self._close_cm(cm)
        finally:
            self._release_slot()

    async def close(self) -> None:
        async with self._cond:
            await self._shutdown()
            self._cond.notify_all()

    def _request_yield(self) -> None:
        """Un render() con un cliente esperando necesita el lugar: si no hay
        páginas en vuelo se suelta ya; si las hay, apenas terminen."""
        self._yield_requested = True
        self._spawn(self._after_release())

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _after_release(self) -> None:
        async with self._cond:
            self._cond.notify_all()
            if self._yield_requested and self._active == 0 and self._context is not None:
                self._yield_requested = False
                log.info("browser_fetch: el listado suelta Firefox para un render con un cliente esperando")
                await self._shutdown()

    async def _acquire(self):
        """Un lugar para una página. Si hace falta relanzar (páginas cumplidas o
        browser roto) no arranca ninguna nueva hasta que terminen las en vuelo:
        con concurrencia 2 nadie le cierra el browser a otra página."""
        async with self._cond:
            await self._cond.wait_for(lambda: not self._draining)
            if self._context is not None and (self._broken or self._pages >= self._recycle_after):
                self._draining = True
                try:
                    await self._cond.wait_for(lambda: self._active == 0)
                    await self._shutdown()
                finally:
                    self._draining = False
                    self._cond.notify_all()
            if self._context is None:
                await self._launch()
            self._active += 1
            self._pages += 1
            return self._context

    def _release(self) -> None:
        # Sincrónico: una cancelación no puede dejar la cuenta torcida.
        self._active -= 1
        self._spawn(self._after_release())

    # -- una página ----------------------------------------------------------

    async def fetch(self, url: str) -> ListingPage:
        """Carga `url` y devuelve el HTML (el estado embebido viene en el SSR,
        no hace falta scrollear ni esperar al JS). Lanza BrowserUnavailable si
        el browser no está, no arranca o la página no cargó; CircuitOpen si el
        host está en descanso; SsrfBlocked si la URL apunta a una red interna."""
        if not available():
            raise BrowserUnavailable("camoufox no está instalado o BROWSER_FETCH_ENABLED=false")
        if circuit_open_for(url):
            raise CircuitOpen(f"{_host_of(url)} viene fallando, en descanso")
        await asyncio.to_thread(assert_public_url, url)

        s = get_settings()
        timeout_ms = s.browser_fetch_timeout_ms
        started = time.monotonic()
        context = await self._acquire()
        try:
            return await asyncio.wait_for(
                self._load(context, url, timeout_ms, started),
                timeout=timeout_ms / 1000 + _PAGE_MARGIN_S,
            )
        except BrowserUnavailable:
            raise
        except asyncio.TimeoutError as exc:
            self._broken = True
            raise BrowserUnavailable(
                f"La página no cargó en {timeout_ms / 1000 + _PAGE_MARGIN_S:.0f} s"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._broken = True
            raise BrowserUnavailable(
                f"El browser no pudo cargar la página: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            self._release()

    async def _load(self, context, url: str, timeout_ms: int, started: float) -> ListingPage:
        page = await context.new_page()
        total = 0
        pending: list[asyncio.Future] = []

        async def _size(request) -> None:
            nonlocal total
            try:
                sizes = await request.sizes()
                total += sum(int(sizes.get(k) or 0) for k in (
                    "requestHeadersSize", "requestBodySize",
                    "responseHeadersSize", "responseBodySize"))
            except Exception:  # noqa: BLE001  (la medición nunca rompe la carga)
                pass

        page.on("requestfinished", lambda r: pending.append(asyncio.ensure_future(_size(r))))
        blocked_before = len(self._blocked)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = await page.content()
            final_url = page.url
            status = response.status if response is not None else None
            if pending:
                await asyncio.wait(pending, timeout=2.0)
        finally:
            try:
                await asyncio.wait_for(page.close(), _CLOSE_PAGE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001  (incluye el timeout: Firefox colgado)
                self._broken = True
                log.warning("browser_fetch: no se pudo cerrar la página (%s): se relanza el browser",
                            type(exc).__name__)
        return ListingPage(
            html=html, final_url=final_url, status=status, bytes=total,
            blocked=len(self._blocked) - blocked_before,
            elapsed_s=time.monotonic() - started,
        )


def note_listing_result(url: str, *, results: int) -> None:
    """Le avisa al corta-circuitos por host cómo le fue a un listado (la
    cantidad de resultados leídos hace de "fotos encontradas")."""
    note_render_result(url, images_found=results)


__all__ = [
    "BrowserUnavailable", "CircuitOpen", "ListingBrowser", "ListingPage", "ML_ALLOWED_HOSTS",
    "RenderedPage", "SsrfBlocked", "available", "circuit_open_for", "host_matches",
    "note_listing_result", "proxy_configured", "proxy_problem", "redact", "render",
    "url_host_allowed",
]
