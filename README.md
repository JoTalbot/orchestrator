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
