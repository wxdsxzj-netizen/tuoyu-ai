# 就绪检查与HTTP指标（OPS-03/OPS-04）

就绪检查比较真实数据库alembic_version全集与镜像内迁移head全集。缺表/空版本/旧版本/未知未来版本均不就绪，HTTP503；进程存活探针保持独立。响应包含实际版本、期望版本和migrations_current，部署冒烟说明已同步。

生产启动器在导入Prometheus前创建每次主进程独有的临时目录并传给所有Uvicorn worker。HTTP计数与直方图由MultiProcessCollector聚合，业务SQL快照只收集一次。worker重启保留主进程生命周期内累计值，主进程重启换新目录，退出清理；保持默认2个worker。开发单进程仍可使用原registry。

原4项全部失败；修复后新增4项+P6共9 passed / 12.20s。随后增加真实生产启动器集成，5项专项通过 / 11.35s：实际启动两个HTTP子进程，20次回环请求聚合为20，DB快照仅1份，主进程退出后目录清理；另有独立进程退出/重启累计5→7与直方图验证。迁移元数据故障测试使用真实隔离PostgreSQL并恢复原值。Ruff通过。

命令：TL_SECRETS_FILE=/dev/null及隔离数据库/Redis运行pytest -q -p no:cacheprovider backend/tests/test_audit_readiness_metrics.py backend/tests/test_p6.py。无数据库迁移，生产镜像入口变化需重建镜像；本机未运行Docker，下一次修复分支CI会验证镜像。未操作生产。
