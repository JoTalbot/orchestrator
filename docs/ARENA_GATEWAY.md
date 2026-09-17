# Шлюз Arena → OpenAI-совместимый API

Полнофункциональный мост между `arena.ai` (режим **Direct**) и любым OpenAI-совместимым
клиентом: Cursor, OpenAI SDK (Python/Node), `curl`, LangChain, балансировщик AIOS и Hermes.

* Сервис: `arena-gateway.service`, `http://127.0.0.1:8791` (слушает `0.0.0.0`)
* Код: `/opt/orchestrator/arena_gateway/` (`app.py`, `engine.py`, `models.py`, `config.py`, `ctl.py`)
* Токен: `/opt/orchestrator/.secrets/arena_gateway_token.txt` (`Authorization: Bearer …`)
* Данные: `data/arena/models_catalog.json` (1074 модели), `direct_models_verified.json` (проверенные),
  `server_actions.json` (React Server Actions арены)

---

## 1. Как это работает

```
Cursor / SDK / Hermes
        │  POST /v1/chat/completions  (OpenAI-формат, stream=true|false)
        ▼
  шлюз :8791  ── очередь с темпом, вотчдоги, ретраи
        │  CDP (127.0.0.1:9222) → выделенная вкладка Chrome на https://arena.ai/text/direct
        ▼
  в странице: grecaptcha.enterprise.execute(sitekey, {action:"chat_submit"})  → токен v3
        │
        ▼
  POST https://arena.ai/nextjs-api/stream/create-evaluation
  { id, mode:"direct-battle", modality:"chat", modelAId:<UUID модели>,
    userMessageId, modelAMessageId,
    userMessage:{content, experimental_attachments:[], metadata:{}},
    recaptchaV3Token }
        │
        ▼  SSE-поток (протокол AI SDK, префикс слота модели a/b)
  a0:"текст дельты"        ← text-delta
  ad:{"finishReason":"stop"} ← finish
        │
        ▼
  разбор → OpenAI-ответ (или SSE-чанки) → клиенту
        │
        ▼
  закрытие чата: React Server Action deleteEvaluationSession(<id>)
```

Ключевые факты, добытые разведкой (17.09.2026):

| Что | Значение |
|---|---|
| Эндпоинт запроса | `POST /nextjs-api/stream/create-evaluation` |
| Продолжение диалога | `POST /nextjs-api/stream/post-to-evaluation/{sessionId}` (то же тело без `mode`) |
| Режимы (`EChatMode`) | `direct`, `direct-battle`, `side-by-side`, `battle` |
| Новый чат | только `direct-battle` (`direct` → 400 «'direct' mode is not allowed when starting a new conversation») |
| Модальности (`EEvaluationModality`) | `auto`, `chat`, `webdev`, `search`, `image`, `p2l`, `video`, `audio` |
| Выбор модели | поле `modelAId` = UUID из каталога; в URL страницы — `?model_a=<publicName>` |
| Анти-бот | reCAPTCHA **Enterprise** v3 (sitekey `6LeTGMcs…`), при отказе — v2-чекбокс (sitekey `6Le3_cYs…`) и поле `recaptchaV2Token` |
| Стоимость | `battle:chat`=0, `battle:*`=0.5, `direct-battle:*:*`=1, `side-by-side:*`=1 |
| Удаление чата | server action `deleteEvaluationSession` (REST `DELETE /api/chat/{id}` для evaluation → 404) |
| История | `GET /api/history/unified` → записи `type:"evaluation"`, `mode:"direct-battle"` |

## 2. Эндпоинты шлюза

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/v1/chat/completions` | чат; `stream:true` → SSE-дельты |
| POST | `/v1/completions` | легаси-промпт (`prompt` → messages) |
| POST | `/v1/images/generations` | генерация картинок (`modality=image`) |
| GET | `/v1/models` | список моделей (`?all=true`, `?verified=true`, `?modality=image`, `?limit=N`) |
| GET | `/health`, `/v1/health` | состояние; `?deep=true` — проверка вкладки и reCAPTCHA |
| GET | `/v1/arena/status` | диагностика: очередь, кулдаун, счётчики, неизвестные коды потока, пробник |
| POST | `/v1/arena/probe` | фоновая проверка доступности моделей `{"limit":8,"modality":"chat","models":["…"]}` |
| DELETE | `/v1/arena/chats/{id}` | вручную закрыть чат арены |

Локальные запросы с `127.0.0.1` токена не требуют (`ARENA_GW_LOCAL_NOAUTH=1`);
для внешних — `Authorization: Bearer <токен>`.

### Расширения протокола (необязательные поля запроса)

```json
{
  "model": "claude-sonnet-4.5:search",     // суффикс :chat|:search|:image|:webdev|:video
  "messages": [...],
  "stream": true,
  "arena_modality": "search",              // альтернатива суффиксу
  "arena_mode": "direct-battle",           // режим арены
  "arena_session": "<evaluation id>",      // продолжить существующий чат (post-to-evaluation)
  "arena_wait_budget": 90                  // сколько секунд ждать в очереди/кулдауне
}
```

Ответ дополняется блоком `arena`: `evaluation_id`, `model_id`, `provider`, `modality`,
`mode`, `upstream_ms`, `bytes`, `recaptcha_ms`.

## 3. Выбор модели

Имя модели в запросе разрешается так (по порядку):

1. UUID из каталога — как есть;
2. точное `publicName` / `name` (`claude-sonnet-4-5-20250929`);
3. псевдоним (`claude-sonnet-4.5`, `sonnet`, `haiku`, `max`, `flux`, `gemini`, `gpt`, `grok`);
4. частичное совпадение с приоритетом: проверенные (✓) → ранг в лидерборде.

Суффикс `:modality` переключает модальность, а реестр сам берёт модель,
ранжированную в этой модальности (`sonnet:search` → `claude-sonnet-4-6-search`).

Свои псевдонимы — файл `data/arena/model_aliases.json` (`{"имя": "publicName"}`).

Проверка доступности (модель реально отвечает в direct-режиме):

```bash
curl -s -X POST http://127.0.0.1:8791/v1/arena/probe -d '{"limit":8,"modality":"chat"}'
curl -s http://127.0.0.1:8791/v1/arena/status | jq .probe
cat /opt/orchestrator/data/arena/direct_models_verified.json | jq '.models | to_entries[] | select(.value.ok) | .value.name'
```

## 4. Вотчдоги и лимиты

| Параметр | По умолчанию | Переменная |
|---|---|---|
| интервал между запросами к арене | 20 с | `ARENA_GW_MIN_INTERVAL` |
| всплеск | 8 запросов / 300 с | `ARENA_GW_BURST_LIMIT`, `ARENA_GW_BURST_WINDOW` |
| одновременных запросов | 1 | `ARENA_GW_CONCURRENCY` |
| ожидание токена reCAPTCHA | 20 с | `ARENA_GW_TOKEN_TIMEOUT` |
| нет ответа (первый байт) | 60 с | `ARENA_GW_FIRST_BYTE` |
| поток «завис» (нет дельт) | 60 с | `ARENA_GW_IDLE` |
| общее время запроса | 300 с | `ARENA_GW_TOTAL` |
| ожидание кулдауна перед 503 | 90 с | `ARENA_GW_COOLDOWN_WAIT` |
| проверка вкладки | каждые 30 с | `ARENA_GW_HEALTH` |

Что делает вотчдог вкладки: если вкладка пропала, уехала на другой URL или страница
перестала отвечать/потеряла `grecaptcha` — движок сам открывает новую вкладку
и переустанавливает JS-ядро (счётчики `tab_lost`, `tab_navigated`, `page_reset`).

Реакция на ошибки арены:

| Ответ арены | Действие шлюза |
|---|---|
| 403 `recaptcha validation failed` | эскалация: клик по чекбоксу reCAPTCHA v2 → ретрай с `recaptchaV2Token` |
| 429 `prompt failed` | та же эскалация v2 (сайт ведёт себя идентично) → ретрай |
| 429 `Too Many Requests` + `retry-after` | запоминаем кулдаун, новые запросы ждут или получают 503 с `Retry-After` |
| 404 `Models not found…` | модель не доступна в этой модальности → ошибка `model_not_found`/`upstream` клиенту |
| таймаут/зависание | отмена задания в странице (`AbortController`), клиенту 504 |

**Важно про кулдаун.** Серия из ~26 запросов с интервалом 3 с приводит к штрафному
`retry-after` ≈ 1200 с (20 мин), хотя глобальный счётчик `ratelimit: limit=1800;w=300`
далёк от исчерпания. Поэтому шлюз по умолчанию держит темп 20 с и не более 8 запросов
в 5 минут.

## 5. Подключение клиентов

### Cursor
Settings → Models → OpenAI API key:
* Base URL: `http://<сервер>:8791/v1`
* API Key: содержимое `.secrets/arena_gateway_token.txt`
* Model: `claude-sonnet-4-5-20250929` (или любое имя из `GET /v1/models`)

### OpenAI SDK (Python)
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8791/v1", api_key=open(
    "/opt/orchestrator/.secrets/arena_gateway_token.txt").read().strip())

r = client.chat.completions.create(
    model="claude-sonnet-4.5",
    messages=[{"role": "system", "content": "Отвечай кратко"},
              {"role": "user", "content": "Три факта о Днепре"}])
print(r.choices[0].message.content, r.arena if hasattr(r, "arena") else "")

for ch in client.chat.completions.create(model="sonnet", stream=True,
        messages=[{"role": "user", "content": "посчитай до 10"}]):
    print(ch.choices[0].delta.content or "", end="", flush=True)
```

### curl
```bash
TOK=$(cat /opt/orchestrator/.secrets/arena_gateway_token.txt)
curl -s http://127.0.0.1:8791/v1/chat/completions \
  -H "Authorization: Bearer $TOK" -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"Привет"}]}'

# веб-поиск и картинки
curl -s ... -d '{"model":"sonnet:search","messages":[{"role":"user","content":"новости AI"}]}'
curl -s http://127.0.0.1:8791/v1/images/generations -d '{"model":"flux-2-pro","prompt":"кот в скафандре"}'
```

### Hermes / AIOS
Скрипт `install_integrations.py` добавляет шлюз в балансировщик AIOS как провайдеров
`arena-*` (тир `arena`) и тир `hermes-arena` в `hermes-models.yaml`:

```bash
python3 /opt/orchestrator/arena_gateway/install_integrations.py --dry    # посмотреть
python3 /opt/orchestrator/arena_gateway/install_integrations.py          # применить
sudo systemctl restart octopus-aios.service hermes-shim.service
```

Провайдер в `/opt/aios/llm/llm_balancer.py`:
`OpenAICompatibleCloudProvider("arena-<slug>", "http://127.0.0.1:8791/v1", "<publicName>", keys, tier="arena", weight=…, timeout=150.0)`,
ключ — `ARENA_GATEWAY_KEY` в `/etc/octopus/secrets.env`.

## 6. Отладка без HTTP

```bash
cd /opt/orchestrator/arena_gateway
../.venv/bin/python ctl.py health --deep          # вкладка + ping + reCAPTCHA
../.venv/bin/python ctl.py models --verified      # проверенные модели
../.venv/bin/python ctl.py resolve "sonnet:search"
../.venv/bin/python ctl.py ask "три факта о Днепре" --model claude-sonnet-4.5 --stream
../.venv/bin/python ctl.py raw "привет" --model gemini-3.1-pro   # сырые части потока
../.venv/bin/python ctl.py probe --limit 6 --modality image
../.venv/bin/python ctl.py v2                     # проверка эскалации reCAPTCHA v2
../.venv/bin/python ctl.py cleanup <evaluation_id>
```

`ctl.py raw` показывает неизвестные коды потока — они же видны в
`/v1/arena/status → unknown_stream_codes` (нужно для добавления новых типов частей:
reasoning, tool calls, источники поиска, картинки).

## 7. Ограничения

* Один запрос за раз и темп ≥ 20 с — иначе арена даёт штрафной кулдаун на ~20 минут.
* Входные изображения (vision) пока не поддерживаются: нужен поток загрузки
  (`generateUploadUrl` → S3 → `metadata.uploads`); server action уже известен.
* Не все модели каталога отвечают в direct-режиме: часть отдаёт 404/429/403.
  Актуальный список — `direct_models_verified.json` (обновляется пробником).
* Слепой режим `battle` (бесплатный) требует предварительно назначенных сервером моделей,
  поэтому шлюз работает в `direct-battle` (множитель стоимости 1).
* Всё ходит через один профиль Chrome: если вкладку закроет человек, вотчдог поднимет новую.
