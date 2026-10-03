#!/bin/bash
# После тихой паузы (флаг аккаунта) — минимальная проверка: снять паузу и сделать
# 2 запроса (claude-sonnet-5 и gemini-3.6-flash). Не долбить: если снова 429,
# адаптивный интервал сам вырастет, а автопауза по серии отправит шлюз отдыхать.
set -u
T=$(sudo cat /opt/orchestrator/.secrets/arena_gateway_token.txt)
LOG=/opt/orchestrator/logs/resume_probe_$(date -u +%Y%m%d_%H%M%S).log
exec > "$LOG" 2>&1
gw() { curl -s --max-time 20 -H "Authorization: Bearer $T" "$@"; }
echo "старт: $(date -u +%Y-%m-%d_%H:%M:%S) UTC"
echo "--- health до ---"
gw localhost:8791/health | python3 -c 'import json,sys;d=json.load(sys.stdin);print("ok",d["ok"],"| пауза",d["paused"],"| кулдаун",d["cooldown_remaining_s"],"| бюджет",d["budget"],"| интервал",d["adaptive_interval_s"])'
echo "--- снимаю паузу (интервал не сбрасываю) ---"
gw -X POST -H 'content-type: application/json' -d '{"seconds":0}' localhost:8791/v1/arena/pause; echo
echo "--- пробник: claude-sonnet-5, gemini-3.6-flash (chat) ---"
gw -X POST -H 'content-type: application/json' \
   -d '{"models":["claude-sonnet-5","gemini-3.6-flash"],"modality":"chat"}' \
   localhost:8791/v1/arena/probe; echo
for i in $(seq 1 120); do
  sleep 20
  S=$(gw localhost:8791/v1/arena/status)
  R=$(echo "$S" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("probe",{}).get("running"))' 2>/dev/null)
  echo "$(date -u +%H:%M:%S) running=$R"
  [ "$R" = "False" ] && break
done
echo
echo "=== ИТОГ ==="
gw localhost:8791/v1/arena/status | python3 -c '
import json,sys
d=json.load(sys.stdin); p=d.get("probe",{}) or {}
for r in p.get("results") or []:
    if r.get("ok"):
        print("  OK    %-30s %6.1f с  %s" % ((r.get("name") or "")[:30], (r.get("ms") or 0)/1000.0, (r.get("text") or "")[:40]))
    else:
        print("  ОТКАЗ %-30s HTTP %-4s %s" % ((r.get("name") or "")[:30], r.get("status"), (r.get("error") or "")[:70]))
print("  бюджет:", d.get("budget"), "| интервал:", d.get("adaptive_interval_s"), "| кулдаун:", d.get("cooldown_remaining_s"), "| серия prompt:", d.get("prompt_failed_streak"))
print("  вердикт:", "ФЛАГ СНЯТ — можно работать" if p.get("results") and any(r.get("ok") for r in p["results"]) else "флаг, похоже, ещё держится")
'
echo "готово: $(date -u +%H:%M:%S) UTC"

echo
V=$(gw localhost:8791/v1/arena/status | python3 -c 'import json,sys;p=json.load(sys.stdin).get("probe",{});print(1 if any(r.get("ok") for r in (p.get("results") or [])) else 0)')
if [ "$V" = "1" ]; then
  echo "=== vision: картинка 1x1 уходит в чат (загрузка шлюзом, без CLI) ==="
  IMG=$(python3 - <<'PYEOF'
import base64, zlib, struct
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \
      chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")
print(base64.b64encode(png).decode())
PYEOF
)
  gw --max-time 400 -X POST -H 'content-type: application/json' \
     -d "{\"model\":\"claude-sonnet-5\",\"arena_wait_budget\":900,\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Какого цвета этот пиксель? Ответь одним словом.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$IMG\"}}]}]}" \
     localhost:8791/v1/chat/completions | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  не JSON:", str(e)[:120]); raise SystemExit
if "error" in d:
    print("  ОТКАЗ:", json.dumps(d["error"], ensure_ascii=False)[:200])
else:
    print("  ОТВЕТ:", (d.get("choices") or [{}])[0].get("message", {}).get("content", "")[:200])
    print("  модель:", d.get("model"))
'
else
  echo "пробник не дал ни одного OK — vision-проверку не делаю (флаг ещё держится)"
fi
