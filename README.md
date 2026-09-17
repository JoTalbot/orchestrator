# Orchestrator

Проект автоматизации. Репозиторий: https://github.com/JoTalbot/orchestrator
Сервер: `/opt/orchestrator/` (OCI, Ubuntu 24.04, arm).

## Часть 1. Сбор всех чатов ChatGPT

Скрипт `chatgpt_export/export_chats.py` выгружает **все чаты** аккаунта ChatGPT
через неофициальный backend-api (тот же, что использует веб-интерфейс
chatgpt.com). Официальный экспорт OpenAI (Settings → Data controls → Export)
тоже возможен, но его нельзя автоматизировать — поэтому используется
backend-api.

Запросы идут через `curl_cffi` с имитацией TLS-отпечатка Chrome: серверные IP
дата-центров (Oracle Cloud и др.) Cloudflare блокирует ответом 403, а с
браузерным TLS-отпечатком API отвечает нормально.

### Что получаем

```
data/
├── chat_list.json   # полный список чатов (метаданные), режим --only-meta
├── chats/           # сырые данные каждого чата (полное дерево mapping)
│   └── {conversation_id}.json
├── light/           # выжимка: только сообщения (role, текст, время)
│   └── {conversation_id}.json
├── index.json       # индекс всех чатов (id, title, create/update_time)
├── errors.json      # чаты, которые не удалось скачать
└── summary.json     # итоговая статистика
```

### Как получить access token

1. Залогиньтесь на https://chatgpt.com в браузере.
2. В том же браузере откройте: https://chatgpt.com/api/auth/session
3. Скопируйте значение поля `accessToken` из ответа JSON.
4. На сервере положите его в файл:

```bash
mkdir -p /opt/orchestrator/.secrets
chmod 600 /opt/orchestrator/.secrets/chatgpt_token.txt
# вставить токен в файл (без пробелов/переносов)
```

Токен живёт несколько дней и автоматически продлевается, пока активна
браузерная сессия. Если скрипт напишет «401» — просто повторите шаги 1–4
и перезапустите экспорт (он продолжится с места остановки).

### Запуск

```bash
cd /opt/orchestrator

# разовая установка
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# пробный запуск на 5 чатах
.venv/bin/python chatgpt_export/export_chats.py --limit 5 --force

# полный экспорт в фоне
bash chatgpt_export/run_export.sh

# следить за прогрессом
tail -f logs/export_*.log
```

Скрипт возобновляемый: уже скачанные чаты пропускаются (сверка по
`update_time`), индекс сохраняется каждые 25 чатов, запись файлов атомарная.

### Безопасность

- `data/` и `.secrets/` — в `.gitignore`: приватные чаты и токены **не**
  попадают в GitHub.
- Права на токен: `chmod 600`.
- Для работы скрипту нужны только чтение ваших чатов через ваш же токен
  сессии; пароль OpenAI не требуется.

## Структура проекта

```
orchestrator/
├── README.md
├── requirements.txt
├── .gitignore
└── chatgpt_export/
    ├── export_chats.py   # основной скрипт экспорта
    ├── verify_data.py    # проверка целостности собранных данных
    └── run_export.sh     # фоновый запуск с логом
```

### Статус первого сбора (14.09.2026)

- Чатов в списке: **274**, скачано: **273** (+1 чат недоступен — им
  поделились или он удалён, API отвечает `conversation_inaccessible`).
- Сообщений суммарно: **47 554**; диапазон дат чатов: 2025-04-30 … 2026-09-13.
- Объём: сырые данные `data/chats/` ≈ 470 МБ, выжимка `data/light/` ≈ 165 МБ.

## Часть 3. Arena AI (Agent Mode) — доступ, API и выгрузка

Полная документация — **`docs/ARENA.md`**, карта всех эндпоинтов арены —
**`docs/ARENA_API.md`** (116 маршрутов), скилл для агентов —
**`skills/arena-ai/SKILL.md`**.

Публичного API у arena.ai нет: прямой HTTP с серверного IP получает
`429 cf-mitigated: challenge` (Cloudflare), а действия защищены reCAPTCHA
Enterprise. Поэтому запросы выполняет вкладка arena.ai в браузере
(CDP `127.0.0.1:9222`, контейнер `octopus-browser-chromium`) через `fetch()`
в контексте страницы.

```
агенты / драйверы / внешние процессы
        │
        ├── REST 127.0.0.1:8790   arena_service/app.py   (systemd: arena-api)
        ├── MCP  (stdio)          arena_mcp/server.py    (17 инструментов)
        └── CLI                   arena_agent/arena_ctl.py
                                        │
                          arena_agent/arena_api.py  →  CDP → Chromium → arena.ai
```

Что работает (проверено на живом аккаунте 17.09.2026, 407 чатов в истории):

- чтение: список чатов, поиск, транскрипт с tool-вызовами, файлы песочницы,
  баланс кредитов (`creditsRemaining`), живой SSE-поток ответа агента;
- запись: создание чата (`POST /nextjs-api/stream/create-chat` + reCAPTCHA v3),
  следующее сообщение (`POST /ai-proxy/realtime/v1/sessions/{id}/in/append`
  с `publicAccessToken`), вложение файлов (загрузка в CAS + `metadata.uploads`),
  остановка генерации, переименование, архив, удаление, отзывы;
- выгрузка: `arena_export/export_chats.py` — возобновляемый экспорт всех чатов
  в `data/arena/`; **уже сохранённые чаты не перекачиваются** (сверка по
  `updatedAt`, флаги `--skip-existing` / `--force`, lock от параллельных запусков).

```bash
cd /opt/orchestrator
TOKEN=$(cat .secrets/arena_service_token.txt)
curl -s -H "X-API-Key: $TOKEN" localhost:8790/health
curl -s -H "X-API-Key: $TOKEN" "localhost:8790/chats?limit=10"
curl -s -H "X-API-Key: $TOKEN" "localhost:8790/chats/<id>?format=md"
curl -s -H "X-API-Key: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"text":"задача","wait":true}' localhost:8790/chats

.venv/bin/python arena_agent/arena_ctl.py list                 # то же из консоли
.venv/bin/python arena_export/export_chats.py --skip-existing  # дозагрузка архива
sudo systemctl status arena-api                                # сервис REST
```

Выгруженные чаты лежат в `data/arena/` (в git не попадают): в них секреты в
открытом виде — SSH-ключи, PAT, токены, которые Jo вставлял в чаты.
