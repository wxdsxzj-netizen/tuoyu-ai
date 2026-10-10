"""AI 评测（X1）：固定评测集 × 当前提示词版本 / 模型 → 通过率；低于门槛判定失败（发布前 CI 跑，失败阻断）。
评测只调用本地规则 / 网关（测试环境为 mock），不访问外网。"""
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.b5.ai import PROMPT_VERSIONS, injection_hits
from app.models import EvalRun

DIR = Path(__file__).parent / "evalsets"


def _load(name):
    return json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))


def _classify(case):
    from app.outreach.inbound import classify_rules
    got = classify_rules(case["in"]["subject"], case["in"]["body"], case["in"]["from"])[0]
    exp = case["expect"]
    return (got in exp if isinstance(exp, list) else got == exp), got


def _intent(case):
    from app.agent.planner import parse
    got = parse(case["in"])
    ok = all(got.get(k) == v for k, v in case["expect"].items())
    return ok, {k: got.get(k) for k in case["expect"]}


def _qa(case):
    from app.outreach.qa import check
    r = check("Hello", case["in"] + "\n\nBest regards", kb_text="ISO 9001 certified ball valves", forbidden=[], facts_used=["x"],
              personalization="medium", purpose="first")
    got = "veto" if any(v["code"] == "FORBIDDEN_PROMISE" for v in r["vetoes"]) else "pass"
    return got == case["expect"], got


def _injection(case):
    got = bool(injection_hits(case["in"]))
    return got == case["expect"], got


def _needs(case):
    from app.crm.handoff import PRODUCT, QTY, USE, WHEN  # noqa: F401  规则抽取（不依赖知识库）
    got = {"quantity": bool(QTY.search(case["in"])), "use": bool(USE.search(case["in"])), "timing": bool(WHEN.search(case["in"]))}
    return all(got[k] == v for k, v in case["expect"].items()), got


SUITES = {"classify": (_classify, "classify"), "intent": (_intent, "intent"), "qa": (_qa, "writer"), "injection": (_injection, "writer"),
          "needs": (_needs, "reply")}


def run_suite(db: Session | None, name: str) -> dict:
    fn, pv = SUITES[name]
    data = _load(name)
    fails, passed = [], 0
    for i, c in enumerate(data["cases"]):
        try:
            ok, got = fn(c)
        except Exception as e:  # noqa: BLE001
            ok, got = False, f"error: {e}"
        if ok:
            passed += 1
        else:
            fails.append({"case": i, "input": c["in"], "expect": c["expect"], "got": got})
    total = len(data["cases"])
    score = round(passed / total, 3) if total else 0
    from app.llm.gateway import get_gateway
    g = get_gateway()
    model = getattr(g, "name", None) or type(g).__name__
    res = {"suite": name, "prompt_version": PROMPT_VERSIONS[pv], "model": str(model), "total": total, "passed": passed, "score": score,
           "threshold": data["threshold"], "ok": score >= data["threshold"], "failures": fails}
    if db is not None:
        from app.core.db import bypass_tenant
        with bypass_tenant():
            db.add(EvalRun(**res))
            db.flush()
    return res


def run_all(db: Session | None = None) -> dict:
    rs = [run_suite(db, n) for n in SUITES]
    return {"ok": all(r["ok"] for r in rs), "suites": rs}
