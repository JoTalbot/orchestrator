#!/bin/bash
# Ежедневная гигиена шлюза: НИ ОДНОГО промпта, дневной бюджет не тратится.
#   1) состояние шлюза (пауза, бюджет, счётчики, очередь cleanup);
#   2) хвосты evaluation в аккаунте — dry-run, без удаления;
#   3) каталог моделей: жив ли источник и сколько моделей (--check, без записи);
#   4) «проверенные» записи, выпавшие из каталога (чистка, без запросов).
# Ставится таймером arena-housekeeping.timer (ежедневно 05:20 UTC).
set -u
GW=http://127.0.0.1:8791
cd /opt/orchestrator || exit 1
LOG=/opt/orchestrator/logs/housekeeping_$(date -u +%Y%m%d).log
exec > "$LOG" 2>&1
echo "=== $(date -u +%Y-%m-%d_%H:%M:%S) UTC ==="

echo "--- 1. состояние шлюза ---"
curl -s --max-time 20 "$GW/health" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  шлюз не ответил:", e); raise SystemExit
print("  ok=%s | пауза=%s (%s с) | интервал=%s с | серия prompt=%s" % (
    d.get("ok"), d.get("paused"), d.get("cooldown_remaining_s"),
    d.get("adaptive_interval_s"), d.get("prompt_failed_streak")))
print("  бюджет: %s | cleanup_pending: %s" % (d.get("budget"), d.get("cleanup_pending")))
c = d.get("counters") or {}
print("  счётчики: %s" % {k: c.get(k) for k in ("ok", "prompt_failed", "upstream_error",
                                                "cleanup_ok", "cleanup_fail")})'

echo "--- 2. хвосты evaluation (dry-run) ---"
timeout 300 .venv/bin/python arena_gateway/cleanup_leftovers.py --dry-run 2>&1 | tail -5

echo "--- 3. каталог моделей: источник жив? ---"
timeout 280 .venv/bin/python arena_agent/dump_models.py --check 2>&1 | tail -6

echo "--- 4. проверенные записи, выпавшие из каталога ---"
timeout 150 .venv/bin/python arena_gateway/ctl.py prune-verified 2>&1 | tail -4

echo "--- чистка старых логов (>30 дней) ---"
find /opt/orchestrator/logs -maxdepth 1 -name 'housekeeping_*.log' -mtime +30 -print -delete
echo "--- готово: $(date -u +%H:%M:%S) UTC ---"
