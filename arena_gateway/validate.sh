#!/bin/bash
# validate.sh — сквозная проверка шлюза Arena: чат, веб-поиск, стрим, картинки, вотчдоги.
#
# Особенности:
#   * уважает адаптивный темп и кулдаун шлюза (ждёт, а не долбит);
#   * при 403/429 от арены НЕМЕДЛЕННО прекращает — каждая попытка во время
#     флага reCAPTCHA продлевает его (см. docs/ARENA_GATEWAY.md §4);
#   * режимы: validate.sh [--quick] [--skip-images]
#
# Выход: 0 — всё прошло, 1 — есть отказы, 2 — шлюз недоступен.
set -u

GW="${ARENA_GW_URL:-http://127.0.0.1:8791}"
TOK="$(cat /opt/orchestrator/.secrets/arena_gateway_token.txt 2>/dev/null)"
QUICK=0; SKIP_IMAGES=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --skip-images) SKIP_IMAGES=1 ;;
  esac
done

H=(-H 'content-type: application/json')
[ -n "$TOK" ] && H+=(-H "Authorization: Bearer $TOK")

FAILS=0
step() { echo; echo "=============== $1 ==============="; }
jq() { python3 -c "$1"; }

status_field() {  # $1 — поле статуса
  curl -s "$GW/v1/arena/status" "${H[@]}" | jq "import json,sys; d=json.load(sys.stdin); print(d.get('$1', 0))" 2>/dev/null
}

pace() {  # ждём окончания кулдауна/паузы
  local cd
  while :; do
    cd="$(status_field cooldown_remaining_s)"; cd="${cd:-0}"
    [ "$cd" -le 0 ] 2>/dev/null && return 0
    echo "  …кулдаун/пауза: ещё ${cd} с — жду"
    if [ "$cd" -gt 600 ]; then sleep 600; else sleep $((cd + 3)); fi
  done
}

try_local() {  # запрос, который не доходит до арены (кулдаун не важен)
  local path="$1" body="$2" lim="${3:-300}" resp code out
  resp="$(curl -s -w $'\n%{http_code}' -m 60 -X POST "$GW$path" "${H[@]}" -d "$body")"
  code="$(printf '%s' "$resp" | tail -1)"
  out="$(printf '%s' "$resp" | head -n -1)"
  echo "  HTTP $code"
  printf '%s' "$out" | head -c "$lim"; echo
}

try() {  # try <описание> <путь> <json-тело> [лимит вывода]
  local desc="$1" path="$2" body="$3" lim="${4:-1200}" resp code out
  pace
  resp="$(curl -s -w $'\n%{http_code}' -m 400 -X POST "$GW$path" "${H[@]}" -d "$body")"
  code="$(printf '%s' "$resp" | tail -1)"
  out="$(printf '%s' "$resp" | head -n -1)"
  echo "  HTTP $code"
  printf '%s' "$out" | head -c "$lim"; echo
  case "$code" in
    200) return 0 ;;
    403|429)
      echo "  !! арена отклонила запрос (reCAPTCHA/лимит) — прекращаем валидацию,"
      echo "     чтобы не продлевать флаг аккаунта. Повторите через час:"
      echo "     curl -s -X POST $GW/v1/arena/pause -d '{\"seconds\":0,\"reset_interval\":true}'"
      FAILS=$((FAILS + 1)); return 2 ;;
    *) FAILS=$((FAILS + 1)); return 1 ;;
  esac
}

step "0. доступность шлюза"
if ! curl -s -m 10 "$GW/health" >/dev/null; then
  echo "  шлюз не отвечает на $GW — запустите: sudo systemctl start arena-gateway"; exit 2
fi
curl -s "$GW/health" | jq "
import json,sys; d=json.load(sys.stdin)
print('  ok:', d['ok'], '| вкладка:', d.get('tab_age_s'), 'с | интервал:', d.get('adaptive_interval_s'), 'с | кулдаун:', d.get('cooldown_remaining_s'), 'с | streak:', d.get('recaptcha_streak'))"

step "1. локальные вотчдоги (арену не трогаем)"
try_local /v1/chat/completions \
    '{"model":"несуществующая-модель","messages":[{"role":"user","content":"тест"}]}' 300

step "1b. предпроверка: тихий режим / кулдаун"
CD="$(status_field cooldown_remaining_s)"; CD="${CD:-0}"
if [ "$CD" -gt 0 ] 2>/dev/null; then
  echo "  шлюз в кулдауне/паузе ещё ${CD} с — реальные запросы к арене не отправляем."
  echo "  снять паузу:  curl -s -X POST $GW/v1/arena/pause -H 'content-type: application/json' \\"
  echo "               -d '{\"seconds\":0,\"reset_interval\":true}'"
  echo "  (во время флага reCAPTCHA каждая попытка продлевает его — лучше подождать)"
  exit 3
fi
echo "  кулдауна нет — можно отправлять запросы"

step "2. чат: claude-sonnet-4.5 (без стрима)"
try "чат" /v1/chat/completions \
    '{"model":"claude-sonnet-4.5","messages":[{"role":"system","content":"Отвечай одним предложением"},{"role":"user","content":"Столица Украины?"}]}' 900
rc=$?; [ $rc -eq 2 ] && { step "ИТОГ: прервано на чате"; exit 1; }

if [ "$QUICK" -eq 1 ]; then
  step "ИТОГ (--quick): чат проверен, ошибок $FAILS"; exit $((FAILS > 0))
fi

step "3. веб-поиск: sonnet:search"
try "поиск" /v1/chat/completions \
    '{"model":"sonnet:search","messages":[{"role":"user","content":"Какая сейчас погода в Днепре? Кратко."}]}' 1400
rc=$?; [ $rc -eq 2 ] && { step "ИТОГ: прервано на поиске"; exit 1; }

step "4. стрим: delta-чанки (SSE)"
pace
curl -sN -m 400 -X POST "$GW/v1/chat/completions" "${H[@]}" \
  -d '{"model":"claude-sonnet-4.5","stream":true,"messages":[{"role":"user","content":"Посчитай от 1 до 5 через запятую"}]}' \
  | head -12
echo "  (стрим: первые 12 строк выше)"

if [ "$SKIP_IMAGES" -eq 0 ]; then
  step "5. картинки: /v1/images/generations (flux-2-pro)"
  try "картинки" /v1/images/generations \
      '{"model":"flux-2-pro","prompt":"A red apple on a wooden table","n":1}' 700
  rc=$?; [ $rc -eq 2 ] && { step "ИТОГ: прервано на картинках"; exit 1; }
fi

step "6. статус шлюза"
curl -s "$GW/v1/arena/status" "${H[@]}" | jq "
import json,sys; d=json.load(sys.stdin)
print('  ok:', d['ok'], '| интервал:', d.get('adaptive_interval_s'), 'с | кулдаун:', d.get('cooldown_remaining_s'), 'с | streak:', d.get('recaptcha_streak'))
print('  счётчики:', json.dumps(d.get('counters',{}),ensure_ascii=False))
print('  неизвестные коды потока:', json.dumps(d.get('unknown_stream_codes',[]),ensure_ascii=False))
print('  последние ошибки:', json.dumps(d.get('recent_errors',[]),ensure_ascii=False)[:400])"

echo
echo "=============== ИТОГ: отказов $FAILS ==============="
[ "$FAILS" -eq 0 ] && echo "все проверки пройдены" || echo "есть падения — см. вывод выше"
exit $((FAILS > 0))
