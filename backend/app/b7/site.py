"""独立站与 SEO（M9）：按租户生成落地页/产品页，路径 /s/{slug}/… 发布；sitemap/robots；基础 on-page SEO。不要求真实域名。"""
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import Product, Site, SitePage, Tenant
from app.outreach.qa import DEFAULT_FORBIDDEN

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


def _md_to_html(md: str) -> str:
    """极简 Markdown → HTML（标题/段落/列表/链接），避免引入重依赖。"""
    out, in_ul = [], False
    for line in (md or "").splitlines():
        if line.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h1>{_esc(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{_esc(line[3:])}</h2>")
        elif line.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif not line.strip():
            if in_ul:
                out.append("</ul>"); in_ul = False
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<p>{_inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _esc(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _inline(t: str) -> str:
    t = _esc(t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def create_site(db: Session, *, slug: str, title: str, lang: str, user_id) -> Site:
    slug = slug.lower().strip()
    if not SLUG_RE.match(slug):
        raise AppError("COMMON_VALIDATION", details={"slug": "小写字母数字连字符，3–64 位"})
    with bypass_tenant():
        if db.scalar(select(Site.id).where(Site.slug == slug, Site.deleted_at.is_(None))):
            raise AppError("COMMON_ALREADY_EXISTS", details={"slug": slug})
    s = Site(slug=slug, title=title, lang=lang or "en", theme={"primary": "#1677ff"}, seo={"title": title}, created_by=user_id)
    db.add(s)
    db.flush()
    home = SitePage(site_id=s.id, path="/", page_type="home", title=title,
                    body_md=f"# {title}\n\nWelcome. We supply quality industrial products worldwide.\n\n- Reliable delivery\n- Export documentation\n- Technical support\n",
                    seo_title=title, seo_description=f"{title} — export manufacturer", sort=0, created_by=user_id)
    db.add(home)
    db.flush()
    return s


def generate_product_pages(db: Session, site: Site, user_id, limit: int = 50) -> int:
    n = 0
    for p in db.scalars(select(Product).where(Product.deleted_at.is_(None), Product.status == "active").limit(limit)):
        path = f"/products/{re.sub(r'[^a-z0-9]+', '-', (p.model or p.name_en or p.name_zh or str(p.id)).lower()).strip('-')[:80]}"
        if db.scalar(select(SitePage.id).where(SitePage.site_id == site.id, SitePage.path == path, SitePage.deleted_at.is_(None))):
            continue
        title = p.name_en or p.name_zh or p.model or "Product"
        body = f"# {title}\n\n**Model:** {p.model or '—'}\n\n{p.description or p.name_zh or ''}\n\n## Specifications\n\n- Contact us for datasheet and MOQ.\n"
        # 承诺边界：去掉禁用承诺词
        for bad in DEFAULT_FORBIDDEN:
            body = re.sub(re.escape(bad), "[redacted]", body, flags=re.I)
        db.add(SitePage(site_id=site.id, path=path, page_type="product", title=title, body_md=body, product_id=p.id,
                        seo_title=f"{title} | {site.title}", seo_description=(p.description or title)[:500], sort=10 + n, created_by=user_id))
        n += 1
    db.flush()
    return n


def publish(db: Session, site: Site) -> Site:
    if not db.scalar(select(SitePage.id).where(SitePage.site_id == site.id, SitePage.deleted_at.is_(None))):
        raise AppError("COMMON_INVALID_STATE", details={"site": "至少一页"})
    site.status, site.published_at = "published", utcnow()
    db.flush()
    return site


def render_page(db: Session, slug: str, path: str) -> dict:
    with bypass_tenant():
        site = db.scalar(select(Site).where(Site.slug == slug, Site.status == "published", Site.deleted_at.is_(None)))
    if not site:
        raise AppError("COMMON_NOT_FOUND")
    path = path if path.startswith("/") else "/" + path
    with bypass_tenant():  # pages are tenant-scoped; need tenant context
        from app.core.db import tenant_context
        with tenant_context(site.tenant_id):
            db.info["tenant_id"] = site.tenant_id
            page = db.scalar(select(SitePage).where(SitePage.site_id == site.id, SitePage.path == path, SitePage.deleted_at.is_(None)))
    if not page:
        raise AppError("COMMON_NOT_FOUND")
    seo_title = page.seo_title or page.title
    seo_desc = page.seo_description or site.seo.get("description") or site.title
    primary = (site.theme or {}).get("primary", "#1677ff")
    html = f"""<!DOCTYPE html><html lang="{site.lang}"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{_esc(seo_title)}</title>
<meta name="description" content="{_esc(seo_desc)}"/>
<meta property="og:title" content="{_esc(seo_title)}"/><meta property="og:description" content="{_esc(seo_desc)}"/>
<link rel="canonical" href="/s/{site.slug}{page.path}"/>
{"<meta name=\"robots\" content=\"noindex\"/>" if page.noindex else ""}
<style>body{{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem;color:#222}}
a{{color:{primary}}} h1{{color:{primary}}} nav a{{margin-right:1rem}} footer{{margin-top:3rem;color:#888;font-size:12px}}</style>
</head><body>
<nav><a href="/s/{site.slug}/">{_esc(site.title)}</a><a href="/s/{site.slug}/sitemap.xml">Sitemap</a></nav>
<article>{_md_to_html(page.body_md)}</article>
<footer>© {_esc(site.title)} · Generated by TradeLeads site builder · Path template /s/{site.slug}/</footer>
</body></html>"""
    return {"html": html, "content_type": "text/html; charset=utf-8"}


def sitemap_xml(db: Session, slug: str) -> str:
    with bypass_tenant():
        site = db.scalar(select(Site).where(Site.slug == slug, Site.status == "published", Site.deleted_at.is_(None)))
    if not site:
        raise AppError("COMMON_NOT_FOUND")
    from app.core.db import tenant_context
    with tenant_context(site.tenant_id):
        db.info["tenant_id"] = site.tenant_id
        pages = list(db.scalars(select(SitePage).where(SitePage.site_id == site.id, SitePage.deleted_at.is_(None), SitePage.noindex.is_(False))))
    urls = "\n".join(f'  <url><loc>/s/{slug}{p.path}</loc><lastmod>{(p.updated_at or utcnow()).date().isoformat()}</lastmod></url>' for p in pages)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>'


def robots_txt(slug: str) -> str:
    return f"User-agent: *\nAllow: /s/{slug}/\nSitemap: /s/{slug}/sitemap.xml\n"


def fingerprint(html: str, headers: dict | None = None) -> dict:
    """基础网站技术指纹（本地规则，不依赖外部服务）。"""
    h = (html or "").lower()
    hdr = {k.lower(): v for k, v in (headers or {}).items()}
    techs = []
    for name, rx in [("WordPress", r"wp-content|wordpress"), ("Shopify", r"cdn.shopify"), ("React", r"data-reactroot|_next/static"),
                     ("Vue", r"__vue__|data-v-"), ("Google Analytics", r"google-analytics.com|gtag/js"), ("Cloudflare", r"cloudflare")]:
        if re.search(rx, h):
            techs.append(name)
    if "server" in hdr:
        techs.append(f"Server:{hdr['server'][:40]}")
    return {"technologies": techs, "detected_at": utcnow().isoformat()}
