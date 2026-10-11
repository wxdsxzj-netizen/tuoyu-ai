# 跨月预占与点数结转（AI-BILL-04）

已采用获准的保守规则：当订阅月份结束且仍有未结清预占时，暂停该租户月结和新付费操作。原任务仍可使用其已预占额度完成，或取消后释放剩余额。缓存、免费操作及其他服务不受此规则限制；不更改租户或账号状态。

月结按月份顺序补跑，先消耗旧结转，再消耗当期赠送；旧结转剩余额到期，只有当期未用赠送按套餐比例结转。原任务跨月结算归入其预占月份。次月赠送、结转与到期分录使用次月起始时间和业务键，避免迟跑把额度记到错误月份。账单与额度在同一事务内写入，留存月结审计事件；失败整体回滚。

每日后台作业补跑所有到期月份。一个租户的预占或数据库异常不会阻断其他租户；新付费入口也会先补跑已到期月份。重复调用不重复出账或发额度。界面显示待结清月份与真实剩余预占，引导完成或取消原任务；没有要求充值来消除该限制。历史订阅超过120个月或周期不一致会停止并返回具体原因，需核查后处理。

## 验证

修复前7项失败断言；修复后扩展到12项真实隔离PostgreSQL回归，覆盖FIFO、完成/取消、数据库事务中止、失败月结恢复、重复补跑、多月补跑、租户隔离、未来月份拒绝及两会话同时月结。与商机账单、钱包、B4/P5扩展合计41项通过。前端14项通过（其中2项结算提示），lint无错误且保留12项既有Hook警告，TypeScript/build通过并保留既有大chunk提示。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_rollover.py backend/tests/test_audit_monthly_billing.py backend/tests/test_audit_wallet.py backend/tests/test_b4.py backend/tests/test_p5.py
.venv/bin/ruff check backend
cd frontend
npm test
npm run lint
npm run build
```

无新增迁移，依赖0018预占及0019账单业务键。旧的月结已产生的历史错误不自动改账，需按原账本核对。回退代码会恢复错误结转和提前月结风险，必须暂停月结及新付费写入并保全账本后评估；没有执行生产回退、支付或供应商调用。
