# 聊天首响与统一回复完成（O-11）

聊天人工回复此前只保存消息，不结算询盘首响。现在成功的聊天回复、SMTP回复和人工核实已送达回复共用同一领域更新：会话已回复/人工模式、最后消息时间、询盘第一次响应、客户有效跟进时间。后续回复不覆盖第一次响应，迟到的核查不会倒退最后消息或跟进时间。失败/未知SMTP结果不调用成功事件。

聊天消息与这些字段在同一数据库事务提交；租户锁和会话锁防止两次客服回复覆盖消息数组，SLA任务也按相同租户锁顺序检查，避免已经回复后仍使用旧快照改派。非法输入与跨租户请求不进入写路径。聊天交付指访客可轮询到服务器消息，并不代表访客已经阅读。

原代码2项核心首响断言失败；另1项无副作用测试最初误用了422，改为应用既有400契约后通过。修复后真实隔离PostgreSQL共24项通过（27.81s）：

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_widget_response.py backend/tests/test_b4.py backend/tests/test_audit_delivery_intents.py
```

包括真实事务回滚、首次响应不变、到期SLA不再改派、其他租户拒绝及既有发送意图回归。不涉及迁移，无真实外发。跨会话询盘聚合另由O-08处理。
