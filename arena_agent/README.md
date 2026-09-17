# Arena Agent — доступ к внутреннему API arena.ai (Agent Mode)

Модуль отвечает на вопрос «можно ли создавать и управлять чатами Arena AI по API,
по принципу скачки чатов ChatGPT». **Можно** — но не напрямую, а через вкладку
браузера. Всё ниже проверено на живом аккаунте 17.09.2026 (407 чатов в истории).

## Почему не «как с ChatGPT»

У ChatGPT получилось ходить в backend-api прямо из Python (`curl_cffi` +
TLS-отпечаток Chrome). С Arena так **не выходит**:

* весь сайт за Cloudflare: прямой запрос с IP дата-центра (Oracle Cloud) даёт
  `429` + `cf-mitigated: challenge` и HTML «Just a moment...» — даже с
  `cf_clearance`, снятым с браузера (cookie привязана к отпечатку TLS);
* на действия стоит **reCAPTCHA Enterprise v3** (sitekey
  `6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0`), токен нужен для создания чата и
  для каждого сообщения.

Поэтому клиент делает запросы **внутри вкладки arena.ai** через CDP
(`Runtime.evaluate` → `fetch`): у страницы правильные куки, origin,
TLS-отпечаток и доступ к `grecaptcha.enterprise`. Браузер — тот же, что
использует `agent_jo` (docker-контейнер `octopus-browser-chromium`, CDP `:9222`).

```
arena_ctl.py / export_chats.py
        └── arena_api.py  ──CDP(ws)──> вкладка arena.ai ──fetch()──> api arena.ai
                                          └── grecaptcha.enterprise.execute()
```

Экспорт открывает **свою** вкладку и выводит её на передний план
(`Page.bringToFront`) — в фоне Cloudflare-челлендж может не решаться вовсе.
Своя вкладка закрывается после работы; чужие вкладки не трогаем.

## Карта API (проверено)

Чтение:

| Метод и путь | Что отдаёт |
|---|---|
| `GET /api/me` | профиль (id, email, supabaseUserId) |
| `GET /api/me/pulse` | «пульс» дня: `{"pulse":100}` |
| `GET /api/billing/balance` | `{"creditsRemaining":1000000,"dailyFreeCredits":1000000}` |
| `GET /api/history/unified?cursor&limit&includeArchived&type` | список всех чатов (курсор-пагинация, `limit<=50`) |
| `GET /api/history/search?q=` | поиск по заголовкам и сообщениям |
| `GET /agent/{id}` | HTML/RSC-пейлоад: **последние ~20 сообщений** + подписанный курсор + `session.publicAccessToken` |
| `GET /api/chat/{id}/messages?cursor&limit` | более ранние сообщения (курсор — только из RSC, иначе `Invalid transcript cursor`) |
| `GET /api/chat/{id}/workspace/latest?includeManifest=true` | дерево файлов песочницы, мелкие файлы — сразу `inlineText` |
| `GET /api/chat/{id}/workspace/{nodeId}` / `?path=` | содержимое файла / каталог |
| `GET /api/chat/{id}/workspace/{nodeId}/download` | скачать файл |
| `GET /api/chat/{id}/preview` | состояние песочницы (порты, процессы) |
| `GET /api/chat/{id}/cost?includeSession=true` | стоимость (у нас вернул `403 Unauthorized`) |

Управление:

| Метод и путь | Что делает |
|---|---|
| `POST /api/chat/{id}/archive` | в архив → `{"id":...,"archivedAt":"..."}` |
| `POST /api/chat/{id}/unarchive` | из архива |
| `DELETE /api/chat/{id}` | удалить чат → `{"deletedAt":"..."}` |
| `POST /api/coding-agent/sessions` | создать coding-сессию (режим Coding) |

Запись (создание чата и сообщения):

| Метод и путь | Что делает |
|---|---|
| `POST /nextjs-api/stream/create-chat` | **новый чат + первое сообщение** → `{"id":"<chat_id>"}` |
| `POST /ai-proxy/realtime/v1/sessions/{id}/in/append` | **следующее сообщение** в чат → `{"ok":true,"seq":N}` |
| тот же путь, chunk `{"kind":"stop"}` | остановить генерацию |
| `GET /ai-proxy/realtime/v1/sessions/{id}/...` (SSE) | живой поток ответа (`text-delta`, `tool-input-delta`, `trigger:turn-complete`) |
| `POST /nextjs-api/stream/stop/{id}/messages/{mid}` | стоп для battle/direct-режимов |

Важно: `POST /api/chat` (Vercel AI SDK) для агент-чата **не работает** —
`403 {"error":"Route not allowed"}`. Агент-режим ездит поверх **Trigger.dev**:
сообщение кладётся во входной поток сессии (`/ai-proxy` = прокси к
`api.trigger.dev`), а ответ читается из SSE-потока той же сессии.

### Тело create-chat

```json
POST /nextjs-api/stream/create-chat
{
  "message": {"id": "<uuidv7>", "role": "user",
              "parts": [{"type": "text", "text": "..."}]},
  "recaptchaV3Token": "<токен действия agentic_chat_submit>",
  "timezone": "Europe/Kiev"
}
→ {"id": "01a0ae4f-539e-71c5-b385-e2a775603189"}
```

Необязательно: `enabledConnectors`, `modelId`, `harnessId`,
`message.metadata.uploads` + части `{"type":"file","url":"data:...","mediaType","filename"}`.

### Тело следующего сообщения (Trigger.dev input chunk)

```json
POST /ai-proxy/realtime/v1/sessions/{chat_id}/in/append
Authorization: Bearer <session.publicAccessToken из RSC-пейлоада чата>
x-trigger-source: sdk
X-Part-Id: <uuid>

{"kind": "message",
 "payload": {"message": {"id":"<uuidv7>","role":"user",
                         "parts":[{"type":"text","text":"..."}],
                         "metadata":{"timezone":"Europe/Kiev",
                                     "submissionSource":"chat_input",
                                     "recaptchaV3Token":"<токен chat_submit>"}},
             "chatId": "<chat_id>",
             "trigger": "submit-message",
             "messageId": "<uuidv7>",
             "metadata": {"timezone":"Europe/Kiev","submissionSource":"chat_input"}}}
→ {"ok": true, "seq": 0}
```

Действия reCAPTCHA, которые шлёт веб-клиент: `agentic_chat_submit`,
`chat_submit`, `chat_retry`, `chat_rerun`, `review_feedback`,
`mutation_feedback`, `pairwise_feedback`, `sign_up`.

## Файлы

* `arena_api.py` — ядро: CDP-вкладка, `fetch` из страницы (с докачкой больших
  тел по кускам), парсер RSC-пейлоада (`self.__next_f` + `$ref`-ссылки),
  reCAPTCHA, все методы API, ретраи на челлендж Cloudflare и на потерю вкладки.
* `arena_ctl.py` — консоль: `list/search/get/md/files/cat/cost/archive/
  unarchive/delete/create/send/stop/wait/me/pulse/balance`.
* `../arena_export/export_chats.py` — массовая выгрузка всех чатов
  (аналог `chatgpt_export`): `data/arena/{index,chats/,light/,errors,summary}`.
* `arena_auth.py` — снять сессию из браузера в `.secrets/arena_auth.json`
  (access/refresh-токены + куки). Нужно только для экспериментов с прямым HTTP —
  основной путь работает и без этого файла.
* Отладка/разведка: `arena_fetch.py` (один запрос из вкладки),
  `arena_sniff.py` (своя вкладка + перехват её запросов),
  `arena_sniff_live.py` (пассивный перехват живой вкладки),
  `arena_scan_js.py` (пути API из JS-бандлов), `arena_capture_ui.py`
  (отправка через UI + перехват реального запроса), `arena_api_test.py`
  (доказательство, что прямой HTTP блокируется Cloudflare),
  `arena_rsc_probe.py`, `arena_probe.py`, `arena_recaptcha_test.py`.

## Команды

```bash
cd /opt/orchestrator

# сколько чатов в аккаунте и что за кредиты
.venv/bin/python arena_agent/arena_ctl.py list
.venv/bin/python arena_agent/arena_ctl.py balance

# скачать транскрипт чата (JSON / Markdown)
.venv/bin/python arena_agent/arena_ctl.py md  <chat_id> --out /tmp/chat.md
.venv/bin/python arena_agent/arena_ctl.py get <chat_id> --out /tmp/chat.json

# файлы песочницы чата и содержимое конкретного
.venv/bin/python arena_agent/arena_ctl.py files <chat_id>
.venv/bin/python arena_agent/arena_ctl.py cat <chat_id> <nodeId> --path sub/dir

# полный экспорт всех чатов (возобновляемый)
.venv/bin/python arena_export/export_chats.py --limit 5      # проба
.venv/bin/python arena_export/export_chats.py                # все 407

# СОЗДАТЬ чат и дождаться ответа
.venv/bin/python arena_agent/arena_ctl.py create "Задача..." --wait --timeout 600

# отправить следующее сообщение в существующий чат
.venv/bin/python arena_agent/arena_ctl.py send <chat_id> "Продолжай" --wait

# остановить генерацию / архив / удалить
.venv/bin/python arena_agent/arena_ctl.py stop <chat_id>
.venv/bin/python arena_agent/arena_ctl.py archive <chat_id>
.venv/bin/python arena_agent/arena_ctl.py delete <chat_id>
```

## Проверенный сценарий (17.09.2026)

```
create "…тест… ответь одним словом: ОК"  → 200, {"id":"01a0ae4f-…"}
                                           агент ответил «ОК»
send   "Ответь одним словом: ПЕРЕХВАТ"    → 200, {"ok":true,"seq":0}
                                           агент ответил «ПЕРЕХВАТ»
archive / unarchive / delete              → 200, чат удалён
export 5 чатов                            → 50 сообщений, 0 ошибок
```

## Ограничения и риски

* API **неофициальное**: Arena может поменять маршруты, схему тела или
  ужесточить reCAPTCHA в любой момент — клиент сломается без предупреждения.
* Свежая вкладка проходит Cloudflare-челлендж 1–3 минуты и только если она на
  переднем плане. Если своя вкладка не поднялась — `--use-current-tab`
  (работа в уже «прогретой» вкладке; она не перезагружается и не navigates).
* Много быстрых запросов с серверного IP ловят челлендж на всю сессию
  (так было 17.09 после прогона `curl_cffi` напрямую) — поэтому в экспорте
  стоит `--sleep` и ретраи. Лимиты сервера: `9000` запросов / 5 мин глобально,
  `60/мин` на `messages`, `200/60` на `pulse`.
* `create` и `send` тратят кредиты аккаунта и создают реальные чаты в истории.
* Ответ агента удобнее не стримить, а поллить транскрипт (`wait`) — SSE-поток
  Trigger.dev требует отдельной подписки и живёт недолго.
* Формально автоматизация веб-интерфейса может не соответствовать Terms of Use
  arena.ai; используем на свой риск и только для своего аккаунта.

## Безопасность

* `data/arena/` и `.secrets/` в `.gitignore` — в выгруженных чатах лежат
  **секреты в открытом виде** (в заголовках и телах чатов Jo вставлял SSH-ключ
  сервера, GitHub PAT, токены Cloudflare). В GitHub это пушить нельзя.
* `.secrets/arena_auth.json` (600) — access/refresh-токены Arena и куки
  `cf_clearance`: по сути полный доступ к аккаунту.
* Все токены, которые светились в чатах/переписке (SSH-ключ OCI, GitHub PAT
  `ghp_…`, Cloudflare), считаются скомпрометированными — их надо ротировать.


## REST-сервис, MCP и полная карта API (добавлено 17.09.2026)

Клиент `arena_api.py` теперь умеет больше, чем описано выше:

| Метод | Что делает |
|---|---|
| `trigger_token(chat_id)` | `publicAccessToken` сессии одним запросом (`POST /api/chat/trigger-token {sessionId}`) — вместо чтения 2 МБ RSC |
| `trigger_session(chat_id)` | старт Trigger.dev-сессии (`{sessionId, timezone}`) |
| `rename(chat_id, title)` | `PATCH /api/history/agentic/{id}` (переводы строк чистятся сами) |
| `upload(data, content_type)` | загрузка файла в CAS: `generate-agent-upload-url` → `PUT` → `{hash, key, url}` |
| `stream_out(chat_id, ...)` | чтение SSE-потока `/ai-proxy/realtime/v1/sessions/{id}/out` |
| `flatten_stream / stream_text / stream_state` | разбор событий потока в текст, рассуждения, инструменты, признак завершения хода |
| `feedback / review_feedback` | отзыв и check-in по ответу агента |
| `wait_idle(..., poll=)` | ожидание ответа; `poll` нужен REST-сервису, чтобы не держать блокировку браузера |

Вложения в сообщение устроены неочевидно: **file-частями становятся только
картинки**, а все загрузки объявляются в `message.metadata.uploads =
[{key, filename, mediaType, kind?}]`. Без этих метаданных сервер отвечает
`400 File parts require validated upload metadata`. Это реализовано в
`_split_files()`.

Над клиентом построены два слоя (подробности — `docs/ARENA.md`):

- `arena_service/app.py` — REST на `127.0.0.1:8790` (systemd-юнит `arena-api`,
  swagger `/docs`, токен в `.secrets/arena_service_token.txt`). Владеет вкладкой
  браузера и сериализует обращения, поэтому наружу и другим агентам следует
  ходить сюда, а не в CDP напрямую.
- `arena_mcp/server.py` — MCP-сервер (stdio) с 17 инструментами поверх REST.

Карта всех эндпоинтов арены (116 маршрутов, найдены разбором 88 JS-бандлов):

```bash
.venv/bin/python arena_agent/scan_api.py scan --download   # пересобрать карту
.venv/bin/python arena_agent/scan_api.py probe             # зондировать из вкладки
```

Результат — `arena_agent/api_map.json` и `docs/ARENA_API.md`. Статусы и тела
при перескане сохраняются, ручные маршруты берутся из `manual_routes.json`,
пояснения — из `api_notes.json`.

Новые команды CLI: `stream <id> [--seconds N] [--last-event-id X]`,
`rename <id> "заголовок"`, `token <id>`, `upload <файл>`.
