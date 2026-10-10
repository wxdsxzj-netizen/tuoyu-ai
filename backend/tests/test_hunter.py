"""Hunter.io 适配器：只用录制 / 模拟的 HTTP 响应，不发真实请求。"""
import httpx
import pytest

from app.compliance.verify import HunterVerifier, MillionVerifierVerifier
from app.core.config import Settings
from app.core.errors import AppError
from app.data.hunter import HunterClient, HunterContactProvider, _RateLimiter
from app.data.providers import MockContactProvider, build_provider

DOMAIN_SEARCH = {
    "data": {"domain": "intercom.com", "disposable": False, "webmail": False, "accept_all": True, "pattern": "{first}",
             "organization": "Intercom", "emails": [
                 {"value": "ciaran@intercom.com", "type": "personal", "confidence": 92, "sources": [{"domain": "github.com"}],
                  "first_name": "Ciaran", "last_name": "Lee", "position": "Support Engineer", "seniority": "senior",
                  "department": "it", "linkedin": None, "phone_number": None,
                  "verification": {"date": "2019-12-06", "status": "valid"}},
                 {"value": "eoghan@intercom.com", "type": "personal", "confidence": 88, "sources": [],
                  "first_name": "Eoghan", "last_name": "McCabe", "position": "CEO", "seniority": "executive",
                  "department": "executive", "linkedin": "https://linkedin.com/in/eoghan", "phone_number": "+1 415 000",
                  "verification": {"date": None, "status": "accept_all"}}]},
    "meta": {"results": 35, "limit": 10, "offset": 0, "params": {"domain": "intercom.com"}}}
FINDER = {"data": {"first_name": "Alexis", "last_name": "Ohanian", "email": "alexis@reddit.com", "score": 97, "domain": "reddit.com",
                   "accept_all": False, "position": "Cofounder", "linkedin_url": None, "phone_number": None, "company": "Reddit",
                   "sources": [], "verification": {"date": "2021-06-14", "status": "valid"}}, "meta": {}}
VERIFIER = {"data": {"status": "valid", "score": 100, "email": "patrick@stripe.com", "regexp": True, "gibberish": False,
                     "disposable": False, "webmail": False, "mx_records": True, "smtp_server": True, "smtp_check": True,
                     "accept_all": False, "block": False, "verification": {"date": "2026-09-14", "status": "valid"}, "sources": []},
            "meta": {"params": {"email": "patrick@stripe.com"}}}


def make(responses, **kw):
    """responses: list of (status, json) 或 callable(request)。"""
    calls = []
    seq = list(responses)

    def handler(req):
        calls.append(req)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        st, body = item
        return httpx.Response(st, json=body)
    sleeps = []
    c = HunterClient("k-123", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, **kw)
    return c, calls, sleeps


def test_domain_search_maps_people_and_uses_header_auth():
    c, calls, _ = make([(200, DOMAIN_SEARCH)])
    people = HunterContactProvider(c).search_people(domain="intercom.com", titles=["CEO"], limit=5)
    assert len(people) == 2
    assert people[0].email == "ciaran@intercom.com" and people[0].full_name == "Ciaran Lee"
    assert people[0].email_confidence == 0.92 and people[0].seniority == "senior"
    assert people[1].seniority == "c_level" and people[1].raw["hunter_verification"] == "accept_all"
    req = calls[0]
    assert req.url.path.endswith("/v2/domain-search")
    assert req.headers["X-API-KEY"] == "k-123" and "api_key" not in str(req.url)
    assert req.url.params["domain"] == "intercom.com" and req.url.params["job_titles"] == "CEO"
    assert req.url.params["limit"] == "5"


def test_email_finder():
    c, calls, _ = make([(200, FINDER)])
    p = HunterContactProvider(c).find_email(full_name="Alexis Ohanian", domain="reddit.com")
    assert p.email == "alexis@reddit.com" and p.email_confidence == 0.97
    assert calls[0].url.params["first_name"] == "Alexis" and calls[0].url.params["last_name"] == "Ohanian"
    assert "full_name" not in calls[0].url.params


def test_email_finder_claimed_451_is_not_processed():
    c, _, _ = make([(451, {"errors": [{"id": "claimed_email", "code": 451, "details": "..."}]})])
    prov = HunterContactProvider(c)
    assert prov.find_email(full_name="Jane Doe", domain="x.com") is None
    assert prov.claimed


@pytest.mark.parametrize("status,code", [(401, "DATA_PROVIDER_AUTH_FAILED"), (429, "DATA_PROVIDER_QUOTA_EXCEEDED"),
                                         (400, "DATA_PROVIDER_BAD_REQUEST")])
def test_error_mapping_no_retry(status, code):
    c, calls, sleeps = make([(status, {"errors": [{"id": "x", "code": status, "details": "d"}]})])
    with pytest.raises(AppError) as e:
        c.domain_search("a.com")
    assert e.value.code == code and len(calls) == 1 and not sleeps


def test_rate_limit_403_retries_then_succeeds():
    c, calls, sleeps = make([(403, {"errors": [{"id": "too_many_requests"}]}), (403, {}), (200, DOMAIN_SEARCH)])
    assert c.domain_search("intercom.com")["organization"] == "Intercom"
    assert len(calls) == 3 and sleeps[:2] == [2, 4]


def test_rate_limit_403_exhausted():
    c, calls, _ = make([(403, {})], max_retries=2)
    with pytest.raises(AppError) as e:
        c.domain_search("a.com")
    assert e.value.code == "DATA_PROVIDER_RATE_LIMITED" and len(calls) == 3


def test_server_error_then_unavailable():
    c, calls, _ = make([(502, {})], max_retries=1)
    with pytest.raises(AppError) as e:
        c.email_finder("a.com", full_name="A B")
    assert e.value.code == "DATA_PROVIDER_UNAVAILABLE" and len(calls) == 2


def test_network_error_retries():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=VERIFIER)
    c = HunterClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    assert c.email_verifier("patrick@stripe.com").status == "valid"


def test_verifier_202_polls_then_result_and_222_unknown():
    c, calls, sleeps = make([(202, {}), (200, VERIFIER)])
    v = c.email_verifier("patrick@stripe.com")
    assert v.status == "valid" and v.smtp_check and len(calls) == 2 and sleeps == [3]
    c2, _, _ = make([(222, {"errors": [{"id": "smtp_failure"}]})])
    assert c2.email_verifier("x@y.com").status == "unknown"


@pytest.mark.parametrize("hstatus,smtp,expected", [("valid", True, "valid"), ("invalid", False, "invalid"),
                                                   ("accept_all", True, "catch_all"), ("disposable", None, "risky"),
                                                   ("webmail", True, "valid"), ("webmail", None, "unknown"),
                                                   ("unknown", None, "unknown")])
def test_hunter_verifier_mapping(hstatus, smtp, expected):
    body = {"data": {**VERIFIER["data"], "status": hstatus, "smtp_check": smtp}}
    c, _, _ = make([(200, body)])
    assert HunterVerifier(c).verify("a@b.com").status == expected


def test_hunter_verifier_claimed_is_risky():
    c, _, _ = make([(451, {"errors": [{"id": "claimed_email"}]})])
    r = HunterVerifier(c).verify("a@b.com")
    assert r.status == "risky" and "CLAIMED_EMAIL" in r.reason_codes


def test_auto_provider_defaults_to_mock_without_key():
    assert isinstance(build_provider("auto", Settings(hunter_api_key="")), MockContactProvider)
    p = build_provider("auto", Settings(hunter_api_key="abc"))
    assert isinstance(p, HunterContactProvider) and p.client.api_key == "abc"


def test_env_var_name_hunter_api_key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "from-env")
    assert Settings().hunter_api_key == "from-env"


def test_rate_limiter_waits_when_per_second_exceeded():
    t = {"now": 100.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s
    rl = _RateLimiter(2, 100, clock=lambda: t["now"], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    assert slept and t["now"] >= 101.0


# ---------- MillionVerifier（模拟响应，字段按官方 v3 文档） ----------
def mv(body, status=200):
    sleeps = []
    return MillionVerifierVerifier("k", "https://api.millionverifier.com/api/v3/", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))), sleep=sleeps.append)


@pytest.mark.parametrize("result,expected", [("ok", "valid"), ("catch_all", "catch_all"), ("unknown", "unknown"),
                                             ("disposable", "risky"), ("invalid", "invalid")])
def test_millionverifier_mapping(result, expected):
    r = mv({"email": "a@b.com", "quality": "good", "result": result, "resultcode": 1, "subresult": "ok", "free": False,
            "role": result == "ok", "credits": 3454, "executiontime": 2, "error": "", "livemode": True}).verify("a@b.com")
    assert r.status == expected and r.provider == "millionverifier"


@pytest.mark.parametrize("err,code", [("insufficient_credits", "DATA_PROVIDER_QUOTA_EXCEEDED"),
                                      ("invalid_api_key", "DATA_PROVIDER_AUTH_FAILED")])
def test_millionverifier_errors(err, code):
    with pytest.raises(AppError) as e:
        mv({"result": "error", "error": err}).verify("a@b.com")
    assert e.value.code == code
