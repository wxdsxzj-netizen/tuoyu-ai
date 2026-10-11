"""官网抓取（M1-01，BR-1-08）。

- 只抓取用户声明有权使用的网址（rights_confirmed），只在起始域名内广度优先；
- 遵守 robots.txt（无法获取 robots.txt 时按允许处理，4xx 视为允许，5xx 视为全部禁止）；
- 优先读取 sitemap.xml；产品页/首页优先；
- 拒绝内网 / 回环地址（防 SSRF），单页超时 20 秒，最多 max_pages 页。
"""
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.data.safe_http import UrlNotAllowed, check_url_allowed, safe_client, safe_get  # noqa: F401 re-export

SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|ico|pdf|zip|rar|mp4|mp3|avi|css|js|woff2?|ttf|xml|exe|dmg)$", re.I)
PRIORITY = re.compile(r"product|products|catalog|solution|about|company|certif|case|project|contact|产品|关于", re.I)


@dataclass
class Page:
    url: str
    title: str
    text: str
    meta_description: str = ""
    headings: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    skipped_robots: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s()-]{7,}\d")


def parse_html(url: str, html: str) -> tuple[Page, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    md = soup.find("meta", attrs={"name": "description"})
    meta = (md.get("content") or "").strip() if md else ""
    heads = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])][:50]
    links = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a["href"]))[0]
        if href.startswith(("http://", "https://")):
            links.append(href)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:20000]
    emails = sorted(set(EMAIL_IN_TEXT.findall(text)))[:20]
    phones = sorted({p.strip() for p in PHONE_IN_TEXT.findall(text)})[:10]
    return Page(url, title, text, meta, [h for h in heads if h], emails, phones), links


class Crawler:
    def __init__(self, client: httpx.Client | None = None, max_pages: int | None = None):
        s = get_settings()
        self.max_pages = max_pages or s.crawl_max_pages
        self.client = client or safe_client(timeout=s.crawl_timeout_s, headers={"User-Agent": s.crawl_user_agent})
        self.ua = s.crawl_user_agent
        self._robots: dict[str, RobotFileParser] = {}

    def _robot(self, base: str) -> RobotFileParser:
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = safe_get(base + "/robots.txt", client=self.client)
                if r.status_code >= 500:
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except UrlNotAllowed:
                rp.disallow_all = True
            except httpx.HTTPError:
                rp.allow_all = True
            self._robots[base] = rp
        return self._robots[base]

    def _sitemap(self, base: str) -> list[str]:
        try:
            r = safe_get(base + "/sitemap.xml", client=self.client)
            if r.status_code == 200 and "<loc>" in r.text:
                return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:500]
        except (httpx.HTTPError, UrlNotAllowed):
            pass
        return []

    def crawl(self, start_urls: list[str]) -> CrawlResult:
        res = CrawlResult()
        seen: set[str] = set()
        for start in start_urls:
            if len(res.pages) >= self.max_pages:
                break
            p = urlparse(start)
            base = f"{p.scheme}://{p.netloc}"
            host = p.hostname.removeprefix("www.") if p.hostname else ""
            robot = self._robot(base)
            prio, normal = deque([start]), deque()
            for u in self._sitemap(base):
                (prio if PRIORITY.search(u) else normal).append(u)
            while (prio or normal) and len(res.pages) < self.max_pages:
                url = (prio or normal).popleft()
                url = urldefrag(url)[0]
                uh = (urlparse(url).hostname or "").removeprefix("www.")
                if url in seen or uh != host or SKIP_EXT.search(urlparse(url).path):
                    continue
                seen.add(url)
                if not robot.can_fetch(self.ua, url):
                    res.skipped_robots.append(url)
                    continue
                try:
                    r = safe_get(url, client=self.client)
                    if r.status_code != 200 or "html" not in r.headers.get("content-type", "text/html"):
                        res.failed.append((url, f"HTTP {r.status_code}"))
                        continue
                except (httpx.HTTPError, UrlNotAllowed) as e:
                    res.failed.append((url, type(e).__name__))
                    continue
                page, links = parse_html(url, r.text)
                res.pages.append(page)
                for link in links:
                    if link not in seen and (urlparse(link).hostname or "").removeprefix("www.") == host:
                        (prio if PRIORITY.search(link) else normal).append(link)
        return res
