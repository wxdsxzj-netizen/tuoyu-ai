"""统一错误码（PRD 06 §10）。响应格式：{"error": {code, message, retryable, trace_id, details}}"""
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.context import trace_id_var

# code: (http, 中文提示, 英文提示, 可重试)
ERROR_CODES: dict[str, tuple[int, str, str, bool]] = {
    "COMMON_REQUIRED": (400, "请填写必填项", "Required field missing", False),
    "COMMON_LENGTH": (400, "内容长度超出范围", "Length out of range", False),
    "COMMON_EMAIL_FORMAT": (400, "格式不正确", "Invalid format", False),
    "COMMON_ENUM": (400, "取值无效", "Invalid value", False),
    "COMMON_VALIDATION": (400, "输入不合法", "Invalid input", False),
    "COMMON_UNAUTHENTICATED": (401, "请重新登录", "Please sign in again", False),
    "COMMON_FORBIDDEN": (403, "没有权限执行此操作", "Permission denied", False),
    "COMMON_NOT_FOUND": (404, "对象不存在或无权查看", "Not found", False),
    "COMMON_VERSION_CONFLICT": (409, "内容已被他人修改，请查看后再保存", "Version conflict", False),
    "COMMON_ALREADY_EXISTS": (409, "已存在", "Already exists", False),
    "COMMON_INVALID_STATE": (409, "当前状态不允许此操作", "Invalid state", False),
    "COMMON_RATE_LIMITED": (429, "操作过于频繁，请稍后再试", "Too many requests", True),
    "COMMON_DEPENDENCY_UNAVAILABLE": (503, "依赖服务暂不可用，系统会自动重试", "Dependency unavailable", True),
    "COMMON_TIMEOUT": (504, "处理超时，请稍后查看结果", "Timeout", True),
    "COMMON_INTERNAL": (500, "系统异常，已记录（附 trace_id）", "Internal error", True),
    "AUTH_INVALID_CREDENTIALS": (401, "邮箱或密码错误", "Invalid email or password", False),
    "AUTH_USER_DISABLED": (403, "账号已停用", "Account disabled", False),
    "AUTH_LOCKED": (423, "连续登录失败次数过多，账号已锁定 15 分钟", "Account locked for 15 minutes", False),
    "AUTH_PASSWORD_WEAK": (422, "密码至少 10 位，且包含字母、数字、符号中的两类", "Password too weak", False),
    "AUTH_MFA_REQUIRED": (401, "需要输入双因素验证码", "MFA code required", False),
    "AUTH_MFA_INVALID": (401, "验证码错误或已过期", "Invalid MFA code", False),
    "AUTH_SESSION_REVOKED": (401, "会话已失效，请重新登录", "Session revoked", False),
    "AUTH_API_KEY_INVALID": (401, "API Key 无效或已吊销", "Invalid or revoked API key", False),
    "ORG_INVITE_INVALID": (410, "邀请链接无效或已过期", "Invitation invalid or expired", False),
    "ORG_DEPT_DEPTH": (422, "部门层级最多 6 级", "Department depth exceeds 6", False),
    "ORG_HANDOVER_TARGET": (422, "交接对象必须是在职成员", "Handover target must be an active member", False),
    "ORG_DELEGATE_INVALID": (422, "代理人须为在职成员且不能是本人", "Invalid delegate", False),
    "APPROVAL_SELF": (403, "不能审批自己提交的申请", "Cannot approve your own request", False),
    "APPROVAL_POLICY_INVALID": (422, "发送类审批不允许超时自动通过", "Send-type approvals cannot auto-approve on timeout", False),
    "SECURITY_RATE_LIMITED": (429, "查看或导出过于频繁，请稍后再试", "Too many views/exports, slow down", True),
    "SUPPORT_GRANT_TOO_LONG": (422, "支持访问授权最长 72 小时", "Support access max 72 hours", False),
    "SUPPORT_NO_GRANT": (403, "租户未授权支持访问或授权已过期", "No active support grant", False),
    "RECYCLE_CONFLICT": (409, "恢复失败：存在同名或重复记录", "Restore conflict", False),
    "NOTIFY_NOT_OPTIONAL": (422, "该类通知不可关闭", "This notification cannot be muted", False),
    "USER_OWNER_PROTECTED": (409, "不能修改或停用所有者", "Owner cannot be changed", False),
    "FILE_TOO_LARGE": (400, "文件不符合要求（过大）", "File too large", False),
    "FILE_TYPE_NOT_ALLOWED": (400, "文件不符合要求（类型）", "File type not allowed", False),
    "COMMON_LIMIT": (400, "超出数量上限", "Limit exceeded", False),
    "KB_URL_UNREACHABLE": (422, "网址无法访问，请检查后重试", "URL unreachable", False),
    "KB_ROBOTS_DISALLOWED": (422, "网站禁止抓取部分页面，已跳过", "Disallowed by robots.txt", False),
    "KB_RIGHTS_REQUIRED": (400, "请确认您有权使用该网站内容", "Please confirm you have rights to the site", False),
    "KB_URL_NOT_ALLOWED": (400, "该网址不允许抓取（内网或非 http/https）", "URL not allowed", False),
    "ICP_NAME_EXISTS": (409, "ICP 名称已存在", "ICP name exists", False),
    "ICP_SAMPLES_LOW": (422, "成交客户少于 10 个，请使用手工创建", "Fewer than 10 won customers", False),
    "ICP_INCOMPLETE": (422, "ICP 至少需要行业、国家/地区、角色三项才能启用", "ICP incomplete", False),
    "IMPORT_NO_ROWS": (400, "文件中没有数据行", "No data rows", False),
    "IMPORT_MAPPING_INVALID": (400, "字段映射不完整（缺少必填字段）", "Invalid mapping", False),
    "DATA_PROVIDER_UNAVAILABLE": (503, "联系人数据源暂不可用（未配置或调用失败）", "Data provider unavailable", True),
    "IMPORT_SUSPECTED_DUPLICATE": (409, "疑似重复，未导入", "Suspected duplicate", False),
    "COMMON_URL_FORMAT": (400, "格式不正确", "Invalid URL", False),
    "COMMON_AMOUNT": (400, "取值无效", "Invalid amount", False),
    "COMMON_DATETIME": (400, "取值无效", "Invalid date", False),
    "DATA_PROVIDER_AUTH_FAILED": (503, "数据源 API Key 无效，请检查配置", "Data provider auth failed", False),
    "DATA_PROVIDER_RATE_LIMITED": (503, "数据源请求过于频繁，请稍后重试", "Data provider rate limited", True),
    "DATA_PROVIDER_QUOTA_EXCEEDED": (402, "数据源额度已用完，请充值或升级套餐", "Data provider credits exhausted", False),
    "DATA_PROVIDER_BAD_REQUEST": (400, "数据源拒绝了请求参数", "Data provider rejected the request", False),
    "WA_OPTIN_REQUIRED": (403, "该号码尚未 WhatsApp 同意（opt-in）", "WhatsApp opt-in required", False),
    "WA_WINDOW_CLOSED": (409, "已超过 24 小时会话窗口，只能发模板消息", "WhatsApp 24h window closed", False),
    "WA_DAILY_LIMIT": (429, "WhatsApp 今日发送已达上限", "WhatsApp daily limit reached", True),
    "CHANNEL_SEND_FAILED": (502, "渠道发送失败", "Channel send failed", True),
    "HUB_LOCKED": (402, "请先解锁该公共库实体", "Unlock required", False),
    "COMPLIANCE_CONSENT_REQUIRED": (403, "该收件人需要同意依据", "Consent basis required", False),
    "COMPLIANCE_LIA_MISSING": (403, "缺少欧盟 / 英国合法利益评估记录", "Legitimate interest assessment missing", False),
    "COMPLIANCE_REGION_DENY": (403, "该地区规则不允许此类触达（附原因码）", "Region rule denies outreach", False),
    "COMPLIANCE_REVIEWER_CONFLICT": (409, "复核人不能是提交人本人", "Reviewer must differ from submitter", False),
    "COMPLIANCE_SANCTION_LIST_STALE": (200, "制裁名单超过 72 小时未更新，需要人工确认", "Sanctions list stale", False),
    "COMPLIANCE_SUPPRESSED": (403, "收件人在抑制名单中（附来源与渠道）", "Recipient is suppressed", False),
    "COMPLIANCE_SUPPRESSION_LOCKED": (403, "此来源的抑制不能解除", "This suppression cannot be revoked", False),
    "COMPLIANCE_UNSUBSCRIBE_INVALID": (400, "退订链接无效或已损坏", "Invalid unsubscribe link", False),
    "COMPLIANCE_DSR_UNVERIFIED": (403, "身份核验未通过", "Identity not verified", False),
    "COMPLIANCE_RECORD_RESTRICTED": (423, "该记录因数据主体请求被限制处理", "Record restricted due to a data subject request", False),
    "DELIVERY_AUTH_FAILED": (422, "授权失败或已失效，请重新授权", "Mailbox authentication failed", False),
    "DELIVERY_DNS_INCOMPLETE": (409, "DNS 未配置完成，暂不能发送冷信", "DNS incomplete", False),
    "DELIVERY_DOMAIN_MAILBOX_LIMIT": (409, "每个开发信域名最多绑定 3 个发件箱", "Max 3 mailboxes per domain", False),
    "DELIVERY_FREQUENCY_LIMIT": (409, "同一企业今日发送已达上限，已顺延", "Company daily frequency reached", True),
    "DELIVERY_MAILBOX_PAUSED": (409, "发件箱处于暂停 / 冷却状态", "Mailbox paused", False),
    "DELIVERY_MAILBOX_TAKEN": (409, "该邮箱已被其他企业绑定", "Mailbox already bound elsewhere", False),
    "DELIVERY_OUTSIDE_WINDOW": (409, "不在收件人发送时间窗口内，已顺延", "Outside sending window", True),
    "DELIVERY_PROVIDER_UNAVAILABLE": (503, "该邮箱服务商的接入暂不可用，请改用 IMAP / SMTP", "Provider not available, use IMAP/SMTP", False),  # noqa: E501
    "DELIVERY_QUOTA_EXHAUSTED": (409, "今日发送额度已用完，已顺延", "Daily quota exhausted", True),
    "DELIVERY_SMTP_HOST_NOT_ALLOWED": (403, "当前环境只允许发往本地测试邮件服务器", "Only local SMTP sink allowed in this environment", False),  # noqa: E501
    "SEQ_CONTENT_QA_FAILED": (422, "内容未通过质检，已转人工编辑，未扣费", "Content failed QA", False),
    "SEQ_ENROLL_DUPLICATE": (409, "联系人已在其他活跃序列中", "Contact already in an active sequence", False),
    "SEQ_NO_MAILBOX": (422, "没有可用的发件箱", "No available mailbox", False),
    "SEQ_TEMPLATE_NOT_APPROVED": (409, "模板尚未审批通过", "Template not approved", False),
    "SEQ_VALIDATION_FAILED": (422, "序列未通过启动检查（列出问题）", "Sequence validation failed", False),
    "SEQ_REVIEWER_CONFLICT": (409, "审批人不能是提交人本人", "Approver must differ from submitter", False),
    "HANDOFF_REJECT_REASON_REQUIRED": (400, "请选择拒绝原因", "Reject reason required", False),
    "COMMON_ALREADY_TAKEN": (409, "已被他人领取", "Already taken by someone else", False),
    "INBOX_SEND_BLOCKED": (409, "发送前检查未通过，未发送", "Pre-send check failed; not sent", False),
    "SEQ_AUTONOMY_NOT_ALLOWED": (403, "本企业未开启 L2 自动通过", "L2 auto-approve is disabled for this tenant", False),
    "CRM_BANK_INFO_MISSING": (422, "尚未设置收款银行信息", "Bank info missing", False),
    "CRM_CLAIM_COOLDOWN": (409, "该客户被释放后处于领取冷却期", "Claim cooldown", False),
    "CRM_CONFIRM_LINK_EXPIRED": (410, "确认链接已过期，请联系业务员", "Confirmation link expired", False),
    "CRM_DAILY_CLAIM_LIMIT": (429, "今日领取次数已用完", "Daily claim limit reached", False),
    "CRM_DOC_LOCKED": (409, "单据已锁定，请申请解锁", "Document locked", False),
    "CRM_LOST_REASON_REQUIRED": (400, "请选择输单原因", "Lost reason required", False),
    "CRM_PRIVATE_POOL_FULL": (409, "私海客户已达上限", "Private pool full", False),
    "CRM_STAGE_DISABLED": (409, "该阶段已停用", "Stage disabled", False),
    "CRM_STAGE_REQUIRED": (400, "推进到该阶段前请先填写必填字段", "Required fields missing for this stage", False),
    "CRM_FIELD_LIMIT": (409, "自定义字段已达上限（每类对象 100 个）", "Custom field limit reached", False),
    "CRM_FIELD_REQUIRED": (400, "请填写必填的自定义字段", "Required custom fields missing", False),
    "CRM_PAYMENT_PLAN_MISMATCH": (400, "回款计划合计须等于订单金额", "Payment plan must equal order total", False),
    "CRM_PAYMENT_INVALID": (400, "回款金额无效或超过未收金额", "Invalid payment amount", False),
    "CRM_UNLOCK_PENDING": (409, "已有待审批的解锁申请", "Unlock request already pending", False),
    "KB_FACT_NEEDS_SOURCE": (400, "事实型卖点必须注明来源", "Factual selling point needs a source", False),
    "KB_DOC_UNSUPPORTED": (400, "不支持的文件格式", "Unsupported file type", False),
    "KB_DOC_TOO_LARGE": (413, "单个文件不能超过 100MB", "File too large (max 100MB)", False),
    "IMPORT_UNDO_EXPIRED": (409, "只能在导入后 24 小时内撤销，且只能撤销一次", "Undo only within 24h, once", False),
    "COMMON_IDEMPOTENCY_CONFLICT": (409, "请求已受理，请查看原操作结果", "Request already accepted; reconcile its outcome", False),
    "COMMON_IDEMPOTENCY_MISMATCH": (422, "同一幂等键用于不同请求", "Idempotency key reused with different request", False),
    "BILLING_PLAN_NOT_FOUND": (404, "套餐不存在或已下架", "Plan not found", False),
    "BILLING_SEATS_EXCEEDED": (409, "席位已满，请先增加席位或停用成员", "No seats available", False),
    "BILLING_BUDGET_EXCEEDED": (402, "超出本月预算", "Budget exceeded", False),
    "BILLING_PAYMENT_UNAVAILABLE": (503, "在线支付未开通（需商户资质），请线下付款", "Online payment unavailable", False),
    "BILLING_NOTICE_TOO_SOON": (400, "调价须至少提前 30 天通知", "Price change needs 30 days notice", False),
    "WIDGET_NOT_FOUND": (404, "组件不存在或已停用", "Widget not found", False),
    "WIDGET_ORIGIN_DENIED": (403, "该网站未被允许嵌入此组件", "Origin not allowed", False),
    "WIDGET_CONSENT_REQUIRED": (400, "请先勾选同意隐私声明", "Consent required", False),
    "WIDGET_RATE_LIMITED": (429, "提交过于频繁，请稍后再试", "Too many submissions", True),
    "CLOSURE_PENDING": (409, "已有解约申请在冷静期中", "Closure already pending", False),
    "SEQ_CREDIT_BUDGET": (402, "序列点数预算已用尽（AI 写信 0.1 点/封）", "Sequence credit budget exhausted", False),
    "RESEARCH_TARGET_UNRESOLVED": (422, "未能从内容中识别出公司（企业邮箱、网址或公司名）", "Could not identify a company", False),
    "LIST_TOO_LARGE": (400, "名单成员超过上限（10 万）", "List too large", False),
    "FX_PROVIDER_UNAVAILABLE": (503, "汇率来源不可用，请手工填写汇率", "FX provider unavailable", True),
    "CRM_SANCTION_BLOCKED": (409, "交易方筛查疑似命中，已阻止", "Blocked by sanctions screening", False),
    "BILLING_INSUFFICIENT": (402, "点数余额不足", "Insufficient credits", False),
    "AGENT_BUDGET_EXCEEDED": (409, "预估点数超过任务预算", "Estimate exceeds task budget", False),
    "AGENT_TOOL_FORBIDDEN": (403, "Agent 无权调用该工具", "Tool not allowed for agent", False),
    "AGENT_ROLLBACK_PARTIAL": (200, "部分动作不可回滚", "Partial rollback", False),
    "INQUIRY_RULE_LIMIT": (409, "分配规则最多 50 条", "At most 50 rules", False),
    "RESEARCH_TARGET_UNRESOLVED": (422, "无法识别要背调的公司", "Cannot resolve company", False),  # noqa: F601
    "LLM_UNAVAILABLE": (503, "AI 服务暂不可用", "AI service unavailable", True),
}


class AppError(Exception):
    def __init__(self, code: str, details: dict | None = None, message: str | None = None):
        assert code in ERROR_CODES, f"未登记的错误码: {code}"
        self.code = code
        self.status, self.message_zh, self.message_en, self.retryable = ERROR_CODES[code]
        self.message = message or self.message_zh
        self.details = details or {}


def error_body(code: str, message: str, retryable: bool, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable,
                      "trace_id": trace_id_var.get(), "details": details or {}}}


async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(error_body(exc.code, exc.message, exc.retryable, exc.details), status_code=exc.status)


async def validation_handler(_: Request, exc: RequestValidationError):
    errs = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(error_body("COMMON_VALIDATION", "输入不合法", False, {"errors": errs}), status_code=400)


async def unhandled_handler(_: Request, exc: Exception):
    import logging
    logging.getLogger("app").exception("unhandled: %s", exc)
    return JSONResponse(error_body("COMMON_INTERNAL", ERROR_CODES["COMMON_INTERNAL"][1], True), status_code=500)
