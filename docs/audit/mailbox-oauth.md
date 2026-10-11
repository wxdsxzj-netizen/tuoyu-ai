# 邮箱 OAuth（F03）

两个入口共用授权状态、取消和重试弹窗；只允许HTTPS的Google/Microsoft授权主机。正式回调页面消费授权结果，清除地址栏授权码，区分成功、取消和未知结果；默认no-referrer，StrictMode不重复提交。未配置时明确显示不可用，client ID单独不能启用真实连接。

正式后端新增持久授权尝试：state绑定租户、发起人和服务商，10分钟有效；PKCE S256、nonce、ID token签名/受众/发行者/有效期及Google已验证邮箱均校验。额外受众必须属于本客户端（本应用未配置其他受信客户端），出现azp时必须匹配client ID；依据[OpenID Connect受众与授权方校验](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)。授权码交换前提交状态，已成功的回调可重查同一邮箱；未知或失败交换不能盲重放，须重新授权。当前权限在交换后再次检查，同租户其他成员的既有邮箱不能被覆盖。域名邮箱数量限制及原有发送/域名/合规闸门保持。

Gmail SMTP/IMAP使用服务商要求的mail.google.com范围；Microsoft使用委托SMTP/IMAP范围和offline_access。参见[Google OAuth服务端流程](https://developers.google.com/identity/protocols/oauth2/web-server)、[Gmail XOAUTH2协议](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol)及[Microsoft邮箱OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)。

访问/刷新令牌加密保存在独立凭据表，接口、日志和审计不返回令牌。刷新按凭据行串行化，独立事务先持久化轮换令牌，避免调用方SMTP/IMAP事务回滚丢失新令牌；SQL异常先回滚再记录失效，失败/撤权需重新授权。传输使用固定服务商主机、TLS与XOAUTH2，SMTP在STARTTLS后重新EHLO；配置被改到其他主机时拒绝发送令牌。解绑清空本系统的令牌，OAuth邮箱不能通过密码修改伪装恢复。供应商侧的应用同意需账号持有人在供应商控制台管理。

迁移0025新增授权尝试与凭据表；存在凭据或授权证据时拒绝破坏性downgrade。应用回退前停用OAuth入口并停止相关任务，保留新表、加密密钥及授权证据；不能用旧密码传输器继续处理OAuth邮箱。

验证：回调原鉴权断言503而非401失败；新增数据库刷新失败断言、STARTTLS后缺EHLO断言均先失败后修复。发布前补充Google/Microsoft受众与azp断言：原6个拒绝用例失败，追加其他受众检查又确认2个失败；最终OAuth/契约/发送幂等共43项通过（36.94s），含10项受众回归。此前OAuth/契约18项通过（20.09s）；OAuth与发送幂等组合32项通过（31.79s）；扩展OAuth/B7/P3/IMAP组合52项通过（208.35s），最后的握手及密码替换保护另由上述定向回归验证；平台测试凭据改为运行时生成后相关2项通过（4.68s）。完整前端18文件81项通过（215.65s），lint 0错误/10项既有警告，类型及生产构建通过。签名、客户端凭据、访问/刷新令牌均在测试运行时生成，采用真实PostgreSQL、HTTP接口、本地签名校验及服务商/SMTP/IMAP替身；没有将其称为真实供应商集成验证。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest backend/tests/test_audit_oauth.py backend/tests/test_audit_oauth_contract.py backend/tests/test_audit_delivery_intents.py -q
npm --prefix frontend test -- --maxWorkers=1
npm --prefix frontend run lint
npm --prefix frontend run build
```

## 默认关闭及剩余外部验证

`TL_MAILBOX_OAUTH_ENABLED=false`为默认值。本次没有读取或配置真实应用密钥、连接真实邮箱、外发邮件或更改服务商账号权限。正式应用需通过受支持的秘密管理渠道配置对应CLIENT_ID/CLIENT_SECRET及已有TL_CREDENTIALS_KEY，TLS公网基址必须为无路径的可信origin，并注册以下精确回调：

- Google：`<TL_PUBLIC_BASE_URL>/mailboxes/oauth/google/callback`
- Microsoft：`<TL_PUBLIC_BASE_URL>/mailboxes/oauth/microsoft/callback`

真实集成尚受环境和授权范围限制：操作员需要提供已注册应用及隔离测试邮箱，在明确授权的环境验证同意/取消、scope、token刷新、供应商撤权、SMTP NOOP认证与IMAP只读登录；实际发送需另行批准。Google范围验证、Microsoft租户SMTP AUTH及同意策略遵守各服务商现行要求。通过后才由操作员开启功能。本地修复完成不表示已部署或真实OAuth已上线验收。
