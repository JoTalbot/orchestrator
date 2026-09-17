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
| `GET /me` `/pulse` `/balance` `/models` | профиль, квота, кредиты (`creditsRemaining`), список моделей |
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
