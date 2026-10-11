# 审计修复跟踪

审计基线：`fdc9a4516ba88572742118c6d36ef6af61282c5f`。修复基线：`c2843e3e0573d3f37652bba636421510bd0ed516`。

修复分支：`fix/audit-c2843e3-local`。此清单覆盖报告全部 58 个编号。原始审计材料与本机环境日志单独保存，不随代码分支发布。

上游核销：AI-BILL-06 的 owner 空变量已修，后续调度修复见该项；Vite 的 /s 代理修正已保留，生产 Caddy 路由另行修复。

状态：已上游修复、已修且测试通过、待修、待业务决定、受环境限制。最终范围、提交对应、测试和回退说明见 [第五批版本记录](docs/audit/2026-10-11-batch05.md)，历史起点见 [首批版本记录](docs/audit/2026-10-11-batch01.md)。

| 编号 | 优先级 | 问题 | 当前状态 / 证据 |
|---|---|---|---|
| S1 | P1 | 匿名隐私请求可以对任意邮箱实施全平台抑制 | 已修且测试通过：4 项真实跨租户/核验/限流回归 + 相关既有回归 |
| S2 | P1 | SSRF 防护没有覆盖重定向与所有抓取入口 | 已修且测试通过：21 个定向函数/HTTP 栈测试；另有隔离数据库全套回归 |
| S3 | P1 | 幂等缓存先于认证返回 且遗漏 API key 身份边界 | 已修且测试通过：当前认证/权限、API key/租户范围、query/TTL、一次性秘密回归通过 |
| S4 | P1 | 幂等只缓存结果 无法阻止并发重复副作用 | 已修且测试通过：真实 PostgreSQL 并发 HTTP 仅一笔提交；未知结果不重试 |
| S5 | P1 | DSR 删除标记完成 但主要邮件副本及联系字段未擦除 | 已修且测试通过：普通副本、部分/孤儿ZIP和完成复核；用户已确认保留及共享来源人工关卡，未核查工单不得完成；见[说明](docs/audit/dsr-erasure.md) |
| S6 | P1 | 管理员可修改审批策略 自批本应由所有者批准的导出 | 已修且测试通过：真实 HTTP + PostgreSQL，4 个新增回归及 5 个 B1 回归通过 |
| S7 | P1 | 租户管理员能自审共享纠错且缺少字段白名单 | 已修且测试通过：9 个新增 HTTP/PostgreSQL 回归 + 3 个 B6 回归 |
| O-01 | P1 | 外发成功后批量事务回滚 下一轮重复发信；并发没有领取锁 | 已修且测试通过：持久业务发送意图、逐条提交、双数据库会话领取、真实事务中止和进程退出；未知结果人工核查 |
| O-02 | P1 | 引用自家退订页脚的真实询盘 被永久当成退订 | 已修且测试通过：抽取本次正文，保留原文；纯文本、多语言、HTML 引用及真实询盘落库回归 |
| O-03 | P1 | 暂时延迟 DSN 被升级为永久 平台退信 | 已修且测试通过：结构化逐收件人 DSN、原邮件关联；4.x/未知/策略失败不写永久退信 |
| O-04 | P2 | 软失败计数每轮清零 指数退避及三次切箱永远失效 | 已修且测试通过：持久次数、10/20/40分钟退避、失败邮箱排除、耗尽核查与明确恢复 |
| O-05 | P1 | 发送前最新规则要求审批 但代码仅拦 deny 直接发出 | 已修且测试通过：地区/规则/收件人证据绑定审批，审批人失效或证据变化需重审，deny 不可审批绕过 |
| O-06 | P1 | 低毛利报价经销售员普通编辑变为零成本 从而免审批 | 已修且测试通过：稳定行 ID、可信成本、版本冲突与真实销售编辑回归 |
| O-07 | P1 | 报价请求 schema 丢弃 SKU ID 底价审批闸门无法正常触发 | 已修且测试通过：本租户 SKU 解析、可信成本、折后底价与错误引用拒绝 |
| O-08 | P2 | 跨会话询盘“合并”只加计数 消息 交付与首次响应不连通 | 已修且测试通过：持久多会话关联、交付聚合、首响及真实并发；见[说明](docs/audit/inquiry-associations.md) |
| O-09 | P1 | 成交停止触达漏掉 scheduled 已成交客户仍可收到冷邮件 | 已修且测试通过：停止全部 LIVE 状态及公司关联联系人，最终发送前再次检查客户阶段 |
| O-10 | P2 | 自动回复可恢复人工暂停 同公司暂停 | 已修且测试通过：OOO 仅顺延 scheduled/active，人工、同公司、合规暂停均保留 |
| O-11 | P2 | 聊天人工答复不更新询盘首响 SLA误告警和改派 | 已修且测试通过：共用回复完成事务、SLA/跟进时间、拒绝/跨租户/回滚及既有渠道共24项；见[说明](docs/audit/widget-response.md) |
| O-12 | P2 | 入站去重和 IMAP 游标恢复不完整 | 已修且测试通过：UID世代/回执、失败重试、真实并发与租户隔离；历史无世代游标保留人工关卡；见[说明](docs/audit/imap-receipts.md) |
| O-13 | P2 | catch all 风险额度只显示提示 没有发送强制校验 | 已修且测试通过：租户UTC日域名≤1、单邮箱catch-all实际占比≤10%，预留纳入额度；不再宣称未定义角色邮箱独立额度 |
| F00 | P1 | 生产发布网站URL被SPA吞掉 | 已修且测试通过：真实本地 Caddy 3 个路由测试（4 个公开路径及静态/私有路径） |
| F01 | P1 | 报价隐藏成本回写问题已合并 | 与 O-06 合并，已修且测试通过 |
| F02 | P1 | 收件箱竞态可能把显示A的回复发送给B | 已修且测试通过：4 个真实 React 组件异步交互回归；lint / tsc / build 通过 |
| F03 | P2 | 绑定Google Microsoft邮箱点击成功却无后续 | 已修且隔离测试通过：正式回调、刷新、XOAUTH2与页面闭环；真实服务商验证受环境限制，默认关闭；见[说明](docs/audit/mailbox-oauth.md) |
| F04 | P2 | 对象深链接与助手预填没有消费端 | 已修且测试通过：会话URL、助手预填、订单通知和登录回跳交互；见[说明](docs/audit/auth-and-deep-links.md) |
| F05 | P2 | 统一批量审批假成功且混合类型直接失败 | 已修且测试通过：批量逐项结果、草稿再生成/修改确认、海关独立状态和代次；全前端50项通过；见[说明](docs/audit/approval-and-analysis-ui.md) |
| F06 | P2 | 重新生成审批草稿后输入框仍显示旧内容 | 已修且测试通过：批量逐项结果、草稿再生成/修改确认、海关独立状态和代次；全前端50项通过；见[说明](docs/audit/approval-and-analysis-ui.md) |
| F07 | P2 | 报价预览金额 毛利与服务器不一致 | 已修且测试通过：含运费/保险/佣金、十进制整数分、动态阈值；无权限不显示毛利 |
| F08 | P1 | 重复录入可双倍记账 关键写操作缺少请求锁 幂等键 | 已修且测试通过：收款14项后端/3项前端，导入与Agent持久提交37项扩展后端；见[收款](docs/audit/order-payment-intents.md)与[提交恢复](docs/audit/submission-recovery.md) |
| F09 | P2 | DSR分页契约不一致需要运行回归 | 已修且测试通过：4项页面回归、旧名单兼容及租户隔离数据库回归；见[说明](docs/audit/form-state-and-lists.md) |
| F10 | P2 | 海关分析并发覆盖导致同业结果时有时无 | 已修且测试通过：批量逐项结果、草稿再生成/修改确认、海关独立状态和代次；全前端50项通过；见[说明](docs/audit/approval-and-analysis-ui.md) |
| F11 | P2 | 旧登录令牌阻断公开分享 邀请 嵌入页 | 已修且测试通过：无/有效/过期令牌公开页、公开401隔离、私有回跳；全前端40项通过 |
| F12 | P2 | 学习建议弹窗取消后残留上一次ICP目标 | 已修且测试通过：4项页面回归、旧名单兼容及租户隔离数据库回归；见[说明](docs/audit/form-state-and-lists.md) |
| F13 | P2 | 多列表服务端截断却呈现客户端分页 | 已修且测试通过：全前端75项、后端分页/搜索回归；见[说明](docs/audit/server-pagination.md) |
| F14 | P2 | 任务页面刷新 返回后不继续观察进行中的导入与爬取 | 已修且测试通过：2项原失败断言，恢复轮询、终态停止、网络退避、卸载清理3项真实页面回归 |
| F15 | P2 | 公开提交错误处理破坏闭环 | 已修且测试通过：6项询盘、聊天和报价确认交互，错误保留输入与重试，明确失效才清会话；见[说明](docs/audit/public-form-errors.md) |
| F16 | P2 | ICP编辑后新建会继承旧数组字段 | 已修且测试通过：4项页面回归、旧名单兼容及租户隔离数据库回归；见[说明](docs/audit/form-state-and-lists.md) |
| F17 | P2 | 已知前端能力缺口 已有后端能力没有完整UI入口 | 已修且测试通过：获准的产品编辑/SKU、名单成员、样品/订单最小入口及权限/重试；见[说明](docs/audit/business-entrypoints.md) |
| F18 | P2 | 名单邮箱状态枚举与联系人状态不一致 | 已修且测试通过：4项页面回归、旧名单兼容及租户隔离数据库回归；见[说明](docs/audit/form-state-and-lists.md) |
| AI-BILL-01 | P1 | 普通补全 验证和瀑布入口绕过点数及预算 | 已修且测试通过：共用调用前检查、缓存/失败免费、Agent单次结算、逐条批量提交 |
| AI-BILL-02 | P1 | 预占不计入硬预算 多个待审批任务可以超预算 | 已修且测试通过：剩余预占计入硬预算、保存部门归属，迁移异常拒绝 |
| AI-BILL-03 | P1 | 余额及付款记账缺少并发保护 | 已修且测试通过：租户钱包锁、付款业务唯一键；真实两会话竞争 |
| AI-BILL-04 | P1 | 月结把“先消耗旧结转”实现成先消耗新赠送 错误没收点数 | 已修且测试通过：获准的跨月预占结清规则、FIFO、分月补跑、失败恢复及并发月结；见[说明](docs/audit/month-boundary.md) |
| AI-BILL-05 | P1 | 按合格商机费用仅记 pending 永远不进入月账单 | 已修且测试通过：原子记账、唯一商机费用、月账单明细和核销关联；异常迁移拒绝 |
| AI-BILL-06 | P2 | 报表订阅调度不命中且无到期报表时崩溃 | 已上游修复owner；调度/补跑/当前权限范围已修且测试通过：真实SQL失败与双线程共14项；见[说明](docs/audit/report-subscriptions.md) |
| AI-BILL-07 | P2 | WhatsApp 日发送计数跨日不重置 | 已修且测试通过：UTC消息记录额度、账号锁、午夜与双会话竞争；渠道扩展21项通过；见[说明](docs/audit/whatsapp-quota.md) |
| AI-BILL-08 | P1 | FB IG 配置token后直接虚报sent 没有发送实现 | 已修且测试通过：真实发送明确503 unavailable，不写sent；保留明确mock和人工任务 |
| AI-BILL-09 | P2 | 深度问答采纳mock回显 只检查首个引用即标grounded | 已修且测试通过：拒绝mock回显、校验全部引用及每个事实，真实数据库与B5共12项；见[说明](docs/audit/research-grounding.md) |
| AI-BILL-10 | P2 | 联系人纠错会显示accepted但不改变联系人 | 已修且测试通过：本地及共享联系人白名单字段实际写入；不支持字段明确拒绝 |
| AI-BILL-11 | P2 | Agent状态机不是真正可恢复的后台执行队列 | 已修且测试通过：持久队列、逐步提交、真实并发/崩溃/控制/权限回归；见[说明](docs/audit/agent-execution.md) |
| AI-BILL-12 | P2 | 已完成任务消费仍被余额页当成“预占中” | 已修且测试通过：只统计预占真实剩余额 |
| AI-BILL-13 | P2 | 通用Job失败路径没有rollback 数据库异常后无法落failed状态 | 已修且测试通过：先回滚、重载Job再保存failed；SQL参数不进入公开错误 |
| AI-BILL-14 | P2 | 业务信号订阅重复通知且不同信号被错误合并 | 已修且测试通过：持久回执、真实信号ID、分页/并发/事务/租户/迁移回归；见[说明](docs/audit/signal-receipts.md) |
| OPS-01 | P1 | 附件备份失败仍写入成功状态 | 已修且测试通过（脚本故障注入）；隔离数据库/附件恢复与回退见 OPS-05 |
| OPS-02 | P2 | 从未成功的备份不会触发缺失告警 | 已修且测试通过：官方 Promtool 6 组缺失、超时、恢复和多实例规则回归；见[说明](docs/audit/backup-alerts.md) |
| OPS-03 | P2 | 就绪探针未校验迁移版本 | 已修且测试通过：真实迁移元数据缺失/旧/未知版本均503；见[说明](docs/audit/readiness-and-metrics.md) |
| OPS-04 | P2 | 多进程 HTTP 指标未聚合 | 已修且测试通过：真实独立进程及双worker启动、退出、聚合；见[说明](docs/audit/readiness-and-metrics.md) |
| OPS-05 | P2 | 运维手册与恢复顺序需要演练校正 | 已修且测试通过：真实隔离PostgreSQL恢复及失败保留；Docker适配器边界见[说明](docs/audit/restore-rehearsal.md) |


## S5 本地修复验证

跨模块清单、完成条件和迁移见 [DSR 删除说明](docs/audit/dsr-erasure.md)。原3项副本/导出失败断言全部失败，迟到缓存另有失败断言；修复后7项新增 + S1/S3/S4/B1/B4 共32 passed / 51.73s，真实 PostgreSQL，含两个独立事务的导出/删除并发。此前普通副本 + P3/B1 共24 passed / 184.50s。Ruff与diff检查通过。

命令：`TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_dsr_erasure.py backend/tests/test_audit_dsr_scope.py backend/tests/test_audit_idempotency.py backend/tests/test_b1.py backend/tests/test_b4.py`。

新增0016迁移。对不可变证据、源归档、共享平台来源和精确邮箱仍命中的未分类载荷，保留分类计数并拒绝自动完成，未自行决定保留期限或修改平台/其他租户数据。0016及普通副本擦除已随修复分支发布；后续失败/孤儿ZIP清理和获准的人工关卡已完成；它们属于后续修复批次，发布版本以批次SHA记录为准。

## 入站邮件修复验证

O-02/O-03/O-10：最初12项中11项失败，修复后12项通过；扩展 MIME/引用处理后，新回归与 P3/P4 共36 passed / 235.09s。最终进一步区分永久地址失败和策略/消息失败，当前18项新增回归与5项合规/成交回归共23 passed / 14.68s。真实隔离 PostgreSQL，外发仅本地测试接收器/替身。

永久地址抑制仅接受关联原邮件、目标收件人一致、Action=failed 且明确地址类状态；临时失败、未知报告、策略拒收和消息大小错误转临时状态或人工核查。依据：[RFC 3464](https://www.rfc-editor.org/rfc/rfc3464.html)、[RFC 3463](https://www.rfc-editor.org/rfc/rfc3463.html)。此次无数据库迁移；回滚会恢复有缺陷的入站分类，不建议单独回退。

命令：`TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_inbound.py backend/tests/test_p3.py backend/tests/test_p4.py`；最终增补运行 `test_audit_inbound.py test_audit_send_gates.py`。

## 最终审批与成交停止

O-05/O-09 原3项失败断言修复通过；补充审批人停用、deny 不可越过、成交后迟到注册，共6项真实数据库/HTTP 回归通过。审批同时需要现行 sequence.approve 权限；最终发送检查客户阶段，成交事务对全部 LIVE 注册加锁终止。无迁移。P3/P4 扩展回归单独记录；SMTP 并发和事务失败仍由 O-01 处理，未因本项关闭而视为发送可靠性已解决。

扩展结果：上述前5项 + P3/P4 共25 passed / 220.92s；迟到注册最终闸门随后纳入发送可靠性回归。

## 发送可靠性与核查闭环

详见 [发送可靠性与回退说明](docs/audit/delivery-reliability.md)。新增0017迁移。序列发送和人工回复共用持久意图；对未知结果保持凭据并停止自动重试。新增投递核查页面，明确结论/证据/权限/租户边界，只有已证实未接收才允许重试。

验证：原3类发送失败断言及人工回复失败已复现；catch-all 未执行额度也有失败断言。发送/审批/DSR + 既有P3/P4扩展41 passed / 238.11s；最后补充真实 PostgreSQL 事务中止、子进程退出、并发额度及核查恢复后，定向28 passed。前端12 passed，lint无错误（12项既有Hook警告），TypeScript/生产build通过（既有大chunk提示）；Ruff/diff检查通过。

命令：`TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_delivery_intents.py backend/tests/test_audit_dsr_erasure.py backend/tests/test_audit_send_gates.py`；扩展增加 `backend/tests/test_p3.py backend/tests/test_p4.py`。前端：`npm test`、`npm run lint`、`npm run build`。

完整冻结快照 `cf68b8d944b55600abb4ed68297d30c7793ceb39` 全后端293 passed / 1 skipped / 1 warning，540.84s。OCR跳过仍因本机缺tesseract。此结果覆盖核查功能最终版本；首批247项是较早快照的独立证据。

首批已发布到修复分支：代码汇总 `0b38d4efb456abac9eeff4a2e2fd6b335a2cf07e`，映射文档提交 `68f0fe002df3a809d99f5d786f3cd6dbfe630ce0`。上传授权阻塞已解除；仍未合并或部署。第二批验证与版本边界见 [第二批记录](docs/audit/2026-10-11-batch02.md)。

## S5 增补：失败导出部分 ZIP

独立复核线索经本地失败注入确认，已增加原子发布、失败清理、真实SQL保存点恢复和DSR孤儿文件清理；受管路径边界不放宽。3项原失败断言通过，扩展验证见[DSR说明](docs/audit/dsr-erasure.md)。这属于原S5范围，不新增编号；用户已确认保留/共享来源继续人工核查，未完成核查的工单不可完成。
