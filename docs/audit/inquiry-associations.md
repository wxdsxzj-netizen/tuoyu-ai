# 跨会话询盘关联（O-08）

同租户、72小时内、同企业域名/同邮箱的既有聚合规则保留。每个参与会话现在有持久`InquiryConversation`关联，原始会话、发件人和消息不搬移；界面分别提供带收件人及主题的会话链接，人工回复仍针对用户选中的真实会话。

后续来信按所有关联消息重评分，同步分配人和待处理交付包。已有交付包保持稳定ID并更新聚合需求/摘要，不覆盖已接受或已拒绝的历史。新增高风险证据进入人工复核并阻止接受原交付包。接受原交付时，询盘和关联会话一起进入原有客户/商机链路。

首响口径沿用原询盘级SLA：任一关联会话成功的人工回复结算该询盘第一次响应；合并不重启时钟，也不延后原期限。早于询盘创建的迟到投递核查不结算新询盘。所有关联会话均保留独立身份，不将多个收件人拼成一次外发。

新增0022只回填数据库已经记录的主会话关系。旧版本已丢失的次会话关联不能靠同域名猜回，历史受影响记录需人工核对；存在关联时降级拒绝直接删除。无生产迁移或历史数据猜测式修复。

原3项回归失败，修复后5项关联专项包括真实双数据库事务、原交付接受、跨租户拒绝、新高风险复核。并发测试发现普通`FOR UPDATE`会与两个已插入子记录的外键锁死锁，改为仍互斥的`FOR NO KEY UPDATE`；没有取消串行化。关联/聊天/P4/P5/B4扩展28项通过（122.95s）。前端真实组件测试验证两个会话链接各自的邮箱、主题和URL。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_inquiry_links.py backend/tests/test_audit_widget_response.py backend/tests/test_p4.py backend/tests/test_p5.py backend/tests/test_b4.py
npm --prefix frontend test -- --maxWorkers=2
npm --prefix frontend run lint
npm --prefix frontend run build
```

完整前端测试的默认并行度在本机造成3项5秒超时；单项均可通过，限制为2个worker后重新运行全部51项通过，lint/type/build通过。回退会重新丢失新会话关联与首响结算，须先保留关联表及交付证据。入站传输去重另由O-12处理。
