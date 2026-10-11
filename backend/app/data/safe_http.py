"""Bounded public-web GETs with per-hop and connect-time destination checks."""
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from app.core.config import get_settings

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class UrlNotAllowed(ValueError):
    pass


def _addresses(host: str, port: int, allow_private: bool) -> list[str]:
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except (OSError, ValueError) as exc:
        raise UrlNotAllowed(f"无法解析域名: {host}") from exc
    if not addresses or (not allow_private and any(not ipaddress.ip_address(ip).is_global for ip in addresses)):
        raise UrlNotAllowed(f"非公网地址: {host}")
    return addresses


def check_url_allowed(url: str, allow_private: bool | None = None) -> str:
    if allow_private is None:
        allow_private = get_settings().crawl_allow_private
    try:
        p = urlparse(url.strip())
        port = p.port or (443 if p.scheme == "https" else 80)
        if p.scheme not in ("http", "https") or not p.hostname or p.username is not None or p.password is not None:
            raise UrlNotAllowed("仅允许无凭据的 HTTP(S) URL")
        if not allow_private and port not in (80, 443):
            raise UrlNotAllowed("仅允许公开 Web 端口")
        if not allow_private:
            _addresses(p.hostname, port, False)
    except ValueError as exc:
        raise UrlNotAllowed(str(exc)) from exc
    return url.strip()


class ValidatedNetworkBackend(httpcore.SyncBackend):
    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve again at the actual connection boundary, then connect to the
        # validated numeric IP. TLS SNI and certificate checks retain the URL host.
        addresses = _addresses(host, port, self.allow_private)
        return super().connect_tcp(addresses[0], port, timeout, local_address, socket_options)


class SafeHTTPTransport(httpx.HTTPTransport):
    def __init__(self, allow_private: bool):
        # HTTPX 0.28.1's transport adapts this HTTPcore pool. Its public pool
        # network_backend hook pins each TCP connection to a validated IP.
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
                                             network_backend=ValidatedNetworkBackend(allow_private))


def safe_client(*, timeout: float = 20, headers=None) -> httpx.Client:
    return httpx.Client(transport=SafeHTTPTransport(get_settings().crawl_allow_private),
                        trust_env=False, follow_redirects=False, timeout=timeout, headers=headers)


def safe_get(url: str, *, client: httpx.Client | None = None, timeout: float = 20,
             headers=None, max_bytes: int = MAX_BYTES, max_redirects: int = MAX_REDIRECTS) -> httpx.Response:
    if client is None:
        with safe_client(timeout=timeout, headers=headers) as owned:
            return safe_get(url, client=owned, max_bytes=max_bytes, max_redirects=max_redirects)
    for hop in range(max_redirects + 1):
        url = check_url_allowed(url)
        # Explicitly override injected clients' follow_redirects as well.
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                if hop == max_redirects:
                    raise UrlNotAllowed("重定向次数超限")
                url = urljoin(str(response.url), response.headers["location"])
                continue
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise UrlNotAllowed("页面响应过大")
                chunks.append(chunk)
            # iter_bytes is decoded, so do not apply Content-Encoding twice.
            response_headers = {k: v for k, v in response.headers.items()
                                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
            return httpx.Response(response.status_code, headers=response_headers, content=b"".join(chunks), request=response.request)
    raise UrlNotAllowed("重定向次数超限")
