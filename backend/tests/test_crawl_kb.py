import httpx
import pytest

from app.data.crawler import Crawler, UrlNotAllowed, check_url_allowed
from tests.conftest import add_user, register

SITE = {
    "/robots.txt": "User-agent: *\nDisallow: /private\n",
    "/sitemap.xml": "<urlset><url><loc>http://acme.test/products</loc></url></urlset>",
    "/": "<html><head><title>ACME Valves | Home</title><meta name='description' content='ACME makes industrial valves since 1998.'></head>"
         "<body><h1>Welcome</h1><a href='/products'>Products</a><a href='/private/x'>p</a><a href='/about'>About</a>"
         "<a href='https://other.test/'>ext</a><p>Contact sales@acme.test or +86 571 8888 6666. CE and ISO 9001 certified.</p></body></html>",
    "/products": "<html><head><title>Products</title></head><body><h2>Ball Valve BV-100</h2><h2>Gate Valve GV-200</h2>"
                 "<a href='/products/bv-100'>bv</a></body></html>",
    "/products/bv-100": "<html><head><title>BV-100</title></head><body><h1>Ball Valve BV-100</h1><p>DN15-DN100</p></body></html>",
    "/about": "<html><head><title>About</title></head><body><h1>About ACME</h1></body></html>",
    "/private/x": "<html>secret</html>",
}


def handler(req: httpx.Request):
    if req.url.host != "acme.test":
        return httpx.Response(404)
    body = SITE.get(req.url.path)
    if body is None:
        return httpx.Response(404)
    ct = "text/plain" if req.url.path.endswith((".txt", ".xml")) else "text/html"
    return httpx.Response(200, text=body, headers={"content-type": ct})


def mock_client():
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_crawler_respects_robots_and_domain():
    res = Crawler(client=mock_client(), max_pages=10).crawl(["http://acme.test/"])
    urls = [p.url for p in res.pages]
    assert "http://acme.test/" in urls and "http://acme.test/products" in urls
    assert not any("private" in u for u in urls) and not any("other.test" in u for u in urls)
    assert "http://acme.test/private/x" in res.skipped_robots
    # 产品页优先于 about
    assert urls.index("http://acme.test/products") < urls.index("http://acme.test/about")


def test_crawler_page_limit():
    res = Crawler(client=mock_client(), max_pages=2).crawl(["http://acme.test/"])
    assert len(res.pages) == 2


def test_url_guard_blocks_private():
    for u in ["http://127.0.0.1/", "http://localhost/", "ftp://example.com", "http://10.0.0.1/"]:
        with pytest.raises(UrlNotAllowed):
            check_url_allowed(u, allow_private=False)


@pytest.fixture
def mock_crawler(monkeypatch):
    import app.tasks.jobs as jobs
    monkeypatch.setattr(jobs, "_crawler_factory", lambda: (lambda max_pages: Crawler(client=mock_client(), max_pages=max_pages)))


def test_crawl_api_end_to_end(client, mock_crawler):
    h = register(client, "a@x.com")
    r = client.post("/api/v1/kb/crawls", headers=h, json={"urls": ["http://acme.test/"]})
    assert r.json()["error"]["code"] == "KB_RIGHTS_REQUIRED"
    r = client.post("/api/v1/kb/crawls", headers=h, json={"urls": ["http://acme.test/", "acme.test"], "rights_confirmed": True})
    assert r.status_code == 202, r.text
    cj = client.get(f"/api/v1/kb/crawls/{r.json()['id']}", headers=h).json()
    assert cj["urls"] == ["http://acme.test/"]  # 同域名去重
    assert cj["status"] == "partial" and cj["pages_fetched"] >= 3 and cj["pages_skipped_robots"] == 1
    assert cj["log"]["method"] == "heuristic"  # mock 模型 → 规则抽取降级
    items = client.get("/api/v1/kb/items", headers=h).json()["items"]
    titles = {(i["type"], i["title"]) for i in items}
    assert ("product", "Ball Valve BV-100") in titles and ("certification", "CE") in titles
    assert any(i["type"] == "contact" and "sales@acme.test" in i["content"] for i in items)
    assert all(i["status"] == "pending" for i in items)
    assert all(i["source_ref"] for i in items)  # 每条都可追溯来源页
    # 批量确认 / 驳回
    pids = [i["id"] for i in items if i["type"] == "product"]
    client.post("/api/v1/kb/items:batch", headers=h, json={"ids": pids[:1], "action": "confirm"})
    client.post("/api/v1/kb/items:batch", headers=h, json={"ids": pids[1:], "action": "reject"})
    confirmed = client.get("/api/v1/kb/items?status=confirmed", headers=h).json()["items"]
    assert [i["id"] for i in confirmed] == pids[:1]
    actions = [x["action"] for x in client.get("/api/v1/audit-logs", headers=h).json()["items"]]
    assert {"kb.crawl_started", "kb.crawl_finished", "kb.item_confirm", "kb.item_reject"} <= set(actions)


def test_crawl_uses_llm_when_valid(client, mock_crawler):
    import json

    from app.llm.gateway import LLMGateway, MockProvider, set_gateway
    reply = json.dumps({"company_profile": {"name": "ACME", "summary": "阀门制造商", "source_url": "http://acme.test/"},
                        "products": [{"name": "球阀 BV-100", "description": "DN15-100", "source_url": "http://acme.test/products/bv-100"}]})
    set_gateway(LLMGateway(MockProvider(replies=[reply])))
    try:
        h = register(client, "a@x.com")
        r = client.post("/api/v1/kb/crawls", headers=h, json={"urls": ["http://acme.test/"], "rights_confirmed": True})
        assert client.get(f"/api/v1/kb/crawls/{r.json()['id']}", headers=h).json()["log"]["method"] == "llm"
        titles = [i["title"] for i in client.get("/api/v1/kb/items", headers=h).json()["items"]]
        assert titles == ["ACME", "球阀 BV-100"]
    finally:
        set_gateway(None)


def test_kb_rbac_and_isolation(client, mock_crawler):
    h = register(client, "a@x.com")
    _, hs = add_user(client, h, "s@x.com", "sales")
    hb = register(client, "b@y.com", "公司B")
    assert client.post("/api/v1/kb/crawls", headers=hs, json={"urls": ["http://acme.test/"], "rights_confirmed": True}).status_code == 403
    client.post("/api/v1/kb/crawls", headers=h, json={"urls": ["http://acme.test/"], "rights_confirmed": True})
    assert client.get("/api/v1/kb/items", headers=hs).json()["total"] > 0  # 业务员可读
    assert client.get("/api/v1/kb/items", headers=hb).json()["total"] == 0
    assert client.get("/api/v1/kb/crawls", headers=hb).json() == []


def test_kb_item_version_conflict(client):
    h = register(client, "a@x.com")
    it = client.post("/api/v1/kb/items", headers=h, json={"type": "faq", "title": "MOQ?", "content": "100 pcs"}).json()
    assert client.patch(f"/api/v1/kb/items/{it['id']}", headers=h, json={"content": "200", "version": 1}).status_code == 200
    r = client.patch(f"/api/v1/kb/items/{it['id']}", headers=h, json={"content": "300", "version": 1})
    assert r.json()["error"]["code"] == "COMMON_VERSION_CONFLICT"
