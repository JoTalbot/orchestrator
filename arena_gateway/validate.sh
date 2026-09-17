#!/bin/bash
# validate.sh — сквозная проверка шлюза: чат, веб-поиск, картинки, стрим, вотчдоги.
GW=http://127.0.0.1:8791
step() { echo; echo "=============== $1 ==============="; }
post() { curl -s -X POST "$GW$1" -H 'content-type: application/json' -d "$2"; }

step "1. health (deep)"
curl -s "$GW/health?deep=true" | python3 -m json.tool

step "2. чат: claude-sonnet-4.5 (без стрима)"
post /v1/chat/completions '{"model":"claude-sonnet-4.5","messages":[{"role":"system","content":"Отвечай одним предложением"},{"role":"user","content":"Столица Украины?"}]}' \
 | python3 -c "import json,sys; d=json.load(sys.stdin); print('content:', d['choices'][0]['message']['content'][:200] if 'choices' in d else d); print('arena:', json.dumps(d.get('arena',{}),ensure_ascii=False)) if 'arena' in d else None"

step "3. ожидание темпа (60 с)"; sleep 60

step "4. веб-поиск: sonnet:search"
post /v1/chat/completions '{"model":"sonnet:search","messages":[{"role":"user","content":"Кто выиграл последний матч Динамо Киев? Кратко."}]}' \
 | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d,ensure_ascii=False)[:1200])"

step "5. ожидание темпа (60 с)"; sleep 60

step "6. стрим: gemini/claude (delta-чанки)"
curl -sN -X POST "$GW/v1/chat/completions" -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4.5","stream":true,"messages":[{"role":"user","content":"Посчитай от 1 до 5 через запятую"}]}' | head -12

step "7. ожидание темпа (60 с)"; sleep 60

step "8. картинки: /v1/images/generations"
post /v1/images/generations '{"model":"flux-2-pro","prompt":"A red apple on a wooden table","n":1}' \
 | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d,ensure_ascii=False)[:900])"

step "9. статус шлюза (счётчики, неизвестные коды потока)"
curl -s "$GW/v1/arena/status" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('ok:', d['ok'], '| адаптивный интервал:', d.get('adaptive_interval_s'), '| кулдаун:', d['cooldown_remaining_s'])
print('счётчики:', json.dumps(d['counters'],ensure_ascii=False))
print('неизвестные коды потока:', json.dumps(d['unknown_stream_codes'],ensure_ascii=False))
print('последние ошибки:', json.dumps(d['recent_errors'],ensure_ascii=False)[:400])"

step "10. вотчдог: ошибка модели / кулдаун"
post /v1/chat/completions '{"model":"несуществующая-модель","messages":[{"role":"user","content":"тест"}]}' | head -c 300
echo
