# 拓域 AI（P0 项目骨架）

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
docker compose exec backend python -m app.seed   # 创建演示账号
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
../.venv/bin/python -m app.seed
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
| POST | /api/v1/auth/register | 公开（注册新组织并成为所有者） |
| POST | /api/v1/auth/login | 公开 |
| GET | /api/v1/auth/me | 登录 |
| GET/PATCH | /api/v1/tenant | tenant.read / tenant.update |
| GET | /api/v1/roles | role.read |
| GET/POST/PATCH/DELETE | /api/v1/users | user.read / user.manage |
| GET | /api/v1/audit-logs | audit.read |
| POST/GET | /api/v1/jobs | job.run / job.read |
| POST | /api/v1/llm/chat | llm.use |
