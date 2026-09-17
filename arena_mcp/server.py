#!/usr/bin/env python3
"""MCP-сервер «arena-ai» — даёт агентам (Claude Code, Cursor, любой MCP-хост)
инструменты для работы с arena.ai через локальный REST-сервис.

Схема:  MCP-хост → этот сервер (stdio) → arena_service (:8790) → Chrome CDP → arena.ai

Запуск вручную:   /opt/orchestrator/.venv/bin/python arena_mcp/server.py
Подключение в MCP-хост (пример для Claude Code / Cursor):
{
  "mcpServers": {
    "arena-ai": {
      "command": "/opt/orchestrator/.venv/bin/python",
      "args": ["/opt/orchestrator/arena_mcp/server.py"],
      "env": {
        "ARENA_API_URL": "http://127.0.0.1:8790",
        "ARENA_API_KEY": "<токен из .secrets/arena_service_token.txt>"
      }
    }
  }
}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("ARENA_API_URL", "http://127.0.0.1:8790").rstrip("/")
KEY = os.getenv("ARENA_API_KEY") or ""
if not KEY:
    tf = ROOT / ".secrets" / "arena_service_token.txt"
    if tf.exists():
        KEY = tf.read_text().strip()

mcp = MCPServer(
    name="arena-ai",
    title="Arena AI (Agent Mode)",
    version="1.1",
    instructions=(
        "Инструменты для работы с arena.ai Agent Mode: список чатов, чтение "
        "транскриптов, отправка сообщений агенту, остановка генерации, файлы "
        "песочницы, баланс и экспорт. Все запросы идут через локальный сервис "
        "arena_service (:8790), который владеет вкладкой браузера — не запускайте "
        "другие CDP-клиенты параллельно."
    ),
)


def call(method, path, payload=None, params=None, raw=False):
    """HTTP-вызов к локальному REST-сервису."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items()
                                             if v is not None})
    data = None
    headers = {"X-API-Key": KEY, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if isinstance(payload, (bytes, str)) and raw:
        data = payload if isinstance(payload, bytes) else payload.encode()
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode()[:1000]}
    except Exception as e:
        return {"error": str(e),
                "hint": "сервис arena-api не запущен? "
                        "sudo systemctl start arena-api (или "
                        "python arena_service/app.py)"}
    ct = "application/json"
    try:
        return json.loads(body)
    except Exception:
        return {"text": body[:200000]}


# ------------------------------------------------------------------ инструменты

@mcp.tool(description="Состояние моста к arena.ai: живая ли вкладка, аккаунт, "
                      "сколько чатов в индексе и скачано локально, идёт ли экспорт.")
def arena_status() -> dict:
    return call("GET", "/health")


@mcp.tool(description="Список чатов arena.ai. limit до 200; source='cache' — из "
                      "локального экспорта (мгновенно), 'live' — с сервера арены.")
def arena_chats(limit: int = 20, source: str = "live",
                include_archived: bool = False, cursor: str = "") -> dict:
    return call("GET", "/chats", params={"limit": limit, "source": source,
                                         "includeArchived": str(include_archived).lower(),
                                         "cursor": cursor or None})


@mcp.tool(description="Поиск по чатам arena.ai (заголовок и содержимое).")
def arena_search(q: str, limit: int = 20) -> dict:
    return call("GET", "/chats/search", params={"q": q, "limit": limit})


@mcp.tool(description="Прочитать чат arena.ai целиком. format: 'md' — разговор "
                      "текстом (удобно читать), 'light' — сообщения без "
                      "рассуждений/инструментов, 'json' — сырой транскрипт. "
                      "source: 'auto' (кэш, если свежий, иначе live), 'cache', 'live'.")
def arena_chat(chat_id: str, format: str = "md", source: str = "auto") -> dict:
    return call("GET", "/chats/%s" % chat_id,
                params={"format": format, "source": source})


@mcp.tool(description="Создать новый чат Agent Mode и отправить первое сообщение. "
                      "wait=true — дождаться ответа агента (может занять минуты).")
def arena_create(text: str, wait: bool = False, timeout: int = 900,
                 model_id: str = "", timezone: str = "") -> dict:
    p = {"text": text, "wait": wait, "timeout": timeout}
    if model_id:
        p["model_id"] = model_id
    if timezone:
        p["timezone"] = timezone
    return call("POST", "/chats", p)


@mcp.tool(description="Отправить следующее сообщение в существующий чат arena.ai. "
                      "wait=true — дождаться ответа агента.")
def arena_send(chat_id: str, text: str, wait: bool = False,
               timeout: int = 900) -> dict:
    return call("POST", "/chats/%s/messages" % chat_id,
                {"text": text, "wait": wait, "timeout": timeout})


@mcp.tool(description="Дождаться завершения текущего хода агента и вернуть его ответ.")
def arena_wait(chat_id: str, timeout: int = 600) -> dict:
    return call("POST", "/chats/%s/wait" % chat_id, params={"timeout": timeout})


@mcp.tool(description="Остановить генерацию агента в чате.")
def arena_stop(chat_id: str) -> dict:
    return call("POST", "/chats/%s/stop" % chat_id)


@mcp.tool(description="Прочитать живой поток ответа агента (SSE). seconds — сколько "
                      "слушать; last_event_id — продолжать с прошлой позиции, "
                      "иначе арена отдаст всю историю записей сессии.")
def arena_stream(chat_id: str, seconds: int = 30, last_event_id: str = "") -> dict:
    return call("GET", "/chats/%s/stream" % chat_id,
                params={"seconds": seconds, "last_event_id": last_event_id or None})


@mcp.tool(description="Файлы песочницы чата (манифест рабочей директории агента).")
def arena_files(chat_id: str) -> dict:
    return call("GET", "/chats/%s/files" % chat_id)


@mcp.tool(description="Прочитать содержимое файла из песочницы чата по его nodeId "
                      "(nodeId берётся из arena_files); path — для подкаталога.")
def arena_read_file(chat_id: str, node_id: str, path: str = "") -> dict:
    return call("GET", "/chats/%s/files/%s" % (chat_id, node_id),
                params={"path": path or None})


@mcp.tool(description="Баланс кредитов arena.ai.")
def arena_balance() -> dict:
    return call("GET", "/balance")


@mcp.tool(description="Переименовать чат (переводы строк в заголовке сервер не "
                      "принимает — будут заменены пробелами).")
def arena_rename(chat_id: str, title: str) -> dict:
    return call("PATCH", "/chats/%s" % chat_id, {"title": title})


@mcp.tool(description="Архивировать чат (unarchive=true — вернуть из архива).")
def arena_archive(chat_id: str, unarchive: bool = False) -> dict:
    return call("POST", "/chats/%s/%s" % (chat_id,
                                          "unarchive" if unarchive else "archive"))


@mcp.tool(description="УДАЛИТЬ чат безвозвратно. Только по явной просьбе пользователя.")
def arena_delete(chat_id: str) -> dict:
    return call("DELETE", "/chats/%s" % chat_id)


@mcp.tool(description="Экспорт чатов в data/arena. start=true — запустить фоновый "
                      "экспорт (limit, skip_existing, force, include_archived); "
                      "start=false — показать статус и хвост лога.")
def arena_export(start: bool = False, limit: int = 0, skip_existing: bool = True,
                 force: bool = False, include_archived: bool = False) -> dict:
    if not start:
        return call("GET", "/export")
    p = {"skip_existing": skip_existing, "force": force,
         "include_archived": include_archived}
    if limit:
        p["limit"] = limit
    return call("POST", "/export", p)


@mcp.tool(description="Карта всех найденных эндпоинтов arena.ai (105 маршрутов из "
                      "JS-бандлов и RPC-клиента, со статусами проверки).")
def arena_api_map() -> dict:
    return call("GET", "/api-map")


def main():
    if not KEY:
        print("[arena-mcp] предупреждение: нет ARENA_API_KEY — сервис может "
              "отвечать 401", file=sys.stderr)
    print("[arena-mcp] стартую, REST: %s" % BASE, file=sys.stderr)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
