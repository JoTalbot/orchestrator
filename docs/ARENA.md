# Arena AI: как мы с ней работаем

Документация моста к [arena.ai](https://arena.ai) (Agent Mode) в проекте
`/opt/orchestrator`. Обновлено 17.09.2026, всё проверено на живом аккаунте.

**Главное:** публичного API у arena.ai нет. Сайт закрыт Cloudflare, прямой HTTP
(`curl https://arena.ai/api/...`) получает 429/challenge, а сессия привязана к
куки браузера и reCAPTCHA v3. Поэтому единственный рабочий путь — выполнять
запросы **из вкладки залогиненного браузера** по CDP. Так сделан весь стек ниже.

## 1. Архитектура

```
                       ┌──────────────────────────────────────────┐
 агенты, драйверы,  →  │ arena_service/app.py    REST 127.0.0.1:8790 │
 внешние процессы      │ (FastAPI, один владелец вкладки, сериализация)│
                       └───────────────┬──────────────────────────┘
 MCP-хосты          →  arena_mcp/server.py (stdio) ─┘
                                        │
                        arena_agent/arena_api.py  (CDP-клиент, парсер RSC)
                                        │  Chrome DevTools Protocol
                       Chromium в docker: octopus-browser-chromium
                       CDP 127.0.0.1:9222 (→ socat → :9223 в контейнере)
                                        │  fetch() из страницы arena.ai
                                        ▼
                                  https://arena.ai
```

Отдельный процесс — выгрузка архива: `arena_export/export_chats.py`
(своя вкладка, пишет в `data/arena/`).

Правило: **браузером владеет один процесс**. Сервис `arena-api` держит вкладку и
сериализует обращения; экспорт запускается отдельно и на время своей работы
забирает вторую вкладку. Не плодить экземпляры сервиса.

## 2. Компоненты

| Путь | Что это |
|---|---|
| `arena_agent/arena_api.py` | Ядро: `CDPTab` (WebSocket к CDP, `js()`), `ArenaAPI` (fetch из вкладки с докачкой тела по кускам), парсер RSC-пейлоада, reCAPTCHA, создание чатов, отправка сообщений, SSE-поток, песочница, архив/удаление, `wait_idle()` |
| `arena_agent/arena_ctl.py` | CLI для разовых операций: `me pulse balance list search get md files cat cost create send wait stop archive unarchive delete` |
| `arena_agent/scan_api.py` | Реверс-инжиниринг API: качает JS-бандлы в `.jscache/`, находит маршруты (RPC-цепочки `F.<path>.$method`, литералы, fetch-вызовы), умеет зондировать их из вкладки → `api_map.json` + `docs/ARENA_API.md` |
| `arena_agent/api_map.json`, `api_notes.json`, `manual_routes.json` | Карта из 116 маршрутов, ручные пометки, маршруты без литералов в бандлах |
| `arena_service/app.py` | REST API (этот документ, раздел 4) |
| `arena_service/arena-api.service`, `install.sh` | systemd-юнит и установка |
| `arena_mcp/server.py` | MCP-сервер (stdio), 17 инструментов поверх REST |
| `arena_export/export_chats.py` | Возобновляемая выгрузка всех чатов |
| `data/arena/` | `index.json`, `chats/<id>.json`, `light/<id>.json`, `files/<id>/`, `summary.json`, `errors.json` |
| `agent_profile/skills/skill-arena-ai.md`, `skills/arena-ai/SKILL.md` | Скиллы для агентов (формат проекта и формат Claude-Code) |
| `.secrets/arena_auth.json` | access/refresh-токены и куки аккаунта (не в git) |
| `.secrets/arena_service_token.txt` | Токен REST-сервиса (создаётся при первом запуске, chmod 600) |

## 3. Как устроен доступ к арене

1. **Транспорт.** Все запросы выполняются как `fetch()` внутри страницы
   `https://arena.ai` через `Runtime.evaluate`. Область видимости страницы даёт
   нужные куки, заголовки и проход через Cloudflare. Большие тела читаются
   кусками по 700 000 code units через `window.__arenaBuf` (иначе CDP режет ответ).
2. **Вкладка.** `connect()` ищет существующую вкладку арены или открывает свою
   (`own=True`) и обязательно поднимает её на передний план (`Page.bringToFront`) —
   фоновая вкладка висит на challenge.
3. **reCAPTCHA v3.** Создание чата и отправка сообщения требуют токен:
   `recaptcha(action)` исполняет штатный `grecaptcha.execute` со страницы.
   Действия: `agentic_chat_submit` (новый чат), `chat_submit` (сообщение),
   `review_feedback` (отзыв/check-in).
4. **Сессия Trigger.dev.** Агент-режим работает на Trigger.dev: у каждого чата есть
   сессия, входной поток (`/in/append`) и выходной SSE (`/out`). Токен сессии —
   `publicAccessToken`, выдаётся через `POST /api/chat/trigger-token {sessionId}`
   (дёшево) либо лежит в RSC-пейлоаде страницы чата (2 МБ, зато всегда).
5. **RSC-пейлоад.** `GET /agent/{id}` возвращает HTML с `self.__next_f.push(...)`
   — внутри сериализованное состояние чата: последняя страница сообщений,
   курсор пагинации, `session.publicAccessToken`. Парсер `unpack_rsc()` +
   `parse_transcript()` разворачивает ссылки-плейсхолдеры (`$L1f`) в объекты.
6. **История.** `GET /api/history/unified?cursor&limit&includeArchived&type`
   (limit ≤ 50) — список чатов; `GET /api/chat/{id}/messages?cursor&limit` —
   более ранние сообщения (пагинация назад).

### Жизненный цикл сообщения

```
POST /nextjs-api/stream/create-chat      → {id}                       (новый чат)
POST /api/chat/trigger-token {sessionId} → {token}
POST /ai-proxy/realtime/v1/sessions/{id}/in/append                    (сообщение / stop)
     Authorization: Bearer <token>, x-trigger-source: sdk, x-part-id: <uuid>
     {kind:"message", payload:{message:{id,role:"user",parts:[...]},
                               chatId, trigger:"submit-message", messageId, metadata}}
GET  /ai-proxy/realtime/v1/sessions/{id}/out                          (SSE-поток ответа)
GET  /api/chat/{id}/messages?cursor=&limit=                           (готовый транскрипт)
GET  /api/chat/{id}/workspace/latest?includeManifest=true             (файлы песочницы)
```

`POST /api/chat` в агент-режиме **не работает** (403 «Route not allowed»).

Готовность хода определяется так (`wait_idle`): последнее сообщение — `assistant`,
у него нет `metadata.pending`, все части в финальном состоянии
(`done` / `output-available` / `output-error`), и размер не меняется два опроса подряд.

## 4. REST API (`arena-api`)

Запуск: `sudo systemctl start arena-api` (порт `127.0.0.1:8790`),
установка — `bash arena_service/install.sh`, swagger — `http://127.0.0.1:8790/docs`.

Авторизация: заголовок `X-API-Key: $(cat /opt/orchestrator/.secrets/arena_service_token.txt)`.
Отключается только для отладки: `ARENA_API_AUTH=0`. Наружу не публиковать:
`ARENA_API_HOST` по умолчанию `127.0.0.1`; если нужен доступ с других машин —
ставьте `0.0.0.0` только за VPN/файрволом.

```bash
TOKEN=$(cat /opt/orchestrator/.secrets/arena_service_token.txt); H="X-API-Key: $TOKEN"
curl -s -H "$H" localhost:8790/health
```

| Метод и путь | Что делает |
|---|---|
| `GET /health` | состояние моста: жива ли вкладка, аккаунт, сколько чатов в индексе/скачано, идёт ли экспорт |
| `GET /me` `/pulse` `/balance` | профиль, квота, кредиты (`creditsRemaining`) |
| `GET /models` | список моделей Agent Mode: `{available, status, models, error}` (пока `available:false`, см. раздел 11) |
| `GET /models/catalog?only&selectable&limit` | каталог всех моделей площадки (1074 записи с UUID) из `initialModels` лидерборда |
| `GET /flags?only=agent` | feature-флаги аккаунта из `posthogFlags` страницы `/agent` |
| `GET /api-map` | карта всех 116 эндпоинтов арены |
| `GET /chats?limit&cursor&include_archived&type&source=live\|cache` | список чатов |
| `GET /chats/search?q&limit` | поиск по чатам |
| `GET /chats/{id}?format=json\|md\|light&source=auto\|live\|cache` | транскрипт |
| `GET /chats/{id}/messages?limit&cursor&light` | страница сообщений |
| `POST /chats` `{text, files?, wait?, timeout?, model_id?, harness_id?, timezone?}` | создать чат и отправить первое сообщение |
| `POST /chats/{id}/messages` `{text, files?, wait?, timeout?, timezone?}` | следующее сообщение |
| `POST /chats/{id}/wait?timeout` | дождаться конца хода и вернуть ответ |
| `GET /chats/{id}/stream?seconds&last_event_id` | живой SSE-поток (события + склеенный текст) |
| `POST /chats/{id}/stop` | остановить генерацию |
| `PATCH /chats/{id}` `{title}` | переименовать (переводы строк заменяются пробелами) |
| `POST /chats/{id}/archive` `/unarchive`, `DELETE /chats/{id}` | архив / удаление |
| `POST /chats/{id}/feedback` `{node_id, text}` или `{node_id, action}` | отзыв / check-in |
| `GET /chats/{id}/files` | манифест песочницы агента |
| `GET /chats/{id}/files/{nodeId}?path` | содержимое файла песочницы |
| `POST /upload?content_type` | загрузить файл в CAS агента (тело = байты) |
| `GET /export`, `POST /export` `{limit, skip_existing, force, include_archived, sleep}` | статус / запуск фоновой выгрузки |

Пример полного цикла «поставил задачу → получил ответ»:

```bash
curl -s -H "$H" -H 'Content-Type: application/json' localhost:8790/chats \
     -d '{"text":"Собери отчёт по X","wait":true,"timeout":900}' | jq '{id,url,reply}'
curl -s -H "$H" -H 'Content-Type: application/json' \
     -d '{"text":"Добавь выводы","wait":true}' localhost:8790/chats/<id>/messages | jq .reply
curl -s -H "$H" localhost:8790/chats/<id>/files | jq '.manifestNodeId'
```

## 5. MCP

`arena_mcp/server.py` — stdio-сервер, тонкий клиент к REST-сервису. Инструменты:
`arena_status, arena_chats, arena_search, arena_chat, arena_create, arena_send,
arena_wait, arena_stop, arena_stream, arena_files, arena_read_file, arena_balance,
arena_rename, arena_archive, arena_delete, arena_export, arena_api_map`.

Подключение в MCP-хост (Claude Code, Cursor, Cline и т.п.):

```json
{"mcpServers":{"arena-ai":{
  "command":"/opt/orchestrator/.venv/bin/python",
  "args":["/opt/orchestrator/arena_mcp/server.py"],
  "env":{"ARENA_API_URL":"http://127.0.0.1:8790",
         "ARENA_API_KEY":"<токен из .secrets/arena_service_token.txt>"}}}}
```

MCP намеренно ходит через REST, а не в CDP: браузер должен иметь одного владельца.

## 6. CLI и выгрузка

```bash
cd /opt/orchestrator
.venv/bin/python arena_agent/arena_ctl.py list --limit 10
.venv/bin/python arena_agent/arena_ctl.py md --id <chat_id> --out /tmp/chat.md
.venv/bin/python arena_agent/arena_ctl.py files --id <chat_id>
.venv/bin/python arena_agent/arena_ctl.py send --id <chat_id> --text "..." --wait
.venv/bin/python arena_agent/arena_ctl.py balance

# выгрузка архива
.venv/bin/python arena_export/export_chats.py --skip-existing          # только новые/изменённые
.venv/bin/python arena_export/export_chats.py --only-meta              # лишь обновить index.json
.venv/bin/python arena_export/export_chats.py --chat-id <id> --force   # перекачать один
bash arena_export/run_export.sh                                        # в фоне, с логом
```

Дедупликация: чат не перекачивается, если `data/arena/chats/<id>.json` уже есть,
в нём есть сообщения и его `updatedAt` совпадает с индексом. Флаг `--skip-existing`
усиливает правило: сохранённое не трогается вообще. `--force` — наоборот, перекачать всё.
Повторный запуск блокируется lock-файлом `data/arena/.export.lock` (снимается сам,
если процесс мёртв).

## 7. Карта API арены

`docs/ARENA_API.md` — 116 маршрутов с источниками (RPC-клиент `hc("/api")`,
литералы, fetch-вызовы, ручные), статусами зондирования и заметками.
Пересобрать:

```bash
.venv/bin/python arena_agent/scan_api.py scan --download    # обновить кэш бандлов и карту
.venv/bin/python arena_agent/scan_api.py probe              # зондировать маршруты из вкладки
```

Статусы и тела при перескане не теряются, ручные маршруты берутся из
`manual_routes.json`, пометки — из `api_notes.json`.

Что ещё нашлось кроме известного: `PATCH /api/history/{type}/{id}` (переименование),
`POST /api/storage/generate-agent-upload-url` (загрузка файлов в CAS),
`GET /api/chat/{id}/preview` (состояние песочницы), `POST /api/chat/{id}/arena-feedback`
и `review-feedback` (отзывы), `GET /api/coding/github/*` (GitHub-интеграция,
подключена: `JoTalbot/makrotest`), `POST /api/coding-agent/sessions` (режим Coding),
`/nextjs-api/stream/*` (rerun, resample, stop — для батл-режимов),
`realtime/v1/runs|batches|streams` (внутренности Trigger.dev).
Не работают: `/api/chat/agent-models` (403 «Not allowed»), `/api/chat/{id}/cost`
(403), `/api/connectors*` (502 — сломан апстрим).

## 8. Ограничения и грабли

- **Cloudflare**: любые запросы мимо вкладки → 429/challenge. Поэтому весь стек
  работает только на машине с этим браузером.
- **Одна вкладка — один владелец.** Параллельные CDP-клиенты ломают друг другу
  контекст (вкладка закрывается/навигируется).
- **Скорость.** Один запрос к арене — 0.5–4 с; чтение транскрипта большого чата —
  до 20 с (2 МБ RSC). Запросы к сервису сериализуются, поэтому при параллельных
  вызовах растёт очередь, а не нагрузка на арену.
- **Ход агента** занимает от 30 с до 10+ минут. Всегда задавать `timeout`, для
  длинных задач — `wait=false` + `/wait` или `/stream`.
- **SSE без `last_event_id`** отдаёт всю историю записей сессии (реплей).
- **Кредиты**: каждый ход агента тратит квоту (1 000 000/день, `GET /balance`).
- **Заголовок чата** не принимает переводы строк и управляющие символы (400).
- **Файлы песочницы** живут пока жива сессия; для архива их надо скачивать сразу
  (`export_chats.py` это делает в `data/arena/files/<chat_id>/`).
- **`agent-models` закрыт** — выбрать модель программно пока нельзя, только
  `model_id`/`harness_id` если они известны.

## 9. Безопасность

- `.secrets/` в `.gitignore`: `arena_auth.json` (access/refresh-токены, куки) и
  `arena_service_token.txt`.
- REST слушает только `127.0.0.1`. При публикации наружу — reverse-proxy с TLS и
  отдельным токеном, иначе любой сможет читать и писать чаты аккаунта.
- Логи экспорта и транскрипты содержат приватные данные (переписку, доступы,
  ключи из чатов) — `data/arena/` не должен попадать в git и в общие бэкапы.
- Токены аккаунта уже светились в логах отладки: при подозрении на утечку —
  ротация (выйти/зайти на arena.ai, обновить `.secrets/arena_auth.json`).

## 10. Обслуживание

```bash
sudo systemctl status arena-api            # сервис REST
sudo journalctl -u arena-api -n 50 -f      # его логи
sudo systemctl restart arena-api           # переподключит вкладку
sudo docker ps | grep chromium             # браузер
sudo docker restart octopus-browser-chromium
curl -s http://127.0.0.1:9222/json/version # CDP жив?
tail -f /opt/orchestrator/logs/arena_export_*.log
```

Если сервис отвечает «вкладка пропала» — он переподключится сам на следующем
запросе; если не помогает — перезапуск сервиса и/или контейнера браузера.
Сессия арены живёт в профиле контейнера; при её истечении нужно заново
залогиниться в `https://arena.ai` внутри `octopus-browser-chromium`
(веб-интерфейс контейнера: `http://127.0.0.1:6080`).


## 11. Выбор модели в Agent Mode и мониторинг флага

**Коротко: выбрать модель пока нельзя — функция закрыта A/B-флагом, который нашему
аккаунту не назначен.** Ниже — что именно проверено (17.09.2026).

Единственный «модельный» маршрут арены — `GET /api/chat/agent-models`:

```js
// схема ответа из бандла app/[locale]/app/agent/page-*.js
{models: [{id: <uuid>, publicName: string, displayName: string|null}]}
```

Он кормит селектор модели в композере, который рендерится только при флаге
`agent-model-selector`; туда же уходит `modelId` в теле `create-chat`:

```js
ev = flag("agent-model-selector") === true
POST /nextjs-api/stream/create-chat
  {..., ...(ev && selected ? {modelId: selected} : {}),
        ...(harnessExperiment ? {harnessId} : {})}
```

Проверка на живом аккаунте:

| Что | Результат |
|---|---|
| `GET /api/chat/agent-models` | `403 {"error":"Not allowed"}` |
| то же + `?productMode=agentic`, + `referer: /agent`, + `x-arena-product-mode` | `403 Not allowed` |
| `POST /api/chat/agent-models` | `403 Route not allowed` |
| `/api/model`, `/api/models`, `/api/chat/models`, `/api/leaderboard*` | `403 Route not allowed` (маршрутов нет) |
| `agent-model-selector` в `posthogFlags` страницы `/agent` | ключ отсутствует (флаг не назначен) |
| `modelId` в `create-chat` | поле принимается, валидируется как UUID: на мусор — `400 ZodError: Invalid uuid, path: ["modelId"]` |
| `modelId`/`modelName`/`harnessId` в 403 выгруженных транскриптах | не встречаются — агент не фиксирует модель |
| `/leaderboard/agent` (1.75 МБ RSC) | только имена моделей (`Claude Fable 5.1 (Max)`, `GPT 6 Astra (Max)`, `Gemini 3.8 Flash (High)`, `Grok 4.5`), внутренних id нет |

### Каталог моделей (найден 17.09.2026)

В RSC-пейлоаде страницы `/leaderboard/agent` лежит массив **`initialModels`** —
полный реестр моделей площадки вместе с внутренними UUID:

```json
{"id":"019c6d29-a30c-7e20-9bd0-6650af926623","organization":"anthropic",
 "provider":"…","publicName":"claude-sonnet-4-6","name":"claude-sonnet-4-6",
 "displayName":"claude-sonnet-4-6",
 "capabilities":{"inputCapabilities":{"text":true,"image":true,"file":true},
                 "outputCapabilities":{"text":true,"web":true}},
 "userSelectable":true,"rank":2,"rankByModality":{"chat":3,"webdev":31}}
```

**1074 записи**, `userSelectable: true` у 948; организации: openai (82),
google (72), alibaba (61), anthropic (39), xai (24), minimax (19), wan (16),
bytedance (13), meta (12), moonshot (11), mistral (9) и ещё ~616 без указания.
Модальности в `rankByModality`: `chat`, `webdev`, `image`, `search`, `video`
— отдельной модальности `agent` там нет, то есть это каталог батл-режимов.

Выгрузка и доступ:

```bash
.venv/bin/python arena_agent/dump_models.py            # → data/arena/models_catalog.json
curl -s -H "X-API-Key: $TOKEN" "localhost:8790/models/catalog?only=claude&selectable=true"
curl -s -H "X-API-Key: $TOKEN" "localhost:8790/models/catalog?only=openai&limit=200"
```

Топ таблицы лидерборда агентов (46 позиций, это то, что реально воюет в Agent
Mode): Claude Fable 5.1 (Max), GPT 6 Astra (Max), Claude Opus 5 (High/Max),
Claude Fable 5 (High), Claude Opus 4.8 (High), GPT 5.6 Sol (xHigh), Kimi K3 (Max),
Claude Sonnet 5 (High), GPT 5.5 (xHigh), Hy4 preview, DeepSeek V4.1 Flash (Max),
Gemini 3.8 Flash (High), GLM 5.2/5.3 (Max), Muse Spark 1.3 (Max), Qwen3.8 Max,
Grok 4.5 / 4.6 (xHigh), Minimax M3, Mistral Medium 3.5, Solar Pro 4 и др.

### Проверка: можно ли выбрать модель (тест записью)

`POST /nextjs-api/stream/create-chat` с `modelId` из каталога:

| modelId | Ответ арены |
|---|---|
| `019c6d29-…` (claude-sonnet-4-6, anthropic) | **403 `{"error":"Not allowed"}`** — чат не создан |
| `019e71ea-…` (gpt-5.5-instant, openai) | **403 `{"error":"Not allowed"}`** — чат не создан |
| `not-a-uuid` | 400 `ZodError: Invalid uuid, path: ["modelId"]` |
| без `modelId` | 200, чат создаётся, агент отвечает |

Вывод: поле существует и валидируется, но доступ к выбору модели закрыт на
стороне сервера тем же флагом `agent-model-selector`. Знание валидных UUID не
помогает — нужна выдача флага на аккаунт. За этим следит `arena-model-watch`.

Побочное наблюдение: несколько `create-chat` подряд (3–4 за минуту) приводят к
challenge Cloudflare (`429 Just a moment...`) — между созданиями чатов нужна
пауза порядка минуты.

Флаги аккаунта целиком видны через `GET /flags` (ключ RSC — `posthogFlags`,
PostHog отдаёт только назначенные флаги, `$undefined` → `null`). Наши
`agent-*`: `agent-leaderboard: true`, `agent-pareto: true`,
`agent-mode-workspace-storage: true`, `agentic-dlp-pii-detection: "treatment-1"`,
`agent-harness-randomization: null`, `agent-mode-connectors: null`,
`agentic-custom-feedback: null`, `agentic-arena-feedback: null`.

### Монитор `arena-model-watch`

Как только арена выдаст флаг (или маршрут начнёт отвечать 200), монитор сообщит
об этом в Telegram и сохранит список моделей — чтобы сразу можно было передавать
`model_id` при создании чата.

```bash
systemctl list-timers arena-model-watch.timer          # каждые 15 минут
sudo journalctl -u arena-model-watch.service -n 20
tail -f logs/model_watch.log

.venv/bin/python arena_service/model_watch.py --once       # одна проверка
.venv/bin/python arena_service/model_watch.py --loop 900   # цикл в foreground
.venv/bin/python arena_service/model_watch.py --self-test  # имитация срабатывания
.venv/bin/python arena_service/model_watch.py --send-test  # проверка доставки
```

Монитор ходит **через REST-сервис** (`GET /models`, `GET /flags`), поэтому не
спорит с ним за вкладку браузера. Алерт срабатывает на переходе
«недоступно → доступно» (и обратно — как предупреждение); повторных
уведомлений нет, состояние хранится в `data/arena/model_watch.json`
(история последних 200 замеров), список моделей — в `data/arena/models_available.json`.
Доставка — теми же реквизитами, что и алерты Hermes:
`/etc/hermes/telegram.env` + `/etc/hermes/telegram.chats.json` (читаются через
`sudo`, либо задаются переменными `ARENA_WATCH_TELEGRAM_TOKEN` /
`ARENA_WATCH_TELEGRAM_CHAT` / `ARENA_WATCH_WEBHOOK`).

Когда доступ появится, создать чат конкретной моделью можно так:

```bash
curl -s -H "X-API-Key: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"text":"задача","model_id":"<id из /models>","wait":true}' \
     localhost:8790/chats
```

## 12. OpenAI-совместимый шлюз (`arena-gateway`)

Полноценный мост arena.ai → OpenAI API: любой клиент (Cursor, OpenAI SDK, `curl`,
LangChain, балансировщик AIOS, Hermes) получает чат/поиск/картинки арены как обычную
OpenAI-модель. Подробности — **`docs/ARENA_GATEWAY.md`**.

* Сервис: `arena-gateway.service` → `http://127.0.0.1:8791` (`0.0.0.0`, Bearer-токен
  `.secrets/arena_gateway_token.txt`; локальные запросы без токена).
* Код: `arena_gateway/{app,engine,models,config,ctl}.py` + `install_integrations.py`.

### Контракт direct-режима (добыт перехватом 17.09.2026)

```
POST https://arena.ai/nextjs-api/stream/create-evaluation
{"id":"<uuid7>","mode":"direct-battle","modality":"chat","modelAId":"<UUID модели>",
 "userMessageId":"<uuid7>","modelAMessageId":"<uuid7>",
 "userMessage":{"content":"...","experimental_attachments":[],"metadata":{}},
 "recaptchaV3Token":"<grecaptcha.enterprise.execute(sitekey,{action:'chat_submit'})>"}
```

Ответ — SSE-поток протокола AI SDK с префиксом слота модели (`a` = модель A, `b` = B):

```
a0:"дельта текста"            ← text-delta (JSON-строка)
ad:{"finishReason":"stop"}    ← finish
```

* Режимы: `direct`, `direct-battle`, `side-by-side`, `battle`. Новый чат создаётся
  только в `direct-battle` (`direct` → 400 «'direct' mode is not allowed when starting
  a new conversation»).
* Модальности: `auto`, `chat`, `webdev`, `search`, `image`, `p2l`, `video`, `audio`.
* Продолжение диалога: `POST /nextjs-api/stream/post-to-evaluation/{sessionId}`
  (то же тело без `mode`).
* Закрытие чата: React Server Action `deleteEvaluationSession` (`POST` на текущий путь
  с заголовком `Next-Action: <id>`); REST `DELETE /api/chat/{id}` для evaluation → 404.
  Все 13 server actions выгружены в `data/arena/server_actions.json`.
* История: `GET /api/history/unified` → записи `type:"evaluation"`, `mode:"direct-battle"`.

### Выбор модели

`modelAId` — UUID из каталога (`data/arena/models_catalog.json`, 1074 записи).
В UI страницы тот же выбор задаётся query-параметром `?model_a=<publicName>`
(неизвестное значение откатывается к дефолту `max`). Шлюз разрешает имя модели так:
UUID → точное `publicName` → псевдоним (`sonnet`, `haiku`, `flux`, `max`, …) →
частичное совпадение (приоритет: проверенные, затем ранг). Суффикс `:search|:image|
:webdev|:video|:chat` переключает модальность (`sonnet:search` → `claude-sonnet-4-6-search`).

### Анти-бот и темп (главная грабля)

* reCAPTCHA **Enterprise** v3, sitekey `6LeTGMcs…`, action `chat_submit`
  (в агент-режиме — `agentic_chat_submit`).
* Отказ v3 → сайт показывает модалку «Security Verification» с чекбоксом v2
  (sitekey `6Le3_cYs…`) и ретраит с `recaptchaV2Token`. У нас v2 сразу эскалирует
  в картинный челлендж (bframe 400×580) — автоматически не решается, поэтому
  `ARENA_GW_V2=false`.
* Серия быстрых запросов даёт штраф: 403 `recaptcha validation failed`
  и 429 c `retry-after` ≈ 1200 с, хотя глобальный счётчик `ratelimit: limit=1800;w=300`
  почти не потрачен. Хватает ≈3 запросов подряд с интервалом 20 с.
* Флаг ставится **на аккаунт и держится десятки минут**: 17.09.2026 спустя 30+ мин
  после последнего запроса 403 получали и мы, и штатный UI сайта. Перезагрузка
  страницы, новая вкладка и новый профиль — штраф НЕ снимают.
* Поэтому шлюз держит темп 45 с (не более 6 за 15 мин), а после отказа уходит в
  штрафной кулдаун 1200 с и быстро отвечает 503 + `Retry-After` вместо выжигающих
  ретраев. Интервал адаптивный: отказ → ×2 (до 900 с), успех → ×0.9 (до 45 с).
  Состояние переживает перезапуск (`data/arena/gateway_state.json`).
* «Очеловечивание» вкладки (движения мыши + микро-скролл каждые 120 с,
  `ARENA_GW_HUMANIZE`) повышает оценку reCAPTCHA Enterprise. Реалистичная
  пропускная способность шлюза — ~10–20 запросов в час.

### Вотчдоги

| Ситуация | Реакция |
|---|---|
| нет токена reCAPTCHA 20 с | ошибка `no_response`/`transport` |
| арена не ответила за 60 с | 504 `no_response`, задание отменяется (`AbortController`) |
| поток молчит 60 с | 504 `stream_stalled` |
| ответ дольше 300 с | 504 `timeout` |
| вкладка пропала / уехала / потеряла `grecaptcha` | вотчдог (каждые 30 с) поднимает новую вкладку и переустанавливает JS-ядро |
| 403 recaptcha / 429 | штрафной кулдаун + замедление темпа |

### Быстрая проверка

```bash
curl -s http://127.0.0.1:8791/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"Привет"}]}'
curl -sN ... -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"до 10"}]}'
curl -s http://127.0.0.1:8791/v1/arena/status | jq '{ok,adaptive_interval_s,cooldown_remaining_s,counters}'
cd /opt/orchestrator/arena_gateway && ../.venv/bin/python ctl.py health --deep
```

### Подключение к Hermes/AIOS

```bash
python3 arena_gateway/install_integrations.py --dry   # показать изменения
python3 arena_gateway/install_integrations.py         # применить (провайдеры arena-*, тир arena)
sudo systemctl restart octopus-aios.service hermes-shim.service
```

В `llm_balancer.py` это `ArenaGatewayProvider("arena-<slug>", "http://127.0.0.1:8791/v1",
"<publicName>", arena_keys, tier="arena", weight=…, timeout=150.0)`, ключ
`ARENA_GATEWAY_KEY` в `/etc/octopus/secrets.env`; в `hermes-models.yaml` —
псевдоним `hermes-arena` (тир `arena`).

Класс `ArenaGatewayProvider` (вставляется тем же скриптом) даёт две защиты:

* `strict_tier = True` — провайдер отвечает **только** на запросы своего тира, иначе
  балансировщик доходит до арены в общем фолбэке и выжигает лимит reCAPTCHA
  (без фильтра за 25 минут было 7 посторонних вызовов). Фильтр в `LLMBalancer.ask()`:
  `if getattr(provider, "strict_tier", False) and provider.tier != target_tier: continue`.
* `is_available()` опрашивает `/health` шлюза (кэш 60 с) — в кулдауне провайдер
  помечается `healthy: false` в `/health` балансировщика.

Цепочка маршрутизации Hermes: `hermes-arena` → шим (`TIER_BY_MODEL`, `VALID_TIERS`
дополнены `"arena"`) → мост AIOS `/api/v1/aios/ask` (`tier` → `task_type`) →
балансировщик (тир `arena`). Если арена недоступна, ответ приходит с фолбэка,
а шим показывает расхождение: `{"tier":"arena","provider":"groq-gpt-oss-20b","provider_tier":"fast"}`.
