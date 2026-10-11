# 既有业务操作入口（F17）

用户确认的最小范围已落地：产品编辑保留未编辑的参数/翻译等结构化字段；产品详情可以新建和维护SKU及阶梯价；静态名单可以搜索、翻页选择并添加成员；样品可申请、填写物流、记录反馈并按原状态机推进；订单可按客户手工创建或关联报价，沿用原报价状态校验、状态推进、取消和收款入口。没有增加自动履约、状态或权限。

SKU新增部分更新端点，仅允许本租户、本产品且未删除的SKU。未提交的成本/底价等字段保留；无price.floor权限不能清空、修改或读取这些字段。名单成员服务端校验可见性和存在性，锁定名单防止并发覆盖。样品推进锁定记录，重复操作仍受原状态机约束。写按钮遵守既有权限。

新建样品/订单即时锁定重复点击，持久保存当前浏览器会话内的请求身份和输入；未知结果重试同一请求，超过23小时窗口暂停自动重试，要求先核对记录。选择器提供服务端搜索和加载后续页。原有收款幂等保护保留。

最初5项页面回归失败，最终8项业务组件回归通过（包括隐藏写入口、超时重载后同请求重试和样品反馈）；原有3项收款和4项表单状态回归也通过。整套前端一次运行62通过、1项因5秒超时失败；该产品组件改用与其他复杂交互相同的20秒上限后，重新跑全部8项业务组件通过。lint/type/build通过，保留既有警告；后续分页整合后的完整前端75项通过（228.21s）。

后端原SKU编辑404失败、名单接受外租户/不存在ID失败。最终8项SKU/名单/既有B2回归通过（18.28s）；扩展B2/B3共13通过、1项因无本地OCR工具跳过（24.52s）。数据库为本地隔离PostgreSQL，未调用外部供应商。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_product_maintenance.py backend/tests/test_b2.py
npm --prefix frontend test -- --maxWorkers=1 tests/business-entrypoints.test.tsx
npm --prefix frontend run lint
npm --prefix frontend run build
```

无数据库迁移。回退UI不会删除已创建业务记录；应保留SKU更新和名单服务端边界校验，不能通过回退恢复跨租户引用。
