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
