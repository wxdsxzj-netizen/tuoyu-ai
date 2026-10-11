# 深度问答证据（AI-BILL-09）

mock提供方或mock模型回显不作为用户答案。每个候选事实的全部证据编号都须可解析到报告证据，任何无证据、无效引用或推断事实都会使grounded为false，并保留推断/未核实标注。

真实模型答案仅在每个子句带规范引用、覆盖全部所选证据且不存在未知编号时采用，否则回退到可追溯的报告原文拼接，model记录为rules。grounded表示答案引用完整地对应当前报告，不代表外部来源真实性或语义事实已由独立第三方核验；此修复没有宣称模型引用校验可以证明事实。

原7项中6项失败；修复后7项新增数据库持久化回归+既有B5共12 passed / 13.30s。包含mock回显、仅首引用、散落编号、虚构引用、原文推断/坏编号和完整有效引用。全部模型调用为替身，无供应商费用。无迁移；Ruff通过。

命令：TL_SECRETS_FILE=/dev/null并使用隔离测试数据库运行 .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_research_grounding.py backend/tests/test_b5.py。
