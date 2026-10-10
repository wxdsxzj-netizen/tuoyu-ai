"""制裁 / 管制筛查（M5-08 / M5-11）。

名单来源（至少 2 个，均为公开数据、无需 Key）：
1. 美国商务部综合筛查名单 CSL（合并商务部 / 国务院 / 财政部 11 份名单）
2. 美国财政部 OFAC SDN 名单（sdn.csv + alt.csv 别名），作为第二来源冗余
匹配：名称规范化（去公司后缀 / 标点 / 大小写）+ 别名；分词倒排召回候选，再用 Jaro-Winkler 与词集合相似度取最大值；
≥ 阈值（默认 0.85）写入复核队列（SM-20）。名单超过 72 小时未更新则结果标记“名单过期”。
"""
import csv
import hashlib
import io
import logging
import re
import unicodedata
import uuid
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import bypass_tenant, utcnow
from app.data.normalize import name_similarity
from app.models import Company, Contact, SanctionEntry, SanctionListVersion, SanctionToken, ScreeningHit

log = logging.getLogger("sanctions")
csv.field_size_limit(10_000_000)
NORM_VERSION = "2"  # 规范化 / 匹配算法变更时递增，强制重建名单索引

SUFFIX = {"co", "company", "corp", "corporation", "inc", "incorporated", "ltd", "limited", "llc", "plc", "gmbh", "ag", "sa",
          "sas", "srl", "spa", "bv", "nv", "oy", "ab", "as", "jsc", "ojsc", "pjsc", "cjsc", "ooo", "zao", "oao", "pte", "pvt",
          "kg", "the", "of", "and", "llp", "lp", "group", "holding", "holdings", "international", "intl", "trading", "fze", "fzco",
          "sarl", "bhd", "sdn", "tbk", "pt", "cv", "de", "y", "et", "la", "le"}
STOP_TOKENS = SUFFIX | {"a", "an", "for", "in", "al", "el"}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and "))
    toks = [t for t in s.split() if t not in SUFFIX and len(t) > 1]
    return " ".join(toks)


def tokens(n: str) -> set[str]:
    return {t for t in n.split() if len(t) >= 3 and t not in STOP_TOKENS}


def _token_side(xs: list[str], ys: list[str]) -> float:
    tot = 0.0
    for x in xs:
        best = max((name_similarity(x, y) for y in ys), default=0.0)
        tot += best if best >= 0.85 else 0.0  # 单词相似度低于 0.85 视为不匹配（避免 JW 前缀偏置）
    return tot / max(len(xs), 1)


def similarity(a: str, b: str) -> float:
    """双向词集合匹配（容忍词序颠倒与单词拼写差异）+ 长度相近时的整串 Jaro-Winkler。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    tok = (_token_side(ta, tb) + _token_side(tb, ta)) / 2
    whole = 0.0
    if min(len(a), len(b)) >= 8 and min(len(a), len(b)) / max(len(a), len(b)) >= 0.85:
        whole = name_similarity(a, b)
    return round(max(tok, whole), 4)


# ---------------- 解析 ----------------
def _country_codes(addresses: str) -> list[str]:
    out = []
    for part in (addresses or "").split(";"):
        last = part.strip().rsplit(",", 1)[-1].strip()
        if len(last) == 2 and last.isalpha() and last.isupper():
            out.append(last)
    return sorted(set(out))


def parse_csl(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        name = (r.get("name") or "").strip()
        if not name:
            continue
        alts = [a.strip() for a in (r.get("alt_names") or "").split(";") if a.strip()]
        rows.append({"source": "csl", "list_name": (r.get("source") or "CSL")[:256], "external_id": (r.get("_id") or r.get("id") or "")[:64],  # noqa: E501
                     "entity_type": (r.get("type") or "")[:32] or None, "name": name, "aliases": alts,
                     "countries": _country_codes(r.get("addresses") or ""), "addresses": r.get("addresses") or None,
                     "programs": r.get("programs") or None, "source_url": r.get("source_information_url") or None})
    return rows


def parse_ofac(sdn_text: str, alt_text: str | None) -> list[dict]:
    """OFAC 旧版 CSV 无表头：sdn = ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type, Tonnage, GRT,
    Vess_flag, Vess_owner, Remarks；alt = ent_num, alt_num, alt_type, alt_name, alt_remarks。空值为 -0-。"""
    def clean(v):
        v = (v or "").strip()
        return None if v in ("-0-", "") else v
    aliases: dict[str, list[str]] = {}
    if alt_text:
        for r in csv.reader(io.StringIO(alt_text)):
            if len(r) >= 4 and clean(r[3]):
                aliases.setdefault(r[0].strip(), []).append(clean(r[3]))
    rows = []
    for r in csv.reader(io.StringIO(sdn_text)):
        if len(r) < 4 or not clean(r[1]) or not r[0].strip().isdigit():
            continue
        rows.append({"source": "ofac_sdn", "list_name": "OFAC SDN - Treasury", "external_id": r[0].strip(),
                     "entity_type": (clean(r[2]) or "entity")[:32], "name": clean(r[1]), "aliases": aliases.get(r[0].strip(), []),
                     "countries": [], "addresses": None, "programs": clean(r[3]),
                     "source_url": "https://sanctionssearch.ofac.treas.gov/"})
    return rows


def load_entries(db: Session, source: str, rows: list[dict], raw_sha: str) -> SanctionListVersion:
    """整版替换：写新版本 → 删除旧版本条目（倒排随级联删除）。"""
    with bypass_tenant():
        v = SanctionListVersion(source=source, sha256=raw_sha, entry_count=len(rows), status="active")
        db.add(v)
        db.flush()
        entries, toks = [], []
        for r in rows:
            names = sorted({n for n in [norm_name(r["name"])] + [norm_name(a) for a in r["aliases"]] if n})
            eid = uuid.uuid4()
            entries.append({"id": eid, "version_id": v.id, "source": source, "list_name": r["list_name"],
                            "external_id": r["external_id"], "entity_type": r["entity_type"], "name": r["name"][:2000],
                            "names_normalized": names, "countries": r["countries"], "addresses": r["addresses"],
                            "programs": r["programs"], "source_url": r["source_url"]})
            for t in set().union(*[tokens(n) for n in names]) if names else set():
                toks.append({"token": t[:64], "entry_id": eid})
        for i in range(0, len(entries), 2000):
            db.execute(SanctionEntry.__table__.insert(), entries[i:i + 2000])
        for i in range(0, len(toks), 5000):
            db.execute(SanctionToken.__table__.insert(), toks[i:i + 5000])
        old = db.scalars(select(SanctionListVersion).where(SanctionListVersion.source == source, SanctionListVersion.id != v.id,
                                                           SanctionListVersion.status == "active")).all()
        for o in old:
            o.status = "superseded"
            db.execute(delete(SanctionEntry).where(SanctionEntry.version_id == o.id))
        db.flush()
    return v


def refresh_lists(db: Session, client: httpx.Client | None = None) -> dict:
    s = get_settings()
    client = client or httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": s.crawl_user_agent})
    out = {}
    for source in ("csl", "ofac_sdn"):
        try:
            if source == "csl":
                text, sha = _fetch(client, s.sanctions_csl_url)
                rows = parse_csl(text)
            else:
                sdn, sha1 = _fetch(client, s.sanctions_ofac_sdn_url)
                alt, sha2 = _fetch(client, s.sanctions_ofac_alt_url)
                sha = hashlib.sha256((sha1 + sha2).encode()).hexdigest()
                rows = parse_ofac(sdn, alt)
            sha = hashlib.sha256((sha + NORM_VERSION).encode()).hexdigest()
            if len(rows) < 10:
                raise ValueError(f"名单条目异常少：{len(rows)}")
            with bypass_tenant():
                cur = db.scalar(select(SanctionListVersion).where(SanctionListVersion.source == source,
                                                                 SanctionListVersion.status == "active"))
            if cur and cur.sha256 == sha:
                cur.fetched_at = utcnow()  # 内容未变：只刷新时间
                out[source] = {"status": "unchanged", "count": cur.entry_count}
            else:
                v = load_entries(db, source, rows, sha)
                out[source] = {"status": "updated", "count": v.entry_count}
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.exception("名单更新失败 %s", source)
            with bypass_tenant():
                db.add(SanctionListVersion(source=source, sha256="-", entry_count=0, status="failed", error=str(e)[:1000]))
            db.commit()
            out[source] = {"status": "failed", "error": str(e)[:300]}
    return out


def _fetch(client, url) -> tuple[str, str]:
    r = client.get(url)
    r.raise_for_status()
    b = r.content
    return b.decode("utf-8-sig", errors="replace"), hashlib.sha256(b).hexdigest()


def list_status(db: Session) -> dict:
    s = get_settings()
    with bypass_tenant():
        rows = db.scalars(select(SanctionListVersion).where(SanctionListVersion.status == "active")).all()
        last_fail = db.scalar(select(SanctionListVersion).where(SanctionListVersion.status == "failed")
                              .order_by(SanctionListVersion.fetched_at.desc()).limit(1))
    newest = max((r.fetched_at for r in rows), default=None)
    stale = newest is None or newest < utcnow() - timedelta(hours=s.sanctions_stale_hours)
    return {"sources": [{"source": r.source, "count": r.entry_count, "fetched_at": r.fetched_at.isoformat(), "version_id": str(r.id)}
                        for r in rows],
            "stale": stale, "last_updated": newest.isoformat() if newest else None,
            "last_failure": {"source": last_fail.source, "at": last_fail.fetched_at.isoformat(), "error": last_fail.error} if last_fail else None}  # noqa: E501


# ---------------- 筛查 ----------------
def match_name(db: Session, name: str, country: str | None = None, threshold: float | None = None, limit: int = 10) -> list[dict]:
    threshold = threshold if threshold is not None else get_settings().sanctions_threshold
    n = norm_name(name)
    toks = tokens(n)
    if not n or not toks:
        return []
    with bypass_tenant():
        cand_ids = db.scalars(select(SanctionToken.entry_id).where(SanctionToken.token.in_(list(toks)))
                              .group_by(SanctionToken.entry_id).order_by(func.count().desc()).limit(500)).all()
        if not cand_ids:
            return []
        entries = db.scalars(select(SanctionEntry).where(SanctionEntry.id.in_(cand_ids))).all()
    hits = []
    for e in entries:
        best, best_name = 0.0, ""
        for cand in e.names_normalized:
            sc = similarity(n, cand)
            if sc > best:
                best, best_name = sc, cand
        if country and e.countries and country.upper() not in e.countries:
            best = round(best * 0.95, 4)  # 国家不一致轻微降权（不排除）
        if best >= threshold:
            hits.append({"entry": e, "score": best, "matched_name": best_name})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def screen_subject(db: Session, subject, subject_type: str) -> str:
    """筛查公司或联系人，写复核队列，返回 sanction_status（clear / pending / confirmed）。"""
    name = subject.name if subject_type == "company" else subject.full_name
    country = subject.country
    hits = match_name(db, name, country)
    existing = {(h.entry_id): h for h in db.scalars(select(ScreeningHit).where(ScreeningHit.subject_id == subject.id,
                                                                                ScreeningHit.deleted_at.is_(None)))}
    matched_keys = {(h["entry"].source, h["entry"].external_id) for h in hits}
    for old in existing.values():  # 名单或算法更新后不再匹配的待复核命中：自动撤下（已复核的保留留档）
        if old.status == "pending" and (old.source, old.external_id) not in matched_keys:
            old.deleted_at = utcnow()
    for h in hits:
        e = h["entry"]
        key = next((x for x in existing.values() if x.source == e.source and x.external_id == e.external_id), None)
        if key:
            key.entry_id, key.list_version_id, key.score = e.id, e.version_id, h["score"]
            continue
        db.add(ScreeningHit(subject_type=subject_type, subject_id=subject.id, subject_name=name[:256], entry_id=e.id,
                            entry_name=e.name[:2000], list_name=e.list_name, source=e.source, external_id=e.external_id,
                            score=h["score"], matched_name=h["matched_name"], list_version_id=e.version_id))
    db.flush()
    statuses = {x.status for x in db.scalars(select(ScreeningHit).where(ScreeningHit.subject_id == subject.id,
                                                                         ScreeningHit.deleted_at.is_(None)))}
    st = "confirmed" if "confirmed" in statuses else ("pending" if "pending" in statuses else "clear")
    subject.sanction_status = st
    if subject_type == "company":
        subject.sanction_screened_at = utcnow()
    return st


def rescreen_tenant(db: Session) -> dict:
    counts = {"companies": 0, "contacts": 0, "pending": 0}
    for c in db.scalars(select(Company).where(Company.deleted_at.is_(None))):
        counts["companies"] += 1
        counts["pending"] += screen_subject(db, c, "company") == "pending"
    for p in db.scalars(select(Contact).where(Contact.deleted_at.is_(None))):
        counts["contacts"] += 1
        counts["pending"] += screen_subject(db, p, "contact") == "pending"
    return counts


def recompute_status(db: Session, subject_type: str, subject_id) -> None:
    model = Company if subject_type == "company" else Contact
    subj = db.get(model, subject_id)
    if not subj:
        return
    statuses = {x.status for x in db.scalars(select(ScreeningHit).where(ScreeningHit.subject_id == subject_id,
                                                                         ScreeningHit.deleted_at.is_(None)))}
    subj.sanction_status = "confirmed" if "confirmed" in statuses else ("pending" if "pending" in statuses else "clear")
