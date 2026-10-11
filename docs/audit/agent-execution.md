# 持久 Agent 执行与恢复（AI-BILL-11）

确认、审批和恢复先提交数据库排队状态及预占，再通知Celery。每分钟扫描排队、过期执行和未处理控制请求；broker不可用时任务仍保留。生产须保持非eager worker配置；测试环境的eager模式在HTTP事务提交后执行。

工作进程使用专用物理数据库连接持有会话级任务锁，配合执行令牌和租约防止重复领取。领取统一执行每租户最多3个running限制，恢复不能绕过。工具始终使用原创建人当前权限，审批人不会替换执行身份；失效或已撤权用户不能继续。

每步执行前提交收据；业务结果、游标、点数和日志一起提交。SQL错误先回滚，再查询账本、记录错误和释放预占。已完成步骤在崩溃后保留。纯数据库步骤可安全重跑；外部补全、验证或背调调用结果不明时暂停并保留证据，不盲目重试。仅所有者且有审批权限、提供供应商明确未执行证据后才能重新排队；否则人工核对或取消，不能凭空补记收费。

暂停/取消写入独立控制表，不等待工具持有的钱包锁；当前步骤事务结束后生效。迟到控制也由扫描器处理。钱包写锁保持排他性，改用NO KEY UPDATE使控制记录的外键检查能并发完成。取消保留未知外部结果收据。页面主动刷新进度，对刷新失败退避重试，显示控制待生效及未知结果说明。

迁移0024新增租约、步骤收据和控制表，将旧版running任务暂停为legacy_running_unknown；没有旧执行证据时不得直接恢复。已有收据/控制或活动任务时拒绝破坏性downgrade。回滚应用前停止worker并核对任务，保留新证据表及钱包锁边界，不能直接恢复旧同步执行器。

初始测试曾被未充值夹具提前挡住，已纠正且不算有效复现。只读提取原版确认函数后，有效断言得到completed而非queued；修复后数据库域26项通过（76.61s），包括真实并发领取、工具阻塞时暂停/取消、SQL回滚、崩溃保留进度、外部未知结果核对、迟到取消、恢复上限、撤权和钱包并发。前端2项新增断言先失败，修复后连同提交幂等6项通过（26.12s）；lint/type/build通过。外部工具均为本地确定性替身，没有调用真实供应商。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest backend/tests/test_audit_agent_queue.py backend/tests/test_audit_agent_recovery.py backend/tests/test_p5.py backend/tests/test_audit_wallet.py -q
npm --prefix frontend test -- --maxWorkers=1 tests/agent-execution.test.tsx tests/submissions.test.tsx
npm --prefix frontend run lint
npm --prefix frontend run build
```
