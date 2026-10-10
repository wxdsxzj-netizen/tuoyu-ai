"""规范化与判重（PRD M02 §8.3）。"""
import re
import unicodedata
from urllib.parse import urlparse

import pycountry
from rapidfuzz.distance import JaroWinkler

LEGAL_SUFFIXES = [
    "co., ltd.", "co. ltd", "co ltd", "company limited", "limited", "ltd", "llc", "l.l.c", "inc", "incorporated",
    "corp", "corporation", "gmbh & co. kg", "gmbh", "ag", "kg", "s.a.", "sa", "s.p.a.", "spa", "s.r.l.", "srl",
    "s.l.", "sl", "b.v.", "bv", "n.v.", "nv", "sp. z o.o.", "sp z oo", "oy", "ab", "as", "a/s", "aps", "pty ltd",
    "pty", "plc", "llp", "lp", "sarl", "sas", "s.a.s.", "eurl", "kft", "s.r.o.", "sro", "pte ltd", "pte", "sdn bhd",
    "bhd", "pt", "tbk", "jsc", "ooo", "zao", "ltda", "s.a. de c.v.", "sa de cv", "co", "company", "group",
]
ABBREV = {"intl": "international", "mfg": "manufacturing", "&": "and", "tech": "technology", "eng": "engineering"}
TRANSLIT = str.maketrans({"ü": "ue", "ö": "oe", "ä": "ae", "ß": "ss", "ø": "oe", "å": "aa", "æ": "ae"})
_suffix_re = re.compile(r"(?:[\s,]+(?:" + "|".join(re.escape(s) for s in sorted(LEGAL_SUFFIXES, key=len, reverse=True)) + r"))+\.?$")


def normalize_company_name(name: str) -> str:
    s = (name or "").strip().lower().translate(TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    for _ in range(3):
        s2 = _suffix_re.sub("", s).strip()
        if s2 == s or not s2:
            break
        s = s2
    s = re.sub(r"[^\w\s]", " ", s)
    words = [ABBREV.get(w, w) for w in s.split()]
    return " ".join(words)


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if "@" in v and "/" not in v:
        v = v.split("@", 1)[1]
    if "://" not in v:
        v = "http://" + v
    host = (urlparse(v).hostname or "").strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return host


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
GMAIL = {"gmail.com", "googlemail.com"}


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    if not EMAIL_RE.match(e):
        return None
    local, dom = e.split("@", 1)
    if dom in GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        dom = "gmail.com"
    return f"{local}@{dom}"


def is_valid_email(email: str | None) -> bool:
    return bool(email and EMAIL_RE.match(email.strip()))


_COUNTRY_ALIASES = {"uk": "GB", "england": "GB", "usa": "US", "america": "US", "uae": "AE", "korea": "KR",
                    "south korea": "KR", "russia": "RU", "vietnam": "VN", "中国香港": "HK", "香港": "HK",
                    "中国台湾": "TW", "台湾": "TW", "美国": "US", "英国": "GB", "德国": "DE", "法国": "FR",
                    "日本": "JP", "韩国": "KR", "印度": "IN", "巴西": "BR", "俄罗斯": "RU", "越南": "VN",
                    "澳大利亚": "AU", "加拿大": "CA", "墨西哥": "MX", "意大利": "IT", "西班牙": "ES",
                    "荷兰": "NL", "波兰": "PL", "阿联酋": "AE", "沙特": "SA", "新加坡": "SG", "马来西亚": "MY",
                    "泰国": "TH", "印度尼西亚": "ID", "印尼": "ID", "菲律宾": "PH", "土耳其": "TR", "中国": "CN"}


def normalize_country(value: str | None) -> str | None:
    """返回 ISO 3166-1 alpha-2；无法识别返回 None。"""
    if not value:
        return None
    v = value.strip()
    if v.lower() in _COUNTRY_ALIASES or v in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES.get(v.lower()) or _COUNTRY_ALIASES[v]
    if len(v) == 2 and pycountry.countries.get(alpha_2=v.upper()):
        return v.upper()
    if len(v) == 3 and (c := pycountry.countries.get(alpha_3=v.upper())):
        return c.alpha_2
    try:
        return pycountry.countries.lookup(v).alpha_2
    except LookupError:
        return None


def name_similarity(a: str, b: str) -> float:
    return JaroWinkler.similarity(a, b)


SUSPECT_THRESHOLD = 0.92
