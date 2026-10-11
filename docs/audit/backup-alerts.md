# 备份成功状态缺失告警（OPS-02）

原规则只比较已经存在的成功时间戳；从未完成备份时，没有时间序列参与比较，因此不会告警。现在分别检查超过26小时的成功时间戳、每个 node 实例缺失的时间戳、整个 node 抓取目标消失；均保持30分钟等待，避免正常启动瞬时误报。一个健康实例不能掩盖另一个实例缺失。

使用与部署镜像一致的官方 Prometheus 2.54.1 Promtool，按[规则单元测试格式](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)验证；全局缺失使用官方 [absent](https://prometheus.io/docs/prometheus/latest/querying/functions/#absent) 语义。原规则在缺失场景失败，修复后6组场景全部 SUCCESS：首次无备份、30分钟等待、近期成功、过期成功、多实例、首次成功恢复和整个抓取目标缺失。

复现命令（仓库根目录）：

```sh
promtool check rules deploy/monitoring/alerts.yml
cd deploy/monitoring
promtool test rules alerts.test.yml
```

CI 使用 `prom/prometheus:v2.54.1` 容器执行同一测试。本机使用校验官方 SHA256 后的同版本二进制；没有访问生产 Prometheus 或备份数据。此项验证告警表达式，不替代备份完整性检查和恢复演练。部署时重载告警规则；回退只涉及该规则，但会重新暴露缺失状态漏报。
