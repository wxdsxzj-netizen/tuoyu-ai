#!/usr/bin/env bash
# 部署后冒烟检查：deploy/scripts/smoke.sh https://app.example.com
set -euo pipefail
U=${1:?用法：smoke.sh https://你的域名}
curl -fsS "$U/livez" && echo
curl -fsS "$U/readyz" && echo
curl -fsS -o /dev/null -w "首页 %{http_code}\n" "$U/"
code=$(curl -s -o /dev/null -w "%{http_code}" "$U/metrics"); [ "$code" = "404" ] && echo "/metrics 未对公网开放 ✓" || echo "警告：/metrics 返回 $code"
curl -sI "$U/" | grep -i strict-transport-security >/dev/null && echo "HSTS ✓"
