"""AI 评测（X1）：固定评测集 × 当前提示词版本 / 模型 → 通过率；低于门槛判定失败（发布前 CI 跑，失败阻断）。
mode="rules"（默认 / CI）：只跑本地规则，不访问外网，结果确定；
mode="model"：每条用例都调用当前网关的真实模型（分类 / 意图 / QA 判定 / 注入识别 / 需求抽取），用于模型上线前验收。"""
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
    from app.agent.planner import rule_parse  # rules 模式：纯规则，绝不调用模型
    got = rule_parse(case["in"])
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


def _classify_m(case):
    from app.outreach.inbound import classify_llm
    got = classify_llm(case["in"]["subject"], case["in"]["body"])
    got = got[0] if got else "error:no_model_output"
    exp = case["expect"]
    return (got in exp if isinstance(exp, list) else got == exp), got


def _intent_m(case):
    from app.agent.planner import parse
    got = parse(case["in"])
    if got.get("source") != "llm":
        return False, "error:fell_back_to_rules"
    ok = all(got.get(k) == v for k, v in case["expect"].items())
    return ok, {k: got.get(k) for k in case["expect"]}


def _qa_m(case):
    from app.llm import judges
    got = judges.qa_verdict(case["in"] + "\n\nBest regards", kb_text="ISO 9001 certified ball valves")
    return got == case["expect"], got


def _injection_m(case):
    from app.llm import judges
    got = judges.injection(case["in"])
    return got == case["expect"], got


def _needs_m(case):
    from app.llm import judges
    d = judges.needs(case["in"])
    return all(d[k] == v for k, v in case["expect"].items()), {k: d[k] for k in ("quantity", "use", "timing")}


MODEL_FNS = {"classify": _classify_m, "intent": _intent_m, "qa": _qa_m, "injection": _injection_m, "needs": _needs_m}

SUITES = {"classify": (_classify, "classify"), "intent": (_intent, "intent"), "qa": (_qa, "writer"), "injection": (_injection, "writer"),
          "needs": (_needs, "reply")}


def run_suite(db: Session | None, name: str, mode: str = "rules") -> dict:
    fn, pv = SUITES[name]
    if mode == "model":
        fn = MODEL_FNS[name]
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
    if mode == "model":
        from app.llm.gateway import get_gateway
        pr = get_gateway().primary
        model = f"{pr.name}:{getattr(pr, 'model', 'mock-1')}"
    else:
        model = "rules"
    res = {"suite": name, "prompt_version": PROMPT_VERSIONS[pv], "model": str(model), "total": total, "passed": passed, "score": score,
           "threshold": data["threshold"], "ok": score >= data["threshold"], "failures": fails}
    if db is not None:
        from app.core.db import bypass_tenant
        with bypass_tenant():
            db.add(EvalRun(**res))
            db.flush()
    return res


def run_all(db: Session | None = None, mode: str = "rules") -> dict:
    rs = [run_suite(db, n, mode) for n in SUITES]
    return {"ok": all(r["ok"] for r in rs), "suites": rs}
