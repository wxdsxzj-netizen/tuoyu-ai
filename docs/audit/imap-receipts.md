# 入站回执和 IMAP 游标（O-12）

0023 保存邮箱 UIDVALIDITY、64位 UID 和同步错误，并新增独立入站回执。邮箱/世代/UID 与邮箱/规范化发件人/Message-ID 分别形成去重身份；数据库唯一约束、租户写锁和邮箱事务 advisory lock 保证并发轮询不重复执行分类、询盘计数、退订等业务。回执仅保存摘要及对象ID，不复制邮件正文或地址。

UIDVALIDITY 变化从新世代起点重新读，Message-ID 回执过滤旧信。升级前已经保存的消息也先核对，避免上线后重复执行业务。没有 UIDVALIDITY 就停止；FETCH 空结果或错误在该UID停止，不越过缺失邮件。游标、回执、消息及业务结果在同一事务提交；失败后可重试。定时任务每个邮箱独立提交，先回滚真实 SQL 异常再处理下一个邮箱。

旧邮箱若已有正数游标但没有记录世代，系统不能证明当前世代与旧游标匹配，会保留游标并显示人工核查提示。上线操作人员须依据原邮箱记录核对世代/处理范围，建立基线后恢复；不得盲目写入世代或重放历史外部动作。本次未连接或修改任何生产邮箱。

专项最初4项失败。最终新增11项 IMAP 回归和5项询盘关联回归全部通过（18.11s），使用真实隔离 PostgreSQL、多连接竞争和 SQL 失败注入；IMAP 服务端响应为确定性协议替身，未宣称真实服务商端到端。更早组合检查60项通过，1项测试错误地检查异常字符串而非 AppError.message；修正断言后上述16项全部通过。组合检查中其余已通过用例未因断言修改重复执行；完整套件将在本批冻结后运行。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_imap_receipts.py backend/tests/test_audit_inquiry_links.py
```

迁移由0022前进到0023；存在回执或超出原32位范围的UID时拒绝破坏性降级。回退应用也应保留回执表和游标世代，避免恢复重复处理缺陷。首次正式接入仍须用隔离的真实 IMAP 邮箱验证服务商协议差异。
