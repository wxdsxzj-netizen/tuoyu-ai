# 表单状态、分页和名单（F09/F12/F16/F18）

DSR表格与服务端统一每页20项，禁用未实现的页大小切换，21条fixture验证末行与第二页。ICP每次打开先清空表单，新建显式初始化全部6个数组。学习建议去除window全局目标，按弹窗持有目标、重新打开清空、必选且请求期间禁止再操作，失败保留当前目标。

动态名单使用联系人标准catch_all；服务端读取旧名单的accept_all条件时兼容映射，避免仅修新表单而遗留旧名单。未跨租户读取或改写联系人状态，无数据库迁移。

四项页面原失败断言均复现，修复后4 passed；旧枚举兼容原失败断言修复后与既有发现/名单场景共2 passed，真实PostgreSQL和HTTP，含另一租户拒绝访问。Ruff、前端lint（11项既有警告）、TypeScript/build通过；构建仍有既有chunk大小提示。

命令：frontend中npm test -- --run tests/form-state.test.tsx；npm run lint；npm run build。后端使用隔离测试数据库及TL_SECRETS_FILE=/dev/null运行pytest -q -p no:cacheprovider backend/tests/test_audit_list_status.py backend/tests/test_b3.py::test_discovery_icp_score_lookalike_semantic_exclusions_lists_saved。无需迁移，代码整体回退会恢复上述缺陷。
