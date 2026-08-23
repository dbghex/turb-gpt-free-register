#!/usr/bin/env bash
set -euo pipefail

# 从任意工作目录启动项目 WebUI。
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# 通过 webui.sh 的 --auth-code 参数传入，优先级高于 .env 中的旧值。
# 默认启动，也保留 stop/restart/status/logs 等管理参数。
if [[ "$#" -eq 0 ]]; then
  set -- start
fi
AUTH_CODE="Admin@123" ./webui.sh "$@"
