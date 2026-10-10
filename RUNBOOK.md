# 运维 Runbook（G-07）

> 适用于单机 docker compose 部署（DEPLOY.md）。所有命令在 `/opt/tuoyu` 下执行，`dc` = `docker compose -f docker-compose.prod.yml --env-file .env.prod`。

## 0. 分级与响应
| 级别 | 定义 | 首响 | 对外 |
|---|---|---|---|
| S1 | 全站不可用 / 数据泄露疑似 | 15 分钟 | 状态页 30 分钟内发布；安全事件 72 小时内通知受影响租户（平台接口 `POST /platform/incidents/{id}:notify`） |
| S2 | 核心功能（发信、收件、登录）故障 | 1 小时 | 状态页发布 |
| S3 | 单功能降级 | 4 小时 | 视情况 |
| S4 | 轻微 / 单租户 | 1 工作日 | 工单回复 |

发布事件：`curl -XPOST -H "X-Platform-Admin-Token: $T" -H 'Content-Type: application/json' https://$APP_DOMAIN/api/v1/platform/incidents -d '{"title":"发信延迟","severity":"S2","components":["sending"],"note":"排查中"}'`；更新：`/incidents/{id}/updates`（status=identified/monitoring/resolved）。

## 1. 降级矩阵
| 依赖故障 | 自动行为 | 人工动作 |
|---|---|---|
| 大模型（Qwen/DeepSeek） | 网关切备用模型；全部失败时 AI 写信回落模板、分类回落规则、翻译显示原文、AI 接待转人工 | 检查密钥额度；必要时 `TL_LLM_PROVIDER=mock` 临时停用 |
| Hunter / MillionVerifier | 补全返回“暂不可用”不扣点；验证只做本地步骤（语法/MX/一次性/角色），状态 `unknown` 不入序列 | 检查额度 |
| Redis | 定时任务、后台作业暂停；接口可用 | `dc restart redis worker beat` |
| PostgreSQL | `/readyz` 503，Caddy 健康检查剔除 | 见 §3 |
| SMTP 发件箱 | 认证失败 30 分钟后重试；软退信指数退避，3 次后换发件箱；退信/投诉超阈值自动熔断 | 通知租户重新授权 |
| DNSBL 解析器 | 查询失败按“未列入”处理并记录 | `dc --profile dnsbl restart unbound` |
| ECB 汇率 | 保留上一次汇率并提示过期 | 手工录入 |
| 制裁名单刷新 | 保留旧名单，告警 | 次日自动重试 |

## 2. 常见告警处理
- **BackendDown / ReadyzFailing**：`dc ps`；`dc logs --tail=200 backend`；迁移未执行时 `dc run --rm migrate`。
- **HighErrorRate**：按 `trace_id` 在 JSON 日志中检索：`dc logs backend | grep <trace_id>`。
- **MailboxCircuitOpen**：查看“发件箱”页熔断原因；确认 DNS / 黑名单（触达工具 → 黑名单检查）；冷却期后自动恢复。
- **BackupStale**：`dc exec backend ls -l /backups`；手动 `deploy/scripts/backup.sh`。
- **WorkerQueueBacklog**：`dc logs worker`；`dc up -d --scale worker=2`（4C8G 上限 2）。

## 3. 数据库
- 恢复：`deploy/scripts/restore.sh <备份文件>`（需输入 RESTORE）。每月演练一次。
- 连接耗尽：`dc exec postgres psql -U tuoyu -c "select count(*) from pg_stat_activity"`；重启 backend。

## 4. 安全事件
1. 定级、开事件（kind=security，自动计算 72 小时通知截止时间）。
2. 遏制：吊销会话（安全页 → 会话）、轮换密钥（`gen-secrets.sh`，注意 `TL_CREDENTIALS_KEY` 轮换需保留旧密钥于 MultiFernet 列表）、封禁 IP。
3. 取证：导出审计日志（安全页 → 审计导出），保留原始日志。
4. 通知：`:notify` 接口给受影响租户发站内通知；需要监管报告时由租户（控制者）提交，平台提供材料。
5. 复盘：5 个工作日内完成，更新本 Runbook。

## 5. 解约清除
租户所有者申请后 30 天冷静期；每小时任务 `b4.hourly` 清除到期租户并生成删除证明（平台接口 `GET /platform/deletion-certificates/{no}`）。备份 14 天滚动过期，删除证明中已注明。
