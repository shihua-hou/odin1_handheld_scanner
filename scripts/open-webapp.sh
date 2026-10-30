#!/usr/bin/env bash
set -euo pipefail
systemctl --user start odin-workbench.service
for _ in $(seq 1 30); do
  if curl --silent --fail --max-time 2 http://127.0.0.1:8080/api/status >/dev/null; then
    exec xdg-open 'http://127.0.0.1:8080/workbench'
  fi
  sleep 1
done
if command -v notify-send >/dev/null 2>&1; then
  notify-send 'Odin1 WebApp' '工作台启动失败，请检查 odin-workbench.service 和 logs/web.log'
fi
exit 1
