"""S2 network policy tests; HTTP transport and DNS are synthetic, no network."""
import ipaddress
import socket

import httpcore
import httpx
import pytest

from app.core.config import get_settings
from app.data.crawler import Crawler, UrlNotAllowed, check_url_allowed


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(get_settings(), "crawl_allow_private", False)

    def resolve(host, port, *args, **kwargs):
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.mark.parametrize("entry", ["robots", "sitemap", "html"])
@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "http://169.254.169.254/meta", "http://10.0.0.1/internal"])
def test_crawler_never_requests_private_redirect_target(entry, target):
    seen = []
    path = {"robots": "/robots.txt", "sitemap": "/sitemap.xml", "html": "/"}[entry]

    def handle(request):
        seen.append(str(request.url))
        if request.url.host == "public.example.com" and request.url.path == path:
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, text="<html>private content</html>", headers={"Content-Type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        crawler = Crawler(client=client)
        if entry == "robots":
            crawler._robot("https://public.example.com")
        elif entry == "sitemap":
            crawler._sitemap("https://public.example.com")
        else:
            crawler.crawl(["https://public.example.com/"])
    assert target not in seen


@pytest.mark.parametrize("url", ["https://user:pass@public.example.com/", "https://public.example.com:8443/",
                                 "http://0.0.0.0/", "http://[::]/", "file:///etc/passwd"])
def test_rejects_non_web_or_credentialed_targets(url):
    with pytest.raises(UrlNotAllowed):
        check_url_allowed(url)


def test_connect_time_dns_rebinding_is_rejected(monkeypatch):
    from app.data.safe_http import ValidatedNetworkBackend

    check_url_allowed("https://public.example.com")
    connected = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda *_a, **_k: connected.append(True))
    with pytest.raises(UrlNotAllowed):
        ValidatedNetworkBackend(False).connect_tcp("public.example.com", 443)
    assert connected == []


def test_connection_uses_validated_numeric_address(monkeypatch):
    from app.data.safe_http import ValidatedNetworkBackend

    connected = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda _self, host, *_a: connected.append(host))
    ValidatedNetworkBackend(False).connect_tcp("public.example.com", 443)
    assert connected == ["93.184.216.34"]


def test_mixed_public_private_dns_answers_fail_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ("93.184.216.34", "10.0.0.1")])
    with pytest.raises(UrlNotAllowed):
        check_url_allowed("https://public.example.com")


def test_redirect_limit_and_response_size():
    from app.data.safe_http import safe_get

    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/again"})

    with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(UrlNotAllowed, match="重定向"):
            safe_get("https://public.example.com", client=client, max_redirects=2)
    assert len(seen) == 3
    with httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 20))) as client:
        with pytest.raises(UrlNotAllowed, match="响应过大"):
            safe_get("https://public.example.com", client=client, max_bytes=10)


def test_safe_relative_redirect_and_decoded_body():
    import gzip

    from app.data.safe_http import safe_get

    def handle(request):
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "/about"})
        return httpx.Response(200, content=gzip.compress(b"public page"),
                              headers={"Content-Encoding": "gzip", "Content-Type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        response = safe_get("https://public.example.com/", client=client)
    assert response.text == "public page"
    assert str(response.url) == "https://public.example.com/about"


def test_inquiry_site_check_uses_guarded_redirects(monkeypatch):
    from app.crm import inquiry
    from app.data import safe_http

    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    monkeypatch.setattr(inquiry, "_site_fetcher", None)
    monkeypatch.setattr(safe_http, "safe_client", lambda **_k: httpx.Client(transport=httpx.MockTransport(handle)))
    assert inquiry._fetch_site("public.example.com") is None
    assert seen == ["https://public.example.com/"]


def test_fingerprint_entry_does_not_fetch_internal_redirect(monkeypatch):
    import asyncio
    import uuid
    from types import SimpleNamespace

    from app.api import b7b8
    from app.data import safe_http

    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    company = SimpleNamespace(id=uuid.uuid4(), domain="public.example.com", tech_stack=None)
    monkeypatch.setattr(b7b8, "_get", lambda *_: company)
    monkeypatch.setattr(b7b8, "audit", lambda *_: None)
    monkeypatch.setattr(safe_http, "safe_client", lambda **_k: httpx.Client(transport=httpx.MockTransport(handle)))
    asyncio.run(b7b8.tech_fp(company.id, request=None, cu=None, db=SimpleNamespace(commit=lambda: None)))
    assert seen == ["https://public.example.com"]
    assert company.tech_stack["note"] == "fetch_failed"
