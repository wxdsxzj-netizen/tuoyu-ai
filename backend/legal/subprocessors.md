# Sub-processors — {{COMPANY}}

> 模板：按实际启用的服务删改。Updated {{DATE}}.

| Sub-processor | Purpose | Data | Location | Enabled when |
|---|---|---|---|---|
| Cloud server provider (e.g. Alibaba Cloud HK / AWS Singapore) | Hosting, database, backups | All service data | Hong Kong / Singapore | Always |
| Object storage (optional) | Off-site encrypted backups | Encrypted backup files | Customer-chosen region | `BACKUP_S3_*` set |
| LLM provider (Qwen / DeepSeek) | Drafting, translation, classification | Message text, company facts | China | Real LLM enabled |
| Hunter.io | Contact discovery / email finding | Company domain, names | USA | `HUNTER_API_KEY` set |
| MillionVerifier | Email verification | Email addresses | EU/USA | `MILLIONVERIFIER_API_KEY` set |
| Customer's own mail provider (SMTP/IMAP) | Sending and receiving email | Email content | Per provider | Mailbox bound |

Public data sources without personal-data transfer: ECB reference FX rates, OFAC/CSL sanction lists, DNSBL lookups (domain names only).
