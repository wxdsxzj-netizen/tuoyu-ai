#!/usr/bin/env bash
# 生成 deploy/.env.prod 里需要的随机密钥（只打印，不写文件）。复制到 .env.prod 后妥善保管（丢失 TL_CREDENTIALS_KEY 将无法解密已绑定的发件箱密码）。
set -euo pipefail
rand() { openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-"$1"; }
echo "POSTGRES_PASSWORD=$(rand 32)"
echo "REDIS_PASSWORD=$(rand 32)"
echo "TL_JWT_SECRET=$(rand 64)"
echo "TL_UNSUBSCRIBE_SECRET=$(rand 48)"
echo "TL_PLATFORM_ADMIN_TOKEN=$(rand 48)"
# Fernet 密钥：32 字节 urlsafe base64
echo "TL_CREDENTIALS_KEY=$(openssl rand 32 | base64 | tr '+/' '-_')"
