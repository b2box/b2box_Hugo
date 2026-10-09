"""Un servidor HTTP de verdad (127.0.0.1, puerto al azar, en un thread) para probar la pila de red REAL de Hugo
(httpx + httpcore + sockets) sin salir a internet: `net_guard.safe_get`, el indexador de tiendas y las descargas de fotos.

`Local.route(path, ...)` registra respuestas fijas o una función `(request) -> (status, headers, body)`; el servidor
anota cada pedido (`requests`: método, path con query, headers). Para que Hugo crea que habla con `https://www.gadnic.com.ar`
se usa `Local.install(monkeypatch)`: el DNS devuelve una IP pública, 127.0.0.1 pasa por pública y TODOS los pedidos de
`httpx.AsyncClient` dentro de `net_guard` van a este servidor (conservando el header Host). Todo lo demás (streaming,
gzip, chunked, redirects, el chequeo de la IP del peer) es el código real.
"""

from __future__ import annotations

import gzip
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from app import net_guard

Body = bytes | str
Response = tuple[int, dict[str, str], Body]


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "Local._Server"

    def log_message(self, *a, **k) -> None:  # silencio
        return

    def _serve(self) -> None:
        local = self.server.local
        seen = Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
        with local.lock:
            local.requests.append(seen)
        handler = local.routes.get(self.path.split("?", 1)[0]) or local.routes.get(self.path) or local.default
        if handler is None:
            status, headers, body = 404, {}, b"no existe"
        elif callable(handler):
            status, headers, body = handler(seen)
        else:
            status, headers, body = handler
        data = body.encode() if isinstance(body, str) else body
        chunked = headers.pop("X-Chunked", None) is not None
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(data), 4096):
                piece = data[i:i + 4096]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    do_GET = do_HEAD = _serve


def gz(data: Body, level: int = 6) -> bytes:
    return gzip.compress(data.encode() if isinstance(data, str) else data, compresslevel=level)


def deflate(data: Body) -> bytes:
    return zlib.compress(data.encode() if isinstance(data, str) else data)


@dataclass
class Local:
    routes: dict[str, Response | Callable[[Seen], Response]] = field(default_factory=dict)
    default: Response | Callable[[Seen], Response] | None = None
    requests: list[Seen] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    port: int = 0
    _httpd: "Local._Server | None" = None

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        local: "Local"

    def route(self, path: str, body: Body = "", status: int = 200, **headers: str) -> None:
        self.routes[path] = (status, {k.replace("_", "-"): v for k, v in headers.items()}, body)

    def redirect(self, path: str, to: str, status: int = 301) -> None:
        self.routes[path] = (status, {"Location": to}, "")

    def start(self) -> "Local":
        self._httpd = Local._Server(("127.0.0.1", 0), _Handler)
        self._httpd.local = self
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def paths(self) -> list[str]:
        with self.lock:
            return [r.path for r in self.requests]

    def install(self, monkeypatch) -> None:
        """Hugo habla con «https://<cualquier host>» y le contesta este servidor, por la pila real."""
        real_client = httpx.AsyncClient
        port = self.port
        orig_public = net_guard._ip_is_public

        class ToLocal(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                original = request.url
                request.url = original.copy_with(scheme="http", host="127.0.0.1", port=port)
                try:
                    return await super().handle_async_request(request)
                finally:
                    request.url = original              # el código de Hugo mira `resp.url`: tiene que ser la URL pedida

        def client(**kw):
            kw.pop("transport", None)
            return real_client(transport=ToLocal(), **kw)

        monkeypatch.setattr(net_guard.httpx, "AsyncClient", client)
        monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
        monkeypatch.setattr(net_guard, "_ip_is_public", lambda ip: ip == "127.0.0.1" or orig_public(ip))
