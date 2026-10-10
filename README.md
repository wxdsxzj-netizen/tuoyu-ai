# 拓域 AI（P0 骨架 + P1 核心数据）

外贸 AI 获客平台的 P0 阶段：多租户、账号与角色、权限、审计日志、统一错误码、异步作业框架、LLM 网关，以及前端管理后台外壳。

## 技术栈
- 后端：Python 3.12+ · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL 17 + pgvector · Redis · Celery
- 前端：Vite · React 18 · TypeScript · Ant Design 5 · i18next（简体中文默认，预留英文）
- 部署：Docker Compose（postgres / redis / backend / worker / frontend）

## 目录
```
backend/
  app/core/      配置、数据库与租户隔离、JWT、RBAC、审计、错误码
  app/api/       REST 接口（/api/v1）
  app/llm/       统一 LLM 网关（千问主用 / DeepSeek 兜底 / mock）
  app/tasks/     Celery 应用与示例作业
  alembic/       数据库迁移
  tests/         pytest（含租户隔离、RBAC 测试）
frontend/        管理后台（登录、成员管理、组织设置、审计日志、作业与 AI）
```

## 方式一：Docker Compose 启动
```bash
cp .env.example .env        # 修改 TL_JWT_SECRET
docker compose up --build
docker compose exec backend python -m app.admin create-tenant --tenant 演示外贸公司 --email owner@demo.com --name 所有者 --password Demo@123456
```
打开 http://localhost:5173 ，用 `owner@demo.com / Demo@123456` 登录。API 文档：http://localhost:8000/docs

## 方式二：本机直接运行
需要 PostgreSQL 17（含 pgvector 扩展）、Redis、Python 3.12+、Node 20。
```bash
# 数据库
sudo -u postgres psql -c "create user tl with password 'tl' superuser;" \
  -c "create database tradeleads owner tl;" -c "create database tradeleads_test owner tl;"

# 后端
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python -m app.admin create-tenant --tenant 演示外贸公司 --email owner@demo.com --name 所有者 --password Demo@123456
../.venv/bin/uvicorn app.main:app --port 8000 --env-file ../.env
# 另开终端启动 worker（需先导出 .env 里的变量）
set -a; . ../.env; set +a; ../.venv/bin/celery -A app.tasks.celery_app worker -l info

# 前端
cd frontend && npm install && npm run dev
```

## 测试
```bash
cd backend && ../.venv/bin/ruff check . && ../.venv/bin/pytest -q   # 使用 tradeleads_test 库
cd frontend && npm run lint && npm run build
```
CI 配置见 `.github/workflows/ci.yml`。

## 关键设计
- **租户隔离**：所有带 `tenant_id` 的表在 ORM 层自动追加租户过滤；没有租户上下文时查询直接报错；禁止写入/修改其他租户数据。访问其他租户对象统一返回 404（不泄露是否存在）。跨租户的系统任务必须显式 `bypass_tenant()`。
- **角色与权限**：每个租户预置 所有者 / 管理员 / 业务员 三个系统角色，权限点见 `app/core/rbac.py`。接口用 `require("权限点")` 声明。后续阶段按 PRD 03 §6 扩展经理、营销、财务、审计员等角色。
- **审计日志**：登录（成功/失败）、注册组织、成员增改删、角色变更、组织设置修改、作业结果、AI 调用都会记录；密码等敏感字段只记“已修改”；数据库触发器保证只追加、不可修改删除。
- **错误码**：响应格式 `{"error": {code, message, retryable, trace_id, details}}`，错误码登记在 `app/core/errors.py`，测试会检查未登记的错误码。每个响应带 `X-Trace-Id`。
- **乐观锁**：业务表带 `version`，修改成员需带版本号，冲突返回 `COMMON_VERSION_CONFLICT`。
- **异步作业**：`jobs` 表记录状态与进度，Celery 执行，任务内显式进入租户上下文。示例：`POST /api/v1/jobs/example`。
- **LLM 网关**：`TL_LLM_PRIMARY=qwen`、`TL_LLM_FALLBACK=deepseek` 并填写 Key 即切换到真实模型；主模型失败自动切兜底；未配置时用 mock。

## 主要接口
| 方法 | 路径 | 权限 |
|---|---|---|
| POST | /api/v1/auth/login | 公开 |
| GET | /api/v1/auth/me | 登录 |
| GET/PATCH | /api/v1/tenant | tenant.read / tenant.update |
| GET | /api/v1/roles | role.read |
| GET/POST/PATCH/DELETE | /api/v1/users | user.read / user.manage |
| GET | /api/v1/audit-logs | audit.read |
| POST/GET | /api/v1/jobs | job.run / job.read |
| POST | /api/v1/llm/chat | llm.use |

## 开通租户（试点期不开放自助注册）
- 命令行：`python -m app.admin create-tenant --tenant 公司名 --email 老板邮箱 --name 姓名 --password 初始密码`；`list-tenants` 查看；`sync-roles` 在权限点变更后同步所有租户的系统角色。
- HTTP：设置 `TL_PLATFORM_ADMIN_TOKEN` 后，`POST /api/v1/platform/tenants`（请求头 `X-Platform-Admin-Token`），`GET /api/v1/platform/tenants`。未设置时接口禁用。

## P1 核心数据
| 功能 | 说明 | 接口 |
|---|---|---|
| 官网解析 → 知识库 | 1–5 个网址，必须勾选“有权使用该网站内容”；遵守 robots.txt，只在起始域名内抓取，sitemap 与产品页优先，拒绝内网地址；抽取公司简介、产品、认证、案例、联系方式，全部“待确认”并挂来源页；LLM 不可用时降级为规则抽取 | `POST /kb/crawls`、`GET /kb/items`、`POST /kb/items:batch` |
| ICP | 导入 ≥ 10 个成交客户后按行业聚类生成 2–5 个 ICP 草稿（LLM 写描述）；也可手工新建；启用需行业、国家、角色三项 | `POST /icps:generate`、`/icps`、`/icps/{id}:activate` |
| 公司 / 联系人 | 租户私有档案；判重：域名一致自动合并，名称相似（Jaro-Winkler ≥ 0.92）+ 同国 + 电话或地址佐证为疑似重复；联系人按规范化邮箱合并；导入的邮箱一律“未验证” | `/companies`、`/companies/facets`、`/contacts` |
| CSV / Excel 导入 | 公司、联系人、成交客户；自动识别中英文表头，可手工映射；支持仅预检；行级校验（必填、邮箱、国家、网址、金额、日期）；疑似重复默认不导入；可下载错误报告；GBK/UTF-8 自动识别；Celery 异步 | `POST /imports:preview`、`POST /imports`、`GET /imports/{id}/errors.csv` |
| 联系人数据源 | 可插拔接口（Apollo / Hunter / Snov），当前只有 mock；配置真实数据源但无 Key 时返回 `DATA_PROVIDER_UNAVAILABLE`，不会回退到抓取 | `POST /companies/{id}:enrich-contacts` |

角色权限：所有者 / 管理员可管理知识库与 ICP；业务员可查看知识库和 ICP，可新建公司 / 联系人、导入、调用数据源。

## P2：联系人验证与合规（M05）

- **Hunter.io 适配器**（`app/data/hunter.py`）：Domain Search / Email Finder / Email Verifier；`HUNTER_API_KEY` 环境变量；进程内限流（15/s·500/min，验证 10/s·300/min）；403 退避重试、429 额度用完、401 Key 无效、451 本人要求停止处理（不入库）。未配置 Key 时自动使用 mock。
- **邮箱验证流水线**（`app/compliance/verify.py`）：语法 → 一次性邮箱 / 角色邮箱 → 平台退信库 → MX → 外部服务（MillionVerifier 默认 / Hunter / mock）；五类结果 + 原因码；24 小时复用不计费；服务不可用记为“未知”。
- **抑制名单**：租户级（邮箱 / 域名 / 公司 / 号码，渠道范围）+ 平台级（90 天内 ≥2 租户投诉、收件人“退订所有发件人”），平台级不暴露来源租户；解除规则按 BR-5-09。
- **退订**：`/u/{token}` HMAC 签名令牌（不含明文邮箱），支持 RFC 8058 一键退订（`List-Unsubscribe-Post`）与三种范围。
- **制裁筛查**：每日下载 trade.gov CSL + OFAC SDN（含别名），倒排召回 + 双向词集合相似度，≥0.85 进入复核队列；名单 > 72 小时未更新标记过期。平台手动刷新：`POST /api/v1/platform/sanctions:refresh?sync=true`。
- **地区规则**：GDPR / UK PECR / CAN-SPAM / CASL / PIPL / AU 默认规则（版本化，平台双人复核发布、可回滚）；LIA、同意记录、发件人身份与实体地址页脚。
- **发送前闸门**：`GET /api/v1/contacts/{id}/compliance-check` 汇总以上全部，供后续序列 / 发信调用。

## P3：DSR 工作台 + 发件箱 + AI SDR 序列

- **数据主体请求（M5-07/12）**：访问 / 删除 / 更正 / 反对；身份核验码（处理人自行发给请求人）；30 天时限（补件暂停计时）；第 20 天提醒、第 25 天升级、第 28 天自动“限制处理”；补件 30 天未回自动关闭；反对与“停止联系”立即写入全平台抑制；删除后只保留哈希抑制，重新导入仍被拦截；公开表单 `POST /privacy/{租户slug}/requests`。
- **发件箱（M6-01/02/04/06/07）**：SMTP/IMAP 绑定（Fernet 加密存储，接口不回传）；OAuth 预留（返回 DELIVERY_PROVIDER_UNAVAILABLE）；DNS 体检 SPF（include 递归 ≤10 次查询）/ DKIM / DMARC / MX + 待配置记录；21 天预热爬坡；健康分 8.6 公式与分级熔断（关注/降速/暂停冷信/暂停域名），冷却 48h（恢复期再犯 96h）+ 管理员确认 + 7 天 50% 额度。
- **序列（M7-01/03/12/14）**：默认 4 步（0/3/7/14 天）；收件人当地工作时间 + 公共假日；同企业每天 ≤3 封；每箱每小时 ≤15 封、间隔 60–300 秒随机；AI 写作（LLM 网关，模型不可用时规则写作，只引用知识库）+ G2 质检（事实溯源/不可承诺一票否决、垃圾词、长度、CTA、页脚）；审批队列（L0/L1 人工，L2 质检≥85 自动）；每封发送前再跑 P2 合规闸门；回复识别（退订关键词兜底、休假顺延、退信、不感兴趣冷却 180 天、同公司智能停止）；内容快照。
- **安全护栏**：`TL_OUTBOUND_ALLOW_REAL=false`（默认）时 SMTP/IMAP 只能连本地测试服务器（Mailpit / aiosmtpd）。

## P4：收件箱与交付（M08 / M10 精简 / 交付包 / 通知中心）

- **统一收件箱**：IMAP 拉取 + 手工登记来信 + 序列发信 + 人工回复统一进入会话；按 In-Reply-To/References → “发件人 + 规范化主题 + 14 天”归并（BR-8-06）；自动建客户（企业域名，免费邮箱不按域名判重）。
- **翻译**：经 LLM 网关（当前 mock，译文带“【译文·模拟】”标记）；入站自动译中文，中文草稿发送前生成双语对照；不可用时显示原文。
- **AI 分类 + 建议回复**：沿用 P3 规则 + LLM 分类；敏感话题（价格、付款、交期、质保、认证、合同）标“需人工”；建议回复只填入编辑框，**人工发送**。
- **人工回复发送前检查**：抑制 / 制裁 / 无效邮箱 / 限制处理拦截；**命中“禁止承诺”直接拦截**，其余质检问题仅提示；仍只允许本地测试 SMTP。
- **交付包（核心指标）**：积极类回复（含非序列的陌生询盘）生成交付包：对话全文 + 译文、背调摘要、需求要点（品类/数量/用途/时间）、建议回复；接受 = 建客户 + 商机（≥2 项需求明确为合格商机）；拒绝必须选原因；2 小时未领取改派（优先业务员），2 次后进共享池，7 天失效；`/handoffs/stats` 输出接受率与合格商机数。
- **精简 CRM**：客户（私海/公海、领取冷却 15 天、每日 20 个、上限 500）、商机看板（7 阶段、输单必选原因、90 天内可重开、赢单转成交客户并移出活跃序列、停留超时预警）、报价（金额整数分存储、毛利/折扣审批：<15% 或折扣 >10% 经理，<8% 所有者；审批后锁定；新版本；作废需原因）、客户在线确认（一次性令牌、只显示客户可见字段）、形式发票 PI（需所有者设置银行信息）、报价/PI PDF（reportlab，内置中文字体）。
- **通知中心（站内）**：N-01 新交付包、N-05 熔断、N-10 商机卡点、N-15 报价审批结果、N-17 交付包改派、N-18 待审批、N-19 银行信息修改、N-20 预热完成/恢复、N-23 DSR 到达/即将超期/升级/限制处理、N-30 交付包失效；同类同对象 10 分钟合并。
- **租户设置**：`l2_auto_approve`（L2 自动通过，默认关闭）：`PUT /api/v1/outreach/settings`。
- 新定时任务：`handoff.sla`（每 5 分钟）、`crm.daily`（每日）。
