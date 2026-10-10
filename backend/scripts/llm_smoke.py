"""真实模型冒烟（约 8 次调用，产生少量费用）：官网知识抽取 / 回复分类 / 意图解析 / AI 写信 / 翻译。

用法（backend 目录）：../.venv/bin/python scripts/llm_smoke.py
需当前环境 TL_LLM_PRIMARY 指向真实提供方；mock 下拒绝运行。不发送任何邮件、不写数据库。
"""
import json
import sys

sys.path.insert(0, ".")
from app.core.config import get_settings  # noqa: E402
from app.llm.gateway import USAGE  # noqa: E402

s = get_settings()
if s.llm_primary == "mock":
    sys.exit("当前为 mock，未调用真实模型")
print(f"provider={s.llm_primary} model={getattr(s, s.llm_primary + '_model', '?')}\n")

# 1 官网知识抽取（示例页面）
from app.data.crawler import Page  # noqa: E402
from app.data.kb_extract import extract, to_items  # noqa: E402

pages = [Page(url="https://nbvalve.example/", title="Ningbo Valve Co. - Industrial Ball & Gate Valves",
              meta_description="Manufacturer of forged steel ball valves and gate valves since 2006.",
              headings=["About us", "Products", "Certifications"],
              text=("Ningbo Valve Co., Ltd. was founded in 2006 in Ningbo, China. We manufacture forged steel ball valves (DN15-DN300), "
                    "cast steel gate valves and check valves for HVAC, water treatment and oil & gas. Our factory covers 20,000 m2 with "
                    "180 employees and an annual capacity of 500,000 valves. Certifications: ISO 9001:2015, CE/PED, API 6D. "
                    "We export to Germany, Poland, the USA and the Middle East. MOQ 100 pcs, lead time 25-35 days, OEM available. "
                    "Contact: sales@nbvalve.example, +86 574 1234 5678."))]
data, how = extract(pages)
items = to_items(data)
print(f"1. 知识抽取 via={how} items={len(items)}")
for it in items[:6]:
    print("   -", json.dumps(it, ensure_ascii=False)[:150])

# 2 回复分类（生产路径：退订/退信走规则，其余走模型）
from app.outreach.inbound import classify  # noqa: E402

for subj, body in [("Re: valves", "Thanks. Could you quote 300 pcs DN50 ball valves, CIF Hamburg? Also MOQ for OEM?"),
                   ("Re: intro", "We are happy with our current supplier, please don't follow up."),
                   ("Re: intro", "Not now - maybe reach out again after Q1 when budgets open."),
                   ("Re: intro", "Please remove me from your list.")]:
    print("2. 分类", classify(subj, body, "buyer@acme.de"), "<=", body[:50])

# 3 意图解析
from app.agent.planner import parse  # noqa: E402

for t in ["帮我找 80 家沙特阿拉伯做水处理工程的承包商，联系采购和项目经理", "research kraft-hvac.de and tell me who decides purchasing"]:
    d = parse(t)
    print("3. 意图", {k: d.get(k) for k in ("type", "count", "country", "industry", "roles", "targets", "source")}, "<=", t[:30])

# 4 AI 写信（内存对象，不入库、不发送）
from app.models import Company, Contact, Sequence, SequenceStep  # noqa: E402
from app.outreach import qa  # noqa: E402
from app.outreach.writer import _llm  # noqa: E402

co = Company(name="Kraft HVAC GmbH", domain="kraft-hvac.de", country="DE", industry="HVAC distribution",
             description="Distributor of heating and ventilation components in Bavaria")
ct = Contact(full_name="Hans Becker", title="Purchasing Manager", email="hans@kraft-hvac.de", country="DE", company=co)
seq = Sequence(name="DE HVAC", language="en", icp_id=None)
step = SequenceStep(step_no=1, purpose="first", instructions="Offer a free sample of DN50 ball valves")
kb = {"text": "\n".join(f"{i.get('title', '')}: {i.get('content', '')}" for i in items) or pages[0].text,
      "forbidden": ["lowest price", "guaranteed delivery", "lifetime warranty"]}
draft = _llm(ct, co, kb, step, seq, "Lily Wang, Ningbo Valve")
if draft:
    r = qa.check(draft["subject"], draft["body"], kb_text=kb["text"], forbidden=kb["forbidden"], facts_used=draft["facts_used"],
                 personalization="medium", purpose="first")
    print(f"4. 写信 model={draft['model']} subject={draft['subject']!r}\n   body={draft['body'][:400]!r}\n"
          f"   facts={draft['facts_used'][:4]} QA vetoes={[v['code'] for v in r['vetoes']]} warnings={[w['code'] for w in r.get('warnings', [])][:5]}")  # noqa: E501
else:
    print("4. 写信 失败（已降级）")

# 5 翻译
from app.inbox.service import translate  # noqa: E402

out, via = translate("Sehr geehrte Damen und Herren, bitte senden Sie uns ein Angebot für 500 Stück Kugelhähne DN50 bis Ende November.")
print(f"5. 翻译 via={via}: {out}")

print("\nusage", USAGE)
