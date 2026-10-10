# 上线清单：需要你提供 / 确认的事项

> 打勾完成后按《DEPLOY.md》执行。标 ★ 的是上线必需，其余可以后补（缺少时对应功能用模拟数据或关闭）。

## A. 基础设施
- [ ] ★ **云服务器**：香港或新加坡，4 vCPU / 8 GB / ≥ 80 GB SSD，Ubuntu 22.04/24.04，公网 IPv4；把 IP 和 SSH 登录方式（密钥）给到部署人
- [ ] ★ 云厂商安全组放行 TCP 22（建议仅限你的 IP）、80、443，UDP 443
- [ ] 对象存储桶（阿里云 OSS / AWS S3 / Cloudflare R2）用于异地备份 + rclone 访问密钥
- [ ] 代码仓库访问方式（私有 Git 仓库地址 + 部署密钥），或直接上传代码包

## B. 域名与 DNS
- [ ] ★ **产品域名**，例如 `app.你的品牌.com`；DNS 管理后台权限
- [ ] ★ A 记录：`app` → 服务器 IP（可选 AAAA、CAA `0 issue "letsencrypt.org"`）
- [ ] ★ 证书通知邮箱（`ACME_EMAIL`）
- [ ] ★ **每个试点租户的发信域名**（建议与主品牌域名分开，例如 `品牌-mail.com`），及其 DNS 记录：
  - [ ] MX（邮箱服务商提供）
  - [ ] SPF：`v=spf1 include:<服务商> ~all`
  - [ ] DKIM：`<selector>._domainkey` TXT（邮箱服务商后台生成）
  - [ ] DMARC：`_dmarc` TXT `v=DMARC1; p=none; rua=mailto:...`
- [ ] ★ 每个发件邮箱的 SMTP / IMAP 主机、端口、账号、**授权码 / 应用专用密码**（不要用主密码）

## C. 第三方密钥
| 变量 | 用途 | 必需 | 获取地址 |
|---|---|---|---|
| `TL_QWEN_API_KEY` | 通义千问：翻译、分类、建议回复、Agent 解析、背调文字 | ★（与 DeepSeek 至少一个） | dashscope.console.aliyun.com（海外服务器建议国际站 Key） |
| `TL_DEEPSEEK_API_KEY` | DeepSeek：主模型或备用 | ★（至少一个） | platform.deepseek.com |
| `HUNTER_API_KEY` | 联系人查找（域名搜索 / 邮箱查找 / 验证） | 建议 | hunter.io → API |
| `MILLIONVERIFIER_API_KEY` | 批量邮箱验证 | 建议 | millionverifier.com → API |

> 缺 Hunter：联系人补全用模拟数据；缺 MillionVerifier：验证改用 Hunter，再缺则用模拟结果（真实发信前必须至少有一个验证服务）。

## D. 系统密钥（用 `deploy/scripts/gen-secrets.sh` 生成，存进密码管理器并离线备份）
- [ ] ★ `TL_JWT_SECRET`（登录令牌签名）
- [ ] ★ `TL_CREDENTIALS_KEY`（Fernet，加密发件箱密码；**丢失后所有发件箱需重新输入密码**）
- [ ] ★ `TL_UNSUBSCRIBE_SECRET`（退订链接签名；**上线后不可更换**，否则已发邮件里的退订链接失效）
- [ ] ★ `POSTGRES_PASSWORD`、`REDIS_PASSWORD`
- [ ] `TL_PLATFORM_ADMIN_TOKEN`（平台管理员 HTTP 接口：开通租户、充值、登记发票；不填只能用命令行）
- [ ] `TL_METRICS_TOKEN`（可选）

## E. 合规与业务资料（每个试点租户）
- [ ] ★ 公司法定名称、实际通讯地址（邮件页脚 CAN-SPAM / CASL 必需）、隐私联系邮箱
- [ ] ★ 隐私政策页面 URL（数据主体请求入口 `https://APP_DOMAIN/privacy/<slug>/requests`）
- [ ] ★ 公司官网（知识库解析）、主营产品、ICP 设定
- [ ] 收款银行信息（PI 用，所有者在 CRM 页填写）
- [ ] 报价审批阈值是否沿用默认（毛利 15% / 8%、折扣 10%）
- [ ] 每个租户的初始点数和价格（平台管理员手动充值；手工发票记录）
- [ ] 团队时区（询盘响应时限按工作时间计算，默认 Asia/Shanghai）

## F. 运维
- [ ] 告警接收方式：企业微信 / 钉钉 / 飞书机器人 webhook（写入 `deploy/monitoring/alertmanager.yml`）
- [ ] 备份保留天数（默认 14 天）和异地备份位置
- [ ] 平台管理员名单（谁可以开租户、充值）

## G. 上线当天需要你**明确确认**的开关
- [ ] `TL_OUTBOUND_ALLOW_REAL=true`：允许发送真实邮件（默认 false，只连接本地测试服务器）
- [ ] `TL_LLM_PRIMARY` 从 mock 改为 qwen / deepseek
- [ ] 第一个试点租户的第一条序列由你审批后再启动
- [ ] 告警是否发送邮件（会产生真实对外邮件，默认不配置）

## B4 新增
- [ ] 确认套餐价格与赠送点数（默认：入门 ¥999/月 2 席 300 点；成长 ¥2999/月 5 席 1200 点；专业 ¥6999/月 15 席 4000 点）
- [ ] 在线支付：如需开通，提供支付宝 / 微信支付 / Stripe 商户资质（未提供前保持 `TL_PAYMENT_PROVIDER=mock`，线下收款 + 手工标记）
- [ ] 律师审阅 `backend/legal/` 下隐私政策、DPA、子处理者清单、Cookie 政策
- [ ] 如启用 DNSBL 真实查询：`--profile dnsbl` + `TL_DNSBL_PROVIDER=dns` + `TL_DNSBL_RESOLVER=unbound`
