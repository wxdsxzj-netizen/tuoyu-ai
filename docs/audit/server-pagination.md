# 服务端分页与远程选择（F13）

询盘、收件箱、Agent、交接、客户、商机、报价、形式发票、投递出入站与意图列表使用服务端总数和页码，不再把第一页当成全部数据。商机看板明确标注当前页及全局总数。团队成员、客户、商机、报价和询盘选择器支持远程搜索及继续加载，保留已选择项。历史成员姓名缓存不作为可选人员全集。

统一分页钩子在过滤条件变化时回到第一页，忽略旧请求结果，展示加载错误及重试；删除最后一页记录后校正页码。相关后端使用稳定ID次序，约束非法页码，并提供必要的搜索条件。形式发票显式传page返回分页对象；旧调用未传page仍保留原数组响应。

前端初始失败证明第21条无法到达；修复及夹具校正后，全套16文件75项通过（228.21s）。包含12项分页、远程成员选择、异步竞态及错误重试回归。断线期间和较高并发的超时运行不计为通过。lint为0错误、10项既有警告；生产构建包含TypeScript检查。

后端形式发票原响应数组不满足分页断言；最终分页+P4组合9项通过（61.38s），增加搜索/非法页码后分页+P5选定组合7项通过（60.13s）。真实隔离PostgreSQL，前端为React/jsdom交互测试。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest backend/tests/test_audit_pagination.py backend/tests/test_p4.py -q
npm --prefix frontend test -- --maxWorkers=1
npm --prefix frontend run lint
npm --prefix frontend run build
```

无数据库迁移；回退将重新隐藏后续记录，不建议回退到客户端假分页。
