# 拓域 AI 生产部署手册（香港 / 新加坡 4 核 8G Ubuntu）

> 适用：单台云服务器（阿里云国际 / 腾讯云国际 / AWS Lightsail 等），Ubuntu 22.04 或 24.04 LTS，4 vCPU / 8 GB 内存 / ≥ 80 GB SSD。
> 架构：Docker Compose 运行 Postgres(pgvector) + Redis + 后端（2 进程）+ Celery worker + Celery beat + Caddy（自动 HTTPS，提供前端静态文件并反向代理后端）。可选监控：Prometheus + Alertmanager + node-exporter。
> 上线前先看《LAUNCH_CHECKLIST.md》，把需要你提供的东西准备好。

---

## 0. 准备清单（摘要）

| 项目 | 示例 | 说明 |
|---|---|---|
| 服务器 | 香港 4C8G Ubuntu 24.04 | 开放 22（建议限制 IP）、80、443 |
| 产品域名 | `app.example.com` | A 记录 → 服务器公网 IP |
| 发信域名 | `mail-example.com`（与主域分开） | SPF / DKIM / DMARC / MX，见第 9 节 |
| 密钥 | Qwen / DeepSeek / Hunter / MillionVerifier | 见 `deploy/.env.prod.example` |

---

## 1. 服务器初始化

```bash
# 以 root 登录后
adduser deploy && usermod -aG sudo deploy
# 本地执行：ssh-copy-id deploy@服务器IP
# 关闭密码登录（确认密钥登录成功后再做）
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && sudo systemctl restart ssh

sudo timedatectl set-timezone UTC          # 服务器统一 UTC，界面按用户时区显示
sudo apt update && sudo apt -y upgrade
sudo apt -y install ca-certificates curl git ufw fail2ban unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# 防火墙
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
sudo ufw --force enable

# 2G swap（8G 内存机器建议开）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

> 注意：Docker 发布的端口会绕过 ufw。本编排只发布 80/443；监控端口只绑定 127.0.0.1。

## 2. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy && newgrp docker
docker compose version     # 需要 v2.20+
# 日志滚动（全局）
echo '{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"5"}}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

## 3. 获取代码

```bash
sudo mkdir -p /opt/tuoyu && sudo chown deploy: /opt/tuoyu
git clone <你的私有仓库地址> /opt/tuoyu      # 或 scp 上传代码包后解压到 /opt/tuoyu
cd /opt/tuoyu
```

## 4. 配置环境变量与密钥

```bash
cp deploy/.env.prod.example deploy/.env.prod
chmod 600 deploy/.env.prod
bash deploy/scripts/gen-secrets.sh          # 打印随机密钥，复制到 .env.prod 对应行
nano deploy/.env.prod                       # 填写所有【必填】项
```

必须确认：
- `APP_DOMAIN`、`TL_PUBLIC_BASE_URL`（https）、`TL_CORS_ORIGINS` 一致；
- `TL_CREDENTIALS_KEY`、`TL_UNSUBSCRIBE_SECRET` **离线备份**（密码管理器）。丢失前者 = 所有发件箱需重新输入密码；更换后者 = 已发出邮件中的退订链接失效；
- `TL_OUTBOUND_ALLOW_REAL=false` 保持不变，直到第 11 节“开闸”。

后端以 `TL_ENV=prod` 启动时会自检：JWT / 加密密钥 / 退订密钥缺失或使用默认值、公开地址不是 https、允许抓取内网等情况会**拒绝启动**，并在日志说明原因。

## 5. DNS（产品域名）

在域名服务商添加：

| 类型 | 主机记录 | 值 |
|---|---|---|
| A | `app` | 服务器公网 IPv4 |
| AAAA（可选） | `app` | 服务器 IPv6 |
| CAA（可选） | `@` | `0 issue "letsencrypt.org"` |

`dig +short app.example.com` 返回服务器 IP 后再进行下一步（Caddy 需要通过 80 端口完成证书验证）。

## 6. 构建并启动

```bash
cd /opt/tuoyu
DC="docker compose -f docker-compose.prod.yml --env-file deploy/.env.prod"
$DC build                     # 首次约 5–10 分钟
$DC up -d                     # migrate 先执行迁移与角色同步，成功后才启动 backend
$DC ps                        # 全部 healthy / running；migrate 为 exited (0)
$DC logs -f --tail=100 backend caddy
```

证书由 Caddy 自动申请和续期（数据保存在 `caddydata` 卷，不要删除）。

## 7. 冒烟检查

```bash
bash deploy/scripts/smoke.sh https://app.example.com
# 期望：livez ok、readyz 中 db/redis 为 true 且显示迁移版本、首页 200、/metrics 404、HSTS ✓
```

## 8. 开通第一个租户、充值点数

```bash
$DC exec backend python -m app.admin create-tenant --tenant "宁波某某阀门" --email boss@客户域名 --name 王总 --password '至少8位强密码'
$DC exec backend python -m app.admin list-tenants                     # 查看 slug
$DC exec backend python -m app.admin grant-credits --tenant <slug> --amount 3000 --note "试点首月"
# 手工发票（客户对公转账后）：
$DC exec backend python -m app.admin create-invoice --tenant <slug> --amount 2999.00 --currency CNY --credits 3000 --period 2026-11 --desc "SOHO 月费"
$DC exec backend python -m app.admin mark-invoice --tenant <slug> --no INV-202611-0001 --status paid --method 银行转账 --ref 银行流水号
```

也可用 HTTP 接口（需 `TL_PLATFORM_ADMIN_TOKEN`）：
```bash
curl -X POST https://app.example.com/api/v1/platform/tenants/<slug>/credits \
  -H "X-Platform-Admin-Token: $TOKEN" -H "X-Operator: 你的名字" -H "Content-Type: application/json" \
  -d '{"amount":1000,"note":"加购"}'
```
新租户默认 **0 点**（`TL_PILOT_GRANT_CREDITS=0`），没有充值时 Agent 任务会提示“点数余额不足”。租户的所有者 / 管理员可在“计费与点数”页面查看余额、账本（可导出 CSV）和发票。

## 9. 发信域名 DNS（客户的发件域名，每个租户各自配置）

以发件域名 `mail-example.com`、邮箱服务商为腾讯企业邮 / Google Workspace / Microsoft 365 为例：

| 类型 | 主机记录 | 值（示例，按邮箱服务商文档为准） |
|---|---|---|
| MX | `@` | 服务商给的 MX（如 `mxbiz1.qq.com` 优先级 5） |
| TXT（SPF） | `@` | `v=spf1 include:spf.mail.qq.com ~all`（只保留实际用到的 include，总查询 ≤ 10 次） |
| TXT（DKIM） | `<selector>._domainkey` | 服务商后台生成的公钥 |
| TXT（DMARC） | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@mail-example.com; adkim=s; aspf=s`（稳定 4 周后改 `p=quarantine`） |

在产品“发件箱”页绑定 SMTP/IMAP 后点“DNS 体检”，SPF / DKIM / DMARC / MX 全部通过才允许发信。新邮箱自动进入预热（每日上限逐步爬升）。**建议冷开发使用与主品牌域名分开的域名**。

## 10. 备份与恢复

```bash
# 每日 03:15（UTC）备份，保留 14 天；可选 rclone 同步到对象存储（阿里云 OSS / S3）
sudo mkdir -p /var/lib/node_exporter/textfile && sudo chown deploy: /var/lib/node_exporter/textfile
crontab -e
15 3 * * * cd /opt/tuoyu && BACKUP_REMOTE=oss:tuoyu-backup deploy/scripts/backup.sh >> /var/log/tuoyu-backup.log 2>&1
```

- 备份内容：数据库（`pg_dump -Fc`，自动校验可读）、上传文件目录，附 SHA256 校验文件。
- 恢复（会覆盖数据库，需输入 RESTORE 确认）：
  ```bash
  deploy/scripts/restore.sh backups/db-20261110T031500Z.dump backups/uploads-20261110T031500Z.tar.gz
  ```
- **每月做一次恢复演练**：在另一台临时服务器上恢复最新备份并登录验证。
- `deploy/.env.prod` 不在备份里，请单独离线保存。

## 11. 开闸：允许发送真实邮件（需你明确确认）

上线初期 `TL_OUTBOUND_ALLOW_REAL=false`，系统只会连接本地测试邮件服务器。确认以下事项后再改为 `true` 并 `$DC up -d backend worker`：
1. 发件域名 DNS 体检全部通过；
2. 合规设置（公司法定名称、地址、隐私邮箱）已填写，退订链接 `https://APP_DOMAIN/u/...` 可打开；
3. 序列已审批，L2 自动通过保持关闭；
4. 先用自己的邮箱做 1–2 封测试。

## 12. 监控与日志

```bash
$DC --profile monitoring up -d prometheus alertmanager node-exporter
ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 deploy@服务器IP   # 本地浏览器打开 http://localhost:9090
```

- 指标：`/metrics`（Caddy 对公网屏蔽，Prometheus 在内网抓取）。包含 HTTP 请求数 / 耗时、数据库可用、发件箱健康级别、24 小时发信量、未恢复熔断、超时询盘、Agent 任务、未结数据主体请求、待处理交付包。
- 告警规则：`deploy/monitoring/alerts.yml`（后端不可用、数据库不可用、5xx > 2%、P95 > 2 秒、熔断、A/B 询盘超时、数据主体请求积压、磁盘 / 内存、备份缺失）。接收方式在 `alertmanager.yml` 配置（企业微信 / 钉钉机器人 webhook 等）。
- 日志：`TL_LOG_JSON=true` 时后端与 worker 输出 JSON 行（含 trace_id、tenant_id、路径、状态码、耗时）；`$DC logs backend | jq` 查看。前端响应头 `X-Trace-Id` 可用于定位单次请求。
- 健康检查：`/livez`（进程存活，容器 HEALTHCHECK 用）、`/readyz`（数据库 + Redis + 迁移版本，异常返回 503）、`/healthz`（兼容旧版）。

## 13. 升级与回滚

```bash
cd /opt/tuoyu
deploy/scripts/backup.sh                     # 升级前先备份
git pull                                     # 或上传新代码包
sed -i 's/^APP_VERSION=.*/APP_VERSION=2026.11.0/' deploy/.env.prod
$DC build && $DC up -d                        # migrate 自动执行新迁移
bash deploy/scripts/smoke.sh https://app.example.com
```
回滚：把 `APP_VERSION` 改回旧值并 `$DC up -d`（旧镜像仍在本地）；若新版本迁移不兼容旧代码，用升级前的备份 `restore.sh` 恢复。

## 14. 可调参数说明（试点默认值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `TL_PILOT_GRANT_CREDITS` | 0 | 新租户自动发放点数；0 = 只能平台管理员手动充值 |
| `TL_AGENT_OVERRUN_BASIS` / `_RATIO` | high / 1.0 | 实际消耗超过“预估上限 × 1.0”才自动暂停；改为 mid / 1.2 即 PRD 原文 |
| `TL_INQ_W_AUTHENTICITY` / `_CLARITY` / `_FIT` | 0.40 / 0.35 / 0.25 | 询盘总分权重（自动归一化，界面显示实际公式） |
| `TL_INQ_WEBSITE_CHECK` | true | 发件人企业邮箱域名官网核验 |
| `TL_INQ_WEBSITE_BONUS` | 25 | 公司不在数据中枢时，若域名能打开真实官网且官网标题 / 正文出现域名主体词或来信署名中的公司名，真实性 +25 |
| `TL_INQ_D_AUTH_THRESHOLD` | 40 | 真实性低于该分一律 D 级（待人工复核） |

> 官网核验只读取首页，遵守禁止内网地址规则，6 秒超时；无法访问或不吻合时不加分，评分明细里写明原因。

## 15. 常见问题

- **证书申请失败**：确认 A 记录已生效、80/443 已放行、云厂商安全组也已放行；`$DC logs caddy`。
- **backend 不启动**：`$DC logs backend` 查看“生产配置不完整：……”提示并补齐 `.env.prod`。
- **香港 / 新加坡访问大模型**：通义千问国际站使用 `TL_QWEN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1`（需国际站 Key）。
- **内存不足**：worker `--concurrency` 改为 2，Postgres `shared_buffers` 改为 768MB。

## 15. B3 新增配置（汇率 / OCR / 黑名单 / 追踪）

- `TL_FX_PROVIDER=ecb`：使用欧洲央行每日参考汇率（公开数据，只做匿名 GET，不发送任何用户数据）；默认 `mock`。每日任务 `crm.daily` 刷新。
- OCR：后端镜像已安装 `tesseract-ocr`（chi_sim + eng），图片文档导入和名片背调直接可用，无需额外配置。
- **黑名单（DNSBL）真实查询**：Spamhaus / SURBL 会拒绝来自公共 DNS（8.8.8.8、1.1.1.1、云厂商默认转发 DNS）的查询，返回 127.255.255.x 拒绝码，系统会把它当作“未列入”，因此必须使用自己的递归解析器：
  1. 启动时加上 profile：`docker compose -f docker-compose.prod.yml --profile dnsbl up -d`（会启动 `unbound` 容器，直接向根服务器递归查询，不经过公共 DNS）。
  2. `.env.prod` 设置 `TL_DNSBL_PROVIDER=dns`、`TL_DNSBL_RESOLVER=unbound`，然后 `docker compose ... up -d backend worker beat`。
  3. 验证：`docker compose exec backend python -c "from app.b3.outreach import _listed; print(_listed('2.0.0.127.zen.spamhaus.org'))"` 应输出 `True`（官方测试地址）。
  4. 每日 `b3.daily` 检查所有发信域名；被列入会自动暂停该域名下的发件箱和域名，并站内通知管理员。Spamhaus 免费查询有日量限制（商业用途量大时需购买 DQS 密钥）。
- `TL_TRACKING_HOST`：追踪域名 CNAME 目标（启用点击追踪时用）；`TL_CREDIT_PRICE_CNY`：单位经济里 1 点折合人民币。

## 16. 打开 / 点击追踪（默认关闭）

- 系统默认**不插入**打开像素，也**不改写**链接；序列里的“追踪策略”目前只是配置项，不会生效。
- 隐私与送达取舍：
  - 打开追踪依赖远程图片，Apple Mail 隐私保护、Gmail 图片代理会让数据失真；欧盟 / 英国（GDPR、ePrivacy）下属于设备信息读取，需事先同意，系统对这些收件人强制关闭。
  - 点击追踪会把链接改写到追踪域名（`TL_TRACKING_HOST`），共享或新域名常被反垃圾系统扣分，影响进箱率。
  - 冷邮件阶段以“回复率”为主指标，不依赖打开率。
- 如将来确需启用，要先配好追踪域名 CNAME（“触达工具 → 域名规划与黑名单”可检查），并在隐私政策里写明。

## 17. B4：套餐、询盘组件、客户成功与数据主权

- **套餐**：首次设置订阅时自动写入入门 / 成长 / 专业三档默认套餐（可用 `POST /api/v1/platform/plans:seed`）。开通：
  `curl -XPUT -H "X-Platform-Admin-Token: $T" -H 'Content-Type: application/json' https://$APP_DOMAIN/api/v1/platform/tenants/<slug>/subscription -d '{"plan_code":"starter","seats":3}'`。
  未设置订阅的租户按试点处理（席位不限、点数手动充值）。每月 1 日 `b4.daily` 自动月结（结转 + 月度账单）。
- **在线支付**：`TL_PAYMENT_PROVIDER=mock`，只在本系统标记已付款、不扣款。接入支付宝 / 微信 / Stripe 需商户资质，届时再加适配器。
- **询盘组件**：后台“询盘组件”新建表单或聊天，复制嵌入代码到官网 `</body>` 前。访客页 `/w/<key>` 已在 Caddy / Nginx 模板中放开 iframe 嵌入（`frame-ancestors *`），来源白名单在后端按组件校验。表单与聊天不会发送任何邮件或外部消息。
- **服务状态页**：`https://$APP_DOMAIN/status`，事件通过平台接口发布（见 RUNBOOK.md）。
- **法务模板**：`/api/v1/public/legal/<slug>/{privacy|dpa|subprocessors|cookies}`，上线前请律师审阅 `backend/legal/*.md`。
- **解约**：`TL_CLOSURE_GRACE_DAYS=30`；`b4.hourly` 清除到期租户并生成删除证明。
- **运维文档**：RUNBOOK.md（降级矩阵、告警处理、安全事件）、LIMITS.md（限额总表）；负载测试：`python deploy/scripts/loadtest.py https://$APP_DOMAIN <token> 20 500`（避开业务高峰）。
