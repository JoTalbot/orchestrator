---
name: arena-ai
description: Работа с arena.ai (Agent Mode) через локальный REST-мост на сервере arm-server-01 — читать историю и транскрипты чатов, ставить задачи агенту арены и ждать ответ, забирать файлы из его песочницы, проверять баланс кредитов, выгружать архив. Применять, когда задача касается arena.ai, её чатов, агента или выгрузки данных оттуда.
---

# Arena AI (Agent Mode)

Публичного API у arena.ai нет: сайт закрыт Cloudflare, прямые HTTP-запросы получают
429/challenge. Весь доступ идёт через браузерную вкладку, которой владеет локальный
REST-мост. Работать нужно **только через мост**.

```
агент → REST :8790 (arena_service/app.py) → CDP :9222 (Chromium в docker) → arena.ai
      → MCP  (arena_mcp/server.py)  ─┘
```

- Сервис: `systemctl status arena-api`, порт `127.0.0.1:8790`, swagger `/docs`
- Токен: `TOKEN=$(cat /opt/orchestrator/.secrets/arena_service_token.txt)`,
  заголовок `X-API-Key: $TOKEN`
- Локальный архив (читается мгновенно, без браузера): `/opt/orchestrator/data/arena/`
  — `index.json` (407 чатов), `chats/<id>.json` (полные), `light/<id>.json` (компактные)
- Карта всех эндпоинтов арены: `/opt/orchestrator/docs/ARENA_API.md` или `GET /api-map`

## Быстрый старт

```bash
TOKEN=$(cat /opt/orchestrator/.secrets/arena_service_token.txt)
H="X-API-Key: $TOKEN"
curl -s -H "$H" localhost:8790/health                 # жив ли мост и аккаунт
curl -s -H "$H" "localhost:8790/chats?limit=10"       # последние чаты
curl -s -H "$H" "localhost:8790/chats/search?q=Cloudflare"
curl -s -H "$H" "localhost:8790/chats/<id>?format=md" # разговор текстом
```

## Маршруты

| Метод и путь | Что делает |
|---|---|
| `GET /health` | состояние моста, аккаунт, прогресс экспорта |
| `GET /me`, `/pulse`, `/balance` | профиль, квота, кредиты |
| `GET /models` | список моделей Agent Mode (`available:false`, пока флаг не выдан) |
| `GET /flags?only=agent` | feature-флаги аккаунта (PostHog) |
| `GET /chats?limit&cursor&source=live\|cache` | список чатов |
| `GET /chats/search?q=` | поиск |
| `GET /chats/{id}?format=json\|md\|light&source=auto\|live\|cache` | транскрипт |
| `GET /chats/{id}/messages?limit&cursor&light` | страница сообщений |
| `POST /chats` `{text, files?, wait?, timeout?, model_id?}` | новый чат + первое сообщение |
| `POST /chats/{id}/messages` `{text, files?, wait?, timeout?}` | следующее сообщение |
| `POST /chats/{id}/wait?timeout=` | дождаться конца хода, вернуть ответ |
| `GET /chats/{id}/stream?seconds&last_event_id` | живой SSE-поток ответа |
| `POST /chats/{id}/stop` | остановить генерацию |
| `PATCH /chats/{id}` `{title}` | переименовать |
| `POST /chats/{id}/archive`, `/unarchive`, `DELETE /chats/{id}` | архив / удаление |
| `POST /chats/{id}/feedback` `{node_id, text\|action}` | отзыв и check-in |
| `GET /chats/{id}/files` | манифест песочницы агента |
| `GET /chats/{id}/files/{nodeId}?path=` | содержимое файла песочницы |
| `POST /upload` (тело = байты) | загрузить файл в CAS агента |
| `GET /export`, `POST /export` `{limit, skip_existing, force}` | статус / запуск выгрузки |
| `GET /api-map` | все найденные эндпоинты арены |

## Рецепты

**Поставить задачу агенту арены и получить ответ**

```bash
curl -s -H "$H" -H 'Content-Type: application/json' localhost:8790/chats \
  -d '{"text":"Сделай X и покажи результат","wait":true,"timeout":900}'
# → {"id":"01a0...","url":"https://arena.ai/agent/01a0...","reply":"..."}
```

Ход агента длится минуты. Для длинных задач шлите `"wait": false`, затем
`POST /chats/{id}/wait?timeout=1800` (или `GET /chats/{id}/stream?seconds=60`).

**Продолжить диалог**

```bash
curl -s -H "$H" -H 'Content-Type: application/json' \
  -d '{"text":"Теперь добавь тесты","wait":true}' \
  localhost:8790/chats/<id>/messages
```

**Забрать файлы из песочницы агента**

```bash
curl -s -H "$H" localhost:8790/chats/<id>/files | jq '.manifest[] | {nodeId,name}'
curl -s -H "$H" "localhost:8790/chats/<id>/files/<nodeId>"
```

**Приложить файл к сообщению**

```bash
curl -s -H "$H" -H 'Content-Type: application/json' -d '{
  "text":"Разбери лог","files":[{"filename":"app.log","mediaType":"text/plain",
  "content":"...содержимое..."}]}' localhost:8790/chats
```

**Обновить архив, не перекачивая сохранённое**

```bash
curl -s -H "$H" -X POST -H 'Content-Type: application/json' \
  -d '{"skip_existing":true}' localhost:8790/export
curl -s -H "$H" localhost:8790/export | jq '{running,downloaded,indexed,logTail}'
```

**Без сервиса (CLI напрямую в браузер)**

```bash
/opt/orchestrator/.venv/bin/python /opt/orchestrator/arena_agent/arena_ctl.py list --limit 5
/opt/orchestrator/.venv/bin/python /opt/orchestrator/arena_agent/arena_ctl.py md --id <chat_id>
/opt/orchestrator/.venv/bin/python /opt/orchestrator/arena_agent/arena_ctl.py send --id <chat_id> --text "..." --wait
```

**MCP-хост (Claude Code, Cursor и т.п.)**

```json
{"mcpServers":{"arena-ai":{
  "command":"/opt/orchestrator/.venv/bin/python",
  "args":["/opt/orchestrator/arena_mcp/server.py"],
  "env":{"ARENA_API_URL":"http://127.0.0.1:8790",
         "ARENA_API_KEY":"<токен из .secrets/arena_service_token.txt>"}}}}
```

Инструменты: `arena_status, arena_chats, arena_search, arena_chat, arena_create,
arena_send, arena_wait, arena_stop, arena_stream, arena_files, arena_read_file,
arena_balance, arena_rename, arena_archive, arena_delete, arena_export, arena_api_map`.

## Правила и грабли

1. **Один владелец браузера.** Сервис держит вкладку и сериализует запросы.
   Не запускайте параллельно другие CDP-клиенты и не плодите экземпляры сервиса.
2. **Не ходить в arena.ai напрямую** (`curl https://arena.ai/api/...`) — будет 429.
3. **Сообщения агенту** идут не в `POST /api/chat` (403 «Route not allowed»), а во
   входной поток Trigger.dev: `POST /ai-proxy/realtime/v1/sessions/{id}/in/append`
   с `Authorization: Bearer <publicAccessToken>`; токен выдаёт
   `POST /api/chat/trigger-token` `{sessionId}`. Это уже делает сервис.
4. **SSE без `last_event_id`** отдаёт всю историю записей сессии — передавайте
   `lastEventId` из прошлого ответа.
5. **Заголовок чата** не может содержать переводы строк (400) — клиент заменяет их пробелами.
6. **Деструктивные действия** (`DELETE /chats/{id}`, `archive`, `stop`) — только по явной
   просьбе пользователя; тесты записи делайте в новом чате.
7. **Кредиты** тратятся на каждый ход агента: перед массовыми прогонами смотреть
   `GET /balance` (`creditsRemaining`) и `GET /pulse`.
8. **Выбор модели** в Agent Mode закрыт флагом `agent-model-selector` (нам не
   выдан): `GET /api/chat/agent-models` → 403. Поле `model_id` в `POST /chats`
   существует и валидируется как UUID, но валидных id моделей взять негде.
   Доступ мониторит `arena-model-watch.timer` (каждые 15 мин) — при включении
   придёт уведомление в Telegram, а список моделей ляжет в
   `data/arena/models_available.json`.
9. Если мост отвечает ошибками вкладки — перезапустить сервис:
   `sudo systemctl restart arena-api`; если умер браузер —
   `sudo docker restart octopus-browser-chromium`.
