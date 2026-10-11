# 恢复顺序与隔离演练（OPS-05）

恢复必须提供同目录、同时间戳的db dump、uploads tar.gz和SHA256SUMS清单。宿主机需要Python 3.11以上（或通过RESTORE_PYTHON指定），容器使用现有Python。缺附件、哈希不符、清单重复、路径穿越、链接、设备文件或保留恢复目录均在任何Docker操作前拒绝。原脚本的提示变量另有非ASCII分隔符解析问题；故障复现仅在临时副本修正变量花括号后，8组断言证明原流程会在验证前尝试停止服务。

新流程先校验，再确认RESTORE，创建临时数据库并执行真实pg_restore、暂存附件。全部成功后停止worker/beat/caddy/backend，禁用旧库连接并终止剩余会话，在事务中交换数据库名称；旧库和附件保留。附件切换写入移动日志，重复执行不会覆盖同一恢复记录。随后迁移、仅启动backend、检查/readyz（数据库、Redis及精确迁移版本）。任何失败保留现场及阶段信息，不能继续启动写入服务。

成功仍保持worker、beat和caddy停止。必须核对备份时点之后的SMTP、供应商计费、支付、DSR和消息队列状态，再由操作员启动服务。恢复数据库不能撤销外部已发生的操作；不能把旧broker任务直接重放当成安全恢复。原数据库允许连接标记保持关闭；恢复元数据排除在后续附件备份之外，旧副本按运维审批人工清理。

本次证据：真实隔离PostgreSQL17及Redis，3项完整恢复/迁移失败/就绪失败演练通过（32.458s），校验实际表数据、附件内容、旧库连接禁用、旧附件保留、服务启动顺序；另有数据库与附件完整回退演练。5项文件/预检测试通过（含6类危险归档子场景）；原备份脚本故障回归继续通过。Docker命令由本地适配器连接真实数据库并运行实际迁移/就绪函数；本机没有Docker，因此这些结果不声称Docker容器端到端或生产恢复已验收。

```sh
.venv/bin/python -m unittest discover -s deploy/tests -v
PGHOST=127.0.0.1 PGPORT="$AUDIT_PG_PORT" PGUSER="$AUDIT_PG_USER" \
RESTORE_PG_BIN="$AUDIT_PG_BIN" RESTORE_TEST_REDIS_URL="$AUDIT_REDIS_URL" \
.venv/bin/python -m unittest discover -s deploy/tests -p test_restore_postgres.py -v
```

真实演练必须显式设置隔离本机数据库客户端、连接参数及Redis；默认跳过，拒绝非环回地址。生成的数据库名只在该次测试追踪和清理，不触碰既有库。

## 操作员恢复后检查

1. 记录脚本输出的previous数据库名、恢复token、备份时间和应用版本；确认/readyz成功。
2. 在内部验证账号登录、业务记录及附件引用/下载；核对备份数据库与附件的业务一致性。
3. 对备份后已发生的外部动作逐项核对，处理未知发送收据、预占、DSR和Celery任务；必要时保持业务暂停。不要执行FLUSHALL或无差别清空共享Redis。
4. 经运维变更批准后使用既有`dc`启动`worker beat`，检查积压与错误，再启动`caddy`。这一步会恢复真实外发，自动恢复脚本不执行。

## 回退到保留副本

先停止worker/beat/caddy/backend。保留当前恢复库为新的唯一名称，在连接postgres管理库的事务中，将previous库改回业务库名，再开启业务库连接；使用psql的`format('%I', ...)`标识符引用，不拼接SQL。保留被替换的恢复库供核对，不能DROP原库。

对已完成的附件切换，可运行下面命令恢复previous目录并将恢复出来的附件另存到replaced-restored（先替换TOKEN）：

```sh
dc run --rm --no-deps -T --entrypoint python backend \
  -c "$(cat deploy/scripts/restore_files.py)" rollback TOKEN
```

该命令拒绝未完成的切换及重复回退。若断电/磁盘错误发生于移动过程中，保持服务停止，按journal.json逐项核对原目录、暂存目录及previous目录再人工恢复；所有副本保留。回退须使用与旧库迁移版本一致的应用镜像，重新完成就绪和业务验证后再决定恢复外发。
