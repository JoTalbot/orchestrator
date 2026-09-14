#!/usr/bin/env bash
# Фоновый запуск экспорта чатов ChatGPT с логом.
# Использование: bash chatgpt_export/run_export.sh [доп. аргументы для export_chats.py]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/export_$(date +%Y%m%d_%H%M%S).log"
nohup .venv/bin/python chatgpt_export/export_chats.py "$@" >"$LOG" 2>&1 &
echo "Запущено (PID $!). Лог: $LOG"
echo "Следить: tail -f $LOG"
