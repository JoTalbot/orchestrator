#!/bin/bash
# run_probe.sh [число_моделей] [модальность] — запускает пробник через HTTP шлюза
# и ждёт завершения (пробник идёт в фоне сервиса, с его же темпом и кулдауном).
GW=http://127.0.0.1:8791
N=${1:-6}
MOD=${2:-chat}
echo "$(date -u +%H:%M:%S) запуск пробника: $N моделей, модальность $MOD"
curl -s -X POST "$GW/v1/arena/probe" -H 'content-type: application/json' \
  -d "{\"limit\":$N,\"modality\":\"$MOD\"}"; echo
for i in $(seq 1 160); do
  sleep 30
  R=$(curl -s "$GW/v1/arena/status" | python3 -c "
import json,sys
p=json.load(sys.stdin).get('probe',{})
print('%s %s/%s %s' % (p.get('running'), p.get('done'), p.get('total'), p.get('error') or ''))" 2>/dev/null)
  echo "$(date -u +%H:%M:%S) probe: $R"
  case "$R" in False*) break;; esac
done
curl -s "$GW/v1/arena/status" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('--- результаты ---')
for r in d.get('probe',{}).get('results',[]):
    print('  %-5s %-6s %-36s %-22s %s' % ('OK' if r.get('ok') else 'отказ', r.get('status'),
        (r.get('name') or '')[:36], (r.get('provider') or '')[:22], (r.get('text') or r.get('error') or '')[:70]))
print('счётчики:', json.dumps(d.get('counters',{}), ensure_ascii=False))
print('адаптивный интервал:', d.get('adaptive_interval_s'), '| кулдаун:', d.get('cooldown_remaining_s'))"
