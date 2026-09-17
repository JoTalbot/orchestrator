#!/usr/bin/env bash
# Фоновый запуск экспорта чатов Arena AI с логом.
# Использование: bash arena_export/run_export.sh [доп. аргументы для export_chats.py]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
LOG="logs/arena_export_$(date +%Y%m%d_%H%M%S).log"
nohup .venv/bin/python arena_export/export_chats.py "$@" >"$LOG" 2>&1 &
echo "Запущено (PID $!). Лог: $LOG"
echo "Следить: tail -f $LOG"
