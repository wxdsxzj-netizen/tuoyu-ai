# 导入与Agent提交恢复、任务轮询（F08/F14）

导入和Agent创建接口要求UUID `request_id`，按当前租户与已认证创建人保存业务请求摘要及对象关联。重试相同内容返回当前对象，内容改变明确拒绝；软删除对象不会因重试而复活。租户事务锁与数据库唯一约束共同覆盖并发，当前权限在查询前校验。凭据没有HTTP缓存的24小时有效期，也不保存历史响应或认证秘密。旧客户端未提供标识会返回400，前后端必须同步更新。

导入摘要包含原始文件SHA-256、文件名、类型、规范化映射及dry-run。重试或拒绝时清除额外上传副本。导入作业本身是持久队列：创建事务提交后投递，broker不可用仍返回真实queued状态，每分钟扫描补投。worker锁定租户及作业，仅处理queued；全部行变更与完成状态一起提交，重复投递不再次导入，进程中断回滚后仍可补跑。显式失败记录安全错误码，不自动重复失败业务。worker重新检查创建人的在职状态和导入权限。

前端在发送前持久保存请求标识和参数；提交期间禁止再次操作，网络结果不明时保留原意图，刷新后复用原标识。导入文件本身不存浏览器，只保存摘要与映射，刷新后需要重新选择原文件。Agent动作增加请求锁与服务器事务串行化。此项修复提交和重复操作边界；Agent后台可恢复执行仍单列AI-BILL-11，未在此提前关闭。

导入和爬取页面初次载入发现queued/running后即轮询，终态自动停止；网络失败5秒后重试，不叠加并发轮询，卸载后忽略迟到响应并清除定时器。爬取结束刷新知识条目。

## 验证与迁移

原5项后端失败断言；11项新增真实PostgreSQL/HTTP回归及公司导入、ICP、P5/P6扩展共37 passed。包含并发创建、内容冲突、租户/成员隔离、broker失败补投、worker中断前真实事务回滚、重复投递及上传副本清理。初次扩展的3项测试因遗漏导入uuid失败，补正后全组通过。

F14原2项页面失败断言；修复后3项轮询交互及3项提交恢复交互通过。全前端23 passed，lint无错误（12项既有Hook警告），TypeScript/build通过（既有大chunk提示）。测试没有真实供应商、邮件或支付调用。

冻结快照全后端检查随后发现：导入代码块编辑误移除了相邻的批量邮箱验证任务，造成2项失败；另有1项迁移测试把最新版本写死为0019。发布前已恢复原有验证任务并改为核对实际迁移head，相关计量/合规/迁移/提交恢复47项全部通过；修正后冻结cf204e4从仓库根重新运行全部测试，362 passed / 4 skipped / 5 subtests passed / 614.02s（含备份脚本）。其中3项Caddy跳过是未设置二进制路径，随后指定本机Caddy补跑部署测试8 passed / 9 subtests passed；仅OCR仍受本机缺tesseract限制。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_submission_intents.py backend/tests/test_companies_import.py backend/tests/test_icp.py backend/tests/test_p5.py backend/tests/test_p6.py
.venv/bin/ruff check backend
cd frontend
npm test
npm run lint
npm run build
```

新增0020，为既有导入/Agent对象追加可空请求标识及摘要；不猜测历史请求身份、不修改历史业务数据。存在新凭据时迁移拒绝降级，避免删除去重证据。部署时先停止旧创建入口和旧导入worker，执行迁移，再同步前后端与新worker/beat；本任务未执行生产迁移或部署。回退会恢复重复操作及丢失队列恢复能力，需先停止相关写入并保全凭据。
