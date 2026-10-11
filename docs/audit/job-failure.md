# 通用作业失败状态

AI-BILL-13：作业执行异常后先回滚损坏事务，重新加载Job，保存failed和审计；之前已提交的进度保留，未提交副作用回滚。错误字段只存已登记业务错误码或异常类型，避免SQL参数、供应商凭据进入公开作业错误。

用真实PostgreSQL `SELECT 1/0`先复现失败，修复后作业/LLM/邮箱验证扩展 **28 passed / 26.41s**，验证进度检查点、未提交副作用、失败状态和审计记录。首次扩展命令引用不存在的测试文件，未执行测试；纠正为实际存在的文件后取得上述结果。

```sh
TL_SECRETS_FILE=/dev/null TL_DATABASE_URL="$AUDIT_DATABASE_URL" TL_REDIS_URL="$AUDIT_REDIS_URL" .venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_audit_job_failure.py backend/tests/test_audit_jobs_llm.py backend/tests/test_p2_compliance.py
```

无需迁移。这不等同于Agent可恢复队列（AI-BILL-11），也不承诺数据库完全不可用时仍能写入状态。
