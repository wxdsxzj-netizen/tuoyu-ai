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
