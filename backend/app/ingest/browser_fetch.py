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
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import get_settings
from app.net_guard import SsrfBlocked, assert_public_url

log = logging.getLogger(__name__)

# Schemes que el browser puede pedir. Todo lo demás se aborta.
_ALLOWED_SCHEMES = {"http", "https"}
# about:blank y blob:/data: internos de la página no son navegación a red;
# dejarlos pasar evita romper páginas legítimas sin abrir superficie.
_PASSTHROUGH_SCHEMES = {"about", "blob", "data"}

# Cache de resolución DNS por host, para no pagar un getaddrinfo por subrecurso
# (una ficha de ML dispara 100+ requests a 4-5 hosts).
_host_ok: dict[str, bool] = {}
_host_ok_lock = asyncio.Lock()


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
    """Camoufox no está instalado o está deshabilitado por configuración."""


def _camoufox():
    """Import perezoso: el paquete es opcional y pesa (Firefox + fingerprints)."""
    from camoufox.async_api import AsyncCamoufox  # noqa: PLC0415

    return AsyncCamoufox


def _proxy_config() -> dict[str, str] | None:
    """Traduce settings.browser_proxy al dict que espera Camoufox/Playwright.

    Playwright quiere el server SIN credenciales embebidas y user/pass en campos
    aparte: "http://u:p@host:port" embebido no siempre autentica. Devuelve None
    si no hay proxy configurado.
    """
    raw = (get_settings().browser_proxy or "").strip()
    if not raw:
        return None
    p = urlparse(raw)
    if not p.hostname:
        log.warning("browser_proxy mal formado, lo ignoro: %r", raw[:60])
        return None
    scheme = p.scheme or "http"
    server = f"{scheme}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    cfg: dict[str, str] = {"server": server}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


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


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _host_is_public(host: str) -> bool:
    """Resuelve el host y exige que TODAS sus IPs sean públicas. Fail-closed."""
    async with _host_ok_lock:
        cached = _host_ok.get(host)
    if cached is not None:
        return cached

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP
        )
        ok = bool(infos) and all(_ip_is_public(i[4][0]) for i in infos)
    except Exception:  # noqa: BLE001
        ok = False  # DNS falla → no pasa

    async with _host_ok_lock:
        _host_ok[host] = ok
    return ok


async def _guard_route(route, request, blocked: list[str]) -> None:
    """Handler de `page.route`: valida cada request del browser contra el guard.

    Sin esto, el browser seguiría un redirect a 169.254.169.254 o cargaría un
    <img src="http://10.0.0.5/..."> sin que `assert_public_url` se entere: ese
    chequeo solo vio la URL de entrada.

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


async def render(url: str, *, max_images: int | None = None) -> RenderedPage:
    """Abre `url` en Camoufox, deja correr el JS y devuelve HTML + fotos del DOM.

    Lanza BrowserUnavailable si no se puede usar el browser, SsrfBlocked si la
    URL apunta a una red no pública.
    """
    if not available():
        raise BrowserUnavailable("camoufox no está instalado o BROWSER_FETCH_ENABLED=false")

    s = get_settings()
    limit = max_images or s.browser_fetch_max_images
    timeout = s.browser_fetch_timeout_ms

    assert_public_url(url)  # falla temprano y barato; el route guard hace el resto

    AsyncCamoufox = _camoufox()
    page_data = RenderedPage()
    blocked: list[str] = []

    # humanize: mueve el mouse con curvas realistas. geoip: alinea timezone,
    # locale y coordenadas con la IP de salida — un fingerprint que se contradice
    # con la IP es justamente lo que detectan los anti-bot. proxy: salida por una
    # IP residencial cuando está configurado (ML bloquea las de datacenter).
    proxy = _proxy_config()
    launch_kwargs: dict = {"headless": True, "humanize": True, "geoip": True}
    if proxy:
        launch_kwargs["proxy"] = proxy
        log.info("browser_fetch: usando proxy %s", proxy["server"])
    async with AsyncCamoufox(**launch_kwargs) as browser:
        page = await browser.new_page()
        await page.route(
            "**/*", lambda route, request: _guard_route(route, request, blocked)
        )

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            # La galería suele cargar con el scroll. Con JS y no con mouse.wheel:
            # la ruta de input nativo de Firefox es donde Camoufox segfaultea.
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 1600)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(800)

            page_data.final_url = page.url
            page_data.title = (await page.title()) or ""
            page_data.html = await page.content()
            raw_images = await page.evaluate(_COLLECT_JS)
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(
                f"El browser no pudo renderizar la página: {type(exc).__name__}: {exc}"
            ) from exc

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


__all__ = ["BrowserUnavailable", "RenderedPage", "SsrfBlocked", "available", "render"]
