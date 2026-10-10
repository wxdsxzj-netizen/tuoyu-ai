"""权限点与预置角色。P0 预置 owner / admin / sales，后续阶段按 PRD 03 §6 扩展。"""

PERMISSIONS: dict[str, tuple[str, str]] = {
    "tenant.read": ("查看组织信息", "View organization"),
    "tenant.update": ("修改组织信息", "Update organization"),
    "user.read": ("查看成员", "View members"),
    "user.manage": ("管理成员", "Manage members"),
    "role.read": ("查看角色", "View roles"),
    "audit.read": ("查看审计日志", "View audit logs"),
    "job.run": ("运行作业", "Run jobs"),
    "job.read": ("查看作业", "View jobs"),
    "llm.use": ("使用 AI", "Use AI"),
    "billing.manage": ("计费与解约", "Billing"),
    "kb.read": ("查看知识库", "View knowledge base"),
    "kb.manage": ("管理知识库与官网解析", "Manage knowledge base"),
    "icp.read": ("查看 ICP", "View ICP"),
    "icp.manage": ("管理 ICP", "Manage ICP"),
    "company.read": ("查看公司与联系人", "View companies & contacts"),
    "company.manage": ("新建/编辑公司与联系人", "Manage companies & contacts"),
    "contact.enrich": ("调用联系人数据源", "Enrich contacts"),
    "import.run": ("导入数据", "Import data"),
}

SYSTEM_ROLES: dict[str, tuple[str, set[str]]] = {
    "owner": ("所有者", set(PERMISSIONS)),
    "admin": ("管理员", set(PERMISSIONS) - {"billing.manage"}),
    "sales": ("业务员", {"tenant.read", "user.read", "job.run", "job.read", "llm.use",
                         "kb.read", "icp.read", "company.read", "company.manage", "contact.enrich", "import.run"}),
}
