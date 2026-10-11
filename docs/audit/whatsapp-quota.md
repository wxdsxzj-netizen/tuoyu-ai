# WhatsApp日额度（AI-BILL-07）

UTC日额度从已持久化外发记录计算，列表展示与发送闸门共用同一口径，不依赖每天归零的调度。账号锁串行化并发发送，并在锁后刷新账号，阻止两个会话争抢最后一个名额同时通过。成功/mock及未知状态占额度，明确失败和入站不计，软删除不能释放已用额度。发送按开始时的UTC日期归属，跨月午夜也自然切换。

同域检查补齐：暂停账号不能发送，模板必须属于当前账号且已审批，会话窗限当前账号，联系人仍须当前租户可见。此项没有新增外发结果幂等协议：供应商已接受但数据库事务失败仍需人工核查，不能把额度锁理解成外部发送exactly-once。未进行任何真实WhatsApp调用。

原3项失败断言；修复后4项新增（UTC午夜、双会话最后名额、旧计数与展示、账号/模板约束）和B6/B7/B8共21 passed / 22.51s，真实本地PostgreSQL，mock适配器。Ruff通过，无迁移，旧sent_today仅作兼容字段，读取以消息记录为准。

命令：TL_SECRETS_FILE=/dev/null并使用隔离数据库运行 .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_whatsapp_quota.py backend/tests/test_b6.py backend/tests/test_b7b8.py backend/tests/test_b7b8_full.py。回退会恢复跨日无法发送与并发超额问题。
