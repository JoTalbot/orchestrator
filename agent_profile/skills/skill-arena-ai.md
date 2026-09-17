# Skill: arena-ai

Применять для работы с arena.ai (Agent Mode) из агентов и внутренних процессов: читать историю чатов и транскрипты, ставить задачи агенту арены, дожидаться ответа, забирать файлы из его песочницы, следить за балансом кредитов и выгружать архив чатов. Публичного API у arena.ai нет — весь доступ идёт через локальный мост `/opt/orchestrator/arena_service/app.py` (REST на 127.0.0.1:8790), который управляет вкладкой Chromium в контейнере `octopus-browser-chromium` (CDP 127.0.0.1:9222).

Порядок действий:

Проверить, что мост живой и авторизован: `curl -s -H "X-API-Key: $(cat /opt/orchestrator/.secrets/arena_service_token.txt)" localhost:8790/health`. Если сервиса нет — `sudo systemctl start arena-api` (или `bash /opt/orchestrator/arena_service/install.sh`). Если CDP не отвечает — `sudo docker start octopus-browser-chromium`.

Для чтения уже выгруженного не дёргать арену: локальный архив лежит в `/opt/orchestrator/data/arena/` (`index.json` — 407 чатов с метаданными, `chats/<id>.json` — полные транскрипты, `light/<id>.json` — компактные). Параметр `source=cache` читает оттуда мгновенно.

Найти нужный чат: `GET /chats?limit=20` (или `source=cache`) либо `GET /chats/search?q=...`. Прочитать: `GET /chats/{id}?format=md` — разговор текстом, `format=light` — сообщения без рассуждений и вызовов инструментов, `format=json` — сырой транскрипт.

Поставить задачу агенту: `POST /chats {"text": "...", "wait": true}` — создаст чат и дождётся ответа; `POST /chats/{id}/messages {"text": "...", "wait": true}` — продолжить существующий. Ход агента длится минуты, поэтому при `wait=false` опрашивать готовность через `POST /chats/{id}/wait` или читать живой поток `GET /chats/{id}/stream`.

Забрать результат из песочницы: `GET /chats/{id}/files` (манифест рабочей директории) → `GET /chats/{id}/files/{nodeId}` (содержимое файла).

Завершить: при необходимости `POST /chats/{id}/stop`, `PATCH /chats/{id} {"title": ...}`, `POST /chats/{id}/archive`. Обновить архив — `POST /export {"skip_existing": true}` (уже сохранённые чаты не перекачиваются).

Ключевые команды и паттерны:

Токен сервиса: `TOKEN=$(cat /opt/orchestrator/.secrets/arena_service_token.txt)`, дальше везде заголовок `X-API-Key: $TOKEN`. Swagger со всеми маршрутами: `http://127.0.0.1:8790/docs`.

CLI без сервиса (напрямую в браузер): `/opt/orchestrator/.venv/bin/python /opt/orchestrator/arena_agent/arena_ctl.py me|balance|list|search|get|md|files|cat|create|send|wait|stop|archive|delete`.

MCP-сервер для агентов с поддержкой MCP: `/opt/orchestrator/arena_mcp/server.py` (stdio), инструменты `arena_status, arena_chats, arena_search, arena_chat, arena_create, arena_send, arena_wait, arena_stop, arena_stream, arena_files, arena_read_file, arena_balance, arena_rename, arena_archive, arena_delete, arena_export, arena_api_map`.

Полная карта эндпоинтов арены (105 маршрутов, найдены разбором JS-бандлов): `/opt/orchestrator/docs/ARENA_API.md` и `GET /api-map`.

Экспорт архива: `/opt/orchestrator/.venv/bin/python /opt/orchestrator/arena_export/export_chats.py --skip-existing` (флаги `--limit`, `--chat-id`, `--force`, `--only-meta`, `--include-archived`); лог в `/opt/orchestrator/logs/`.

Выбор модели: `GET /models` вернёт `{available:false, status:403}`, пока аккаунту
не выдан A/B-флаг `agent-model-selector` (проверить флаг: `GET /flags?only=agent`).
Доступ мониторит `arena-model-watch.timer` (каждые 15 минут, лог
`logs/model_watch.log`, алерт в Telegram, список моделей —
`data/arena/models_available.json`). Как только модели появятся — передавать
`model_id` в `POST /chats`.

Частые ошибки:

Ходить в arena.ai прямым HTTP (`curl https://arena.ai/api/...`) — Cloudflare отдаёт 429/challenge. Все запросы обязаны идти через вкладку браузера, то есть через сервис или `arena_api.py`.

Запускать несколько CDP-клиентов одновременно (сервис + экспорт + CLI): они спорят за вкладку и ломают друг другу авторизацию. Экспорт — отдельный процесс со своей вкладкой, но сервис на время экспорта лучше не нагружать тяжёлыми запросами.

Ждать ответ синхронно без таймаута: ход агента занимает от 30 секунд до 10+ минут. Ставить `timeout` явно и при длинных задачах использовать `wait=false` + опрос.

Забывать про `publicAccessToken` при отправке сообщений: агент-режим не принимает `POST /api/chat` (403 «Route not allowed»), сообщение кладётся во входной поток Trigger.dev-сессии `/ai-proxy/realtime/v1/sessions/{id}/in/append`. Токен выдаёт `POST /api/chat/trigger-token`.

Переименовывать чат заголовком с переводами строк — сервер отвечает 400 «Title must not contain control characters» (клиент чистит их сам, но текст меняется).

Читать SSE-поток без `last_event_id` — арена отдаёт всю историю записей сессии, а не только новые события.

Ломать чужие чаты: `DELETE /chats/{id}` и `archive` применять только по явной просьбе пользователя, а тесты записи проводить в новом чате.
