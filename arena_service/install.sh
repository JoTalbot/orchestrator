#!/usr/bin/env bash
# Установка REST-сервиса arena-api как systemd-демона.
#   bash arena_service/install.sh            # установить + запустить
#   bash arena_service/install.sh --remove   # снять
set -euo pipefail
ROOT=/opt/orchestrator
UNIT=/etc/systemd/system/arena-api.service

if [[ "${1:-}" == "--remove" ]]; then
  sudo systemctl disable --now arena-api 2>/dev/null || true
  sudo rm -f "$UNIT"; sudo systemctl daemon-reload
  echo "arena-api снят"; exit 0
fi

command -v curl >/dev/null || { echo "нужен curl"; exit 1; }

# браузер (CDP :9222) живёт в docker-контейнере — проверяем его
if ! curl -s --max-time 5 http://127.0.0.1:9222/json/version >/dev/null; then
  echo "(!) CDP на 127.0.0.1:9222 не отвечает. Поднимите браузер:"
  echo "    sudo docker start octopus-browser-chromium"
fi

# порт не должен быть занят (раньше 8787 пересекался с control-plane оркестратора)
PORT=$(sed -n 's/.*ARENA_API_PORT=\([0-9]*\).*/\1/p' "$ROOT/arena_service/arena-api.service")
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "(!) порт $PORT уже кем-то занят:"; sudo ss -ltnp | grep ":$PORT " || true
  echo "    поменяйте ARENA_API_PORT в arena_service/arena-api.service"
fi

sudo cp "$ROOT/arena_service/arena-api.service" "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now arena-api
sleep 3
sudo systemctl --no-pager --lines=5 status arena-api | head -12 || true

TOKEN=$(cat "$ROOT/.secrets/arena_service_token.txt" 2>/dev/null || \
        curl -s http://127.0.0.1:8790/token || true)
echo
echo "=== arena-api запущен ==="
echo "токен:  $TOKEN"
echo "проверка:"
curl -s -H "X-API-Key: $TOKEN" http://127.0.0.1:8790/health | head -c 600 || true
echo
echo
echo "swagger: http://127.0.0.1:8790/docs"
echo "MCP:     $ROOT/arena_mcp/server.py"
