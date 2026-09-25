#!/usr/bin/env python3
"""REST API для работы с Arena AI из других агентов, драйверов и внешних процессов.

Архитектура:
    Chrome (:9222) ←CDP→ arena_api.py ← этот сервис (:8790) ← агенты / драйвер / curl / MCP

Один сервис владеет вкладкой браузера, все обращения сериализуются —
поэтому параллельные процессы (в т.ч. экспорт) должны ходить сюда, а не в CDP.

Запуск:
    /opt/orchestrator/.venv/bin/python arena_service/app.py            # 127.0.0.1:8790
    ARENA_API_HOST=0.0.0.0 ARENA_API_PORT=8790 python arena_service/app.py
    systemctl --user start arena-api        # как демон (см. arena-api.service)

Авторизация: заголовок `X-API-Key: <токен>`. Токен лежит в
`.secrets/arena_service_token.txt` (создаётся при первом запуске, chmod 600).
Для локальных процессов можно отключить проверкой ARENA_API_AUTH=0.

Примеры:
    TOKEN=$(cat /opt/orchestrator/.secrets/arena_service_token.txt)
    curl -s -H "X-API-Key: $TOKEN" localhost:8790/health
    curl -s -H "X-API-Key: $TOKEN" "localhost:8790/chats?limit=5"
    curl -s -H "X-API-Key: $TOKEN" -H 'Content-Type: application/json' \\
         -d '{"text":"Привет, назови себя"}' localhost:8790/chats
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "arena_agent"))

from arena_api import (  # noqa: E402
    ORIGIN, ArenaAPI, connect, light_message, transcript_text, wait_idle,
)

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402

DATA_DIR = Path(os.getenv("ARENA_DATA_DIR", str(ROOT / "data" / "arena")))
TOKEN_FILE = ROOT / ".secrets" / "arena_service_token.txt"
HOST = os.getenv("ARENA_API_HOST", "127.0.0.1")
PORT = int(os.getenv("ARENA_API_PORT", "8790"))
AUTH = os.getenv("ARENA_API_AUTH", "1") != "0"
VERSION = "1.1"
TZ = os.getenv("ARENA_TZ", "Europe/Kiev")

_lock = asyncio.Lock()          # доступ к браузеру — строго по одному
_state: dict = {"tab": None, "api": None, "started": time.time(),
                "requests": 0, "export": None}


def api_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t)
    os.chmod(TOKEN_FILE, 0o600)
    return t


TOKEN = api_token()


async def get_api() -> ArenaAPI:
    """Живая вкладка Arena AI (переподключаемся, если она умерла)."""
    api = _state["api"]
    if api is not None:
        try:
            await api.fetch_json("GET", "/api/me", retries=1)
            return api
        except Exception:
            _state["api"] = None
            # ВАЖНО: tab.close() закрывает только websocket. Вкладку в браузере
            # нужно закрывать отдельно, иначе каждая проверка здоровья плодит
            # по вкладке arena.ai/agent (найдено 7 одинаковых).
            await _close_tab(_state["tab"])
            _state["tab"] = None
    last = None
    for attempt in range(3):
        tab = None
        try:
            tab = await connect(own=True, verbose=False)
            api = ArenaAPI(tab, verbose=False, own_tab=True)
            _state["tab"], _state["api"] = tab, api
            return api
        except Exception as e:            # вкладка могла зависнуть на challenge
            last = e
            _state["tab"], _state["api"] = None, None
            # вкладка уже создана в браузере — закрываем, иначе утекает
            # по одной на каждую попытку (до трёх за цикл).
            await _close_tab(tab)
            await asyncio.sleep(4 + 4 * attempt)
    raise RuntimeError("не удалось подключиться к браузеру: %s" % last)


async def _close_tab(tab) -> None:
    """Закрыть и websocket, и саму вкладку в браузере.

    Без второго шага вкладка остаётся жить в host-chrome-proxy, а с ней
    renderer и worker. Именно так в браузере накопились 7 одинаковых
    arena.ai/agent при одном работающем сервисе.
    """
    if tab is None:
        return
    try:
        await tab.close()
    except Exception:
        pass
    target_id = getattr(tab, "target_id", None)
    if not target_id:
        return
    try:
        urllib.request.urlopen("http://127.0.0.1:9222/json/close/" + target_id, timeout=10)
    except Exception:
        pass


@asynccontextmanager
async def browser():
    """Эксклюзивный доступ к браузеру."""
    async with _lock:
        _state["requests"] += 1
        yield await get_api()


def check_auth(x_api_key: Optional[str]):
    if not AUTH:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, TOKEN):
        raise HTTPException(401, "нужен заголовок X-API-Key "
                                 "(токен: .secrets/arena_service_token.txt)")


app = FastAPI(title="Arena AI API", version=VERSION,
              description="Локальный REST-мост к arena.ai (Agent Mode)")


@app.exception_handler(RuntimeError)
async def on_runtime_error(request: Request, exc: RuntimeError):
    """Ошибки браузера/арены отдаём как 503 с понятным текстом, а не 500."""
    return JSONResponse(status_code=503, content={
        "error": str(exc)[:600],
        "hint": "браузер или сессия arena.ai недоступны: повторите запрос, "
                "при необходимости `sudo systemctl restart arena-api`"})


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "error": "%s: %s" % (type(exc).__name__, str(exc)[:600])})


# ------------------------------------------------------------------- служебное

@app.get("/health")
async def health(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    live, me = "not-connected (подключится при первом запросе)", {}
    if _state["api"] is not None:
        try:
            async with browser() as api:
                me = await api.me()
                live = True
        except Exception as e:
            live = "error: %s" % str(e)[:120]
    idx = DATA_DIR / "index.json"
    return {"ok": True, "version": VERSION, "browser": live,
            "uptimeSec": int(time.time() - _state["started"]),
            "requests": _state["requests"], "queueLocked": _lock.locked(),
            "localExport": {"indexExists": idx.exists(),
                            "chats": len(json.loads(idx.read_text())["entries"])
                            if idx.exists() else 0,
                            "downloaded": len(list((DATA_DIR / "chats").glob("*.json")))
                            if (DATA_DIR / "chats").exists() else 0},
            "account": (lambda u: {"id": u.get("id"),
                                   "username": u.get("username"),
                                   "email": u.get("email"),
                                   "emailProvider": u.get("emailProvider")}
                        )((me or {}).get("user") or {}) if me else None,
            "export": _state["export"]}


@app.get("/token", include_in_schema=False)
async def token():
    """Токен сервиса — только для localhost-администрирования."""
    return PlainTextResponse(TOKEN)


# ------------------------------------------------------------------ аккаунт

@app.get("/me")
async def me(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        return await api.me()


@app.get("/pulse")
async def pulse(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        return await api.fetch_json("GET", "/api/me/pulse")


@app.get("/balance")
async def balance(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        return await api.fetch_json("GET", "/api/billing/balance")


@app.get("/models")
async def models(x_api_key: Optional[str] = Header(None)):
    """Список моделей Agent Mode.

    Маршрут арены закрыт фичефлагом `agent-model-selector`, поэтому обычно
    возвращает 403 «Not allowed». Ответ всегда 200 и структурирован — удобно
    мониторить появление доступа (см. arena_service/model_watch.py).
    """
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.agent_models()
        if r["status"] >= 400:
            return {"available": False, "status": r["status"], "models": None,
                    "error": (r["body"] or "")[:200]}
        try:
            data = json.loads(r["body"])
        except Exception:
            data = {}
        return {"available": True, "status": r["status"],
                "models": data.get("models") or data, "error": None}


@app.get("/flags")
async def flags(x_api_key: Optional[str] = Header(None),
                only: Optional[str] = Query(None,
                                            help="фильтр по подстроке в названии флага")):
    """Feature-флаги аккаунта (из RSC страницы /agent).

    Ключевые для нас: `agent-model-selector` (выбор модели),
    `agent-harness-randomization` (харнес агента).
    """
    check_auth(x_api_key)
    async with browser() as api:
        f = await api.feature_flags()
    if not f:
        return {"ok": False, "count": 0, "flags": {},
                "note": "флаги не найдены в пейлоаде страницы"}
    if only:
        f = {k: v for k, v in f.items() if only.lower() in k.lower()}
    return {"ok": True, "count": len(f), "flags": f,
            "modelSelector": ("agent-model-selector" in f) and
                             bool(f.get("agent-model-selector"))}


@app.get("/models/catalog")
async def models_catalog(only: Optional[str] = Query(None,
                       help="фильтр по подстроке в publicName/organization"),
        selectable: Optional[bool] = Query(None),
        limit: int = Query(100, le=2000),
        x_api_key: Optional[str] = Header(None)):
    """Каталог моделей arena.ai (1074 записи) — выгружен из `initialModels`
    в RSC страницы /leaderboard/agent, где модели лежат вместе с внутренними UUID.

    Важно: эти id — из каталога батл-режимов (модальности chat/webdev/image/
    search/video). Агент-режим отдаёт свой список через /api/chat/agent-models,
    который закрыт флагом agent-model-selector (403), и передача modelId в
    create-chat тоже запрещена (403 Not allowed) — см. docs/ARENA.md, раздел 11.
    """
    check_auth(x_api_key)
    p = DATA_DIR / "models_catalog.json"
    if not p.exists():
        raise HTTPException(404, "models_catalog.json нет — запустите dump_models")
    d = json.loads(p.read_text())
    ms = d.get("models") or []
    if only:
        o = only.lower()
        ms = [m for m in ms if o in (m.get("publicName") or "").lower()
              or o in (m.get("organization") or "").lower()
              or o in (m.get("displayName") or "").lower()]
    if selectable is not None:
        ms = [m for m in ms if bool(m.get("userSelectable")) is selectable]
    return {"source": d.get("source"), "savedAt": d.get("savedAt"),
            "totalInCatalog": len(d.get("models") or []), "count": len(ms[:limit]),
            "models": [{k: m.get(k) for k in
                        ("id", "publicName", "displayName", "organization",
                         "provider", "userSelectable", "rank")} | 
                       {"capabilities": m.get("capabilities")} for m in ms[:limit]]}


@app.get("/api-map")
async def api_map(x_api_key: Optional[str] = Header(None)):
    """Карта всех найденных эндпоинтов arena.ai (scan_api.py)."""
    check_auth(x_api_key)
    p = ROOT / "arena_agent" / "api_map.json"
    if not p.exists():
        raise HTTPException(404, "api_map.json нет — запустите scan_api.py")
    return json.loads(p.read_text())


# ------------------------------------------------------------------- чаты

def read_index():
    p = DATA_DIR / "index.json"
    return json.loads(p.read_text()) if p.exists() else {"entries": []}


@app.get("/chats")
async def chats(limit: int = Query(20, le=200), cursor: Optional[str] = None,
                include_archived: bool = False, type: str = "agentic",
                source: str = Query("live", pattern="^(live|cache)$"),
                x_api_key: Optional[str] = Header(None)):
    """Список чатов. source=cache — из локального экспорта (мгновенно, без браузера)."""
    check_auth(x_api_key)
    if source == "cache":
        e = read_index()["entries"]
        return {"source": "cache", "count": len(e[:limit]), "chats": e[:limit]}
    async with browser() as api:
        d = await api.history(cursor=cursor, limit=min(limit, 50),
                              include_archived=include_archived, type=type)
        items = d.get("entries") or []
        pg = d.get("pagination") or {}
        return {"source": "live", "count": len(items),
                "nextCursor": pg.get("cursor") if pg.get("hasMore") else None,
                "chats": [{"id": c["id"], "title": c.get("title"),
                           "type": c.get("type"), "updatedAt": c.get("updatedAt"),
                           "createdAt": c.get("createdAt"),
                           "productMode": c.get("productMode"),
                           "archivedAt": c.get("archivedAt")} for c in items]}


@app.get("/chats/search")
async def chat_search(q: str, limit: int = Query(20, le=200),
                      include_archived: bool = False,
                      x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.fetch_json("GET", "/api/history/search",
                                 {"q": q, "limit": limit,
                                  "includeArchived": str(include_archived).lower()})
        return r


@app.post("/chats")
async def chat_create(payload: dict = Body(...),
                      x_api_key: Optional[str] = Header(None)):
    """Создать чат агент-режима и отправить первое сообщение.

    payload: {text, files?: [{filename, mediaType, content|contentB64}],
              timezone?, model_id?, harness_id?, wait?: bool, timeout?: сек}
    """
    check_auth(x_api_key)
    text = payload.get("text")
    if not text:
        raise HTTPException(400, "нужно поле text")
    wait = bool(payload.get("wait"))
    timeout = int(payload.get("timeout") or 900)
    async with browser() as api:
        files = await _upload_parts(api, payload.get("files") or [])
        r = await api.create_chat(text, files=files or None,
                                  timezone=payload.get("timezone") or TZ,
                                  model_id=payload.get("model_id"),
                                  harness_id=payload.get("harness_id"))
        cid = r.get("id") or r.get("chatId")
        if not cid:
            raise HTTPException(502, "арена не вернула id чата: %s" % str(r)[:300])
        out = {"id": cid, "url": "%s/agent/%s" % (ORIGIN, cid)}
    if wait:
        tr = await wait_reply(cid, timeout)
        out["reply"] = transcript_text(tr, only_last=True)
        out["messages"] = [light_message(m) for m in (tr.get("messages") or [])]
    return out


async def _upload_parts(api, files):
    """[{filename, mediaType, content|contentB64}] → parts для create/send."""
    import base64
    out = []
    for f in files:
        data = f.get("content")
        if data is None and f.get("contentB64"):
            data = base64.b64decode(f["contentB64"])
        if data is None:
            continue
        up = await api.upload(data, f.get("mediaType") or "text/plain")
        out.append({"url": up["url"], "key": up["key"], "hash": up["hash"],
                    "size": up["size"], "mediaType": up["mediaType"],
                    "filename": f.get("filename") or "file"})
    return out


async def wait_reply(chat_id: str, timeout: int):
    """Ждём ответ агента, захватывая браузер только на время каждого опроса.

    Иначе один долгий ход агента (минуты) держал бы всю очередь сервиса.
    """
    async def poll(cid):
        async with browser() as api:
            return (await api.transcript_latest(cid))[0] or {}

    return await wait_idle(None, chat_id, timeout=timeout, interval=6, poll=poll)


@app.get("/chats/{chat_id}")
async def chat_get(chat_id: str,
                   format: str = Query("json", pattern="^(json|md|light)$"),
                   source: str = Query("auto", pattern="^(auto|live|cache)$"),
                   x_api_key: Optional[str] = Header(None)):
    """Полный транскрипт чата.

    source=auto — сначала локальный кэш экспорта (если чат не менялся), иначе live.
    format=light — сообщения без reasoning/tool-деталей (компактно).
    format=md — разговор в markdown.
    """
    check_auth(x_api_key)
    cached = DATA_DIR / "chats" / ("%s.json" % chat_id)
    idx = {e["id"]: e for e in read_index()["entries"]}
    if source in ("auto", "cache") and cached.exists():
        d = json.loads(cached.read_text())
        fresh = idx.get(chat_id, {}).get("updatedAt") == d.get("updatedAt")
        if source == "cache" or fresh:
            return _format_chat(d, format, "cache")
    async with browser() as api:
        tr = await api.transcript_full(chat_id, limit=50)
        msgs = tr.get("messages") or []
        if not msgs:
            raise HTTPException(404, "чат %s не найден или пуст" % chat_id)
        e = idx.get(chat_id) or {}
        d = {"id": chat_id, "title": e.get("title"), "type": e.get("type") or "agentic",
             "createdAt": e.get("createdAt"), "updatedAt": e.get("updatedAt"),
             "messageCount": len(msgs), "messages": msgs,
             "session": tr.get("session"),
             "url": "%s/agent/%s" % (ORIGIN, chat_id)}
        return _format_chat(d, format, "live")


def _format_chat(d: dict, format: str, source: str):
    if format == "md":
        return PlainTextResponse(markdown_of(d))
    msgs = d.get("messages") or []
    if format == "light":
        return {"source": source, "id": d.get("id"), "title": d.get("title"),
                "createdAt": d.get("createdAt"), "updatedAt": d.get("updatedAt"),
                "url": d.get("url") or ("%s/agent/%s" % (ORIGIN, d.get("id"))),
                "messageCount": len(msgs),
                "messages": [light_message(m) for m in msgs]}
    return {"source": source, **d}


def markdown_of(d: dict) -> str:
    out = ["# %s" % (d.get("title") or d.get("id")), "",
           "URL: %s" % (d.get("url") or "%s/agent/%s" % (ORIGIN, d.get("id"))), ""]
    for m in d.get("messages") or []:
        role = {"user": "👤 USER", "assistant": "🤖 ASSISTANT"}.get(
            m.get("role"), m.get("role"))
        out.append("---")
        out.append("## %s" % role)
        for p in m.get("parts") or []:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                out.append(p.get("text") or "")
            elif t == "reasoning":
                out.append("> _рассуждение_: " + (p.get("text") or "")[:1000])
            elif t == "tool":
                name = p.get("toolName")
                out.append("```tool %s\n%s\n```"
                           % (name, json.dumps(p.get("input"), ensure_ascii=False)[:1500]))
                if p.get("output") is not None:
                    out.append("```output\n%s\n```"
                               % str(p.get("output"))[:1500])
            elif t == "file":
                out.append("📎 %s (%s)" % (p.get("filename"), p.get("mediaType")))
        out.append("")
    return "\n".join(out)


@app.get("/chats/{chat_id}/messages")
async def chat_messages(chat_id: str, limit: int = Query(50, le=50),
                        cursor: Optional[str] = None, light: bool = True,
                        x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        out = []
        while len(out) < limit:
            d = await api.messages_page(chat_id, limit=min(limit - len(out), 50),
                                        cursor=cursor)
            out += d.get("messages") or []
            cursor = d.get("nextCursor")
            if not cursor:
                break
        msgs = [light_message(m) if light else m for m in out]
        return {"chatId": chat_id, "count": len(msgs), "nextCursor": cursor,
                "messages": msgs}


@app.post("/chats/{chat_id}/messages")
async def chat_send(chat_id: str, payload: dict = Body(...),
                    x_api_key: Optional[str] = Header(None)):
    """Отправить следующее сообщение. payload: {text, files?, timezone?, wait?, timeout?}"""
    check_auth(x_api_key)
    text = payload.get("text")
    if not text:
        raise HTTPException(400, "нужно поле text")
    wait = bool(payload.get("wait"))
    timeout = int(payload.get("timeout") or 900)
    async with browser() as api:
        files = await _upload_parts(api, payload.get("files") or [])
        r = await api.send_message(chat_id, text,
                                   timezone=payload.get("timezone") or TZ,
                                   files=files or None)
        if r["status"] >= 400:
            raise HTTPException(r["status"], (r["body"] or "")[:400])
        out = {"ok": True, "status": r["status"], "chatId": chat_id,
               "url": "%s/agent/%s" % (ORIGIN, chat_id)}
    if wait:
        tr = await wait_reply(chat_id, timeout)
        out["reply"] = transcript_text(tr, only_last=True)
    return out


@app.get("/chats/{chat_id}/stream")
async def chat_stream(chat_id: str, seconds: int = Query(30, le=300),
                      last_event_id: Optional[str] = None,
                      include_events: bool = Query(False),
                      x_api_key: Optional[str] = Header(None)):
    """Живой поток ответа агента (SSE из Trigger.dev).

    Без last_event_id арена отдаёт всю историю записей сессии — передавайте
    lastEventId из предыдущего ответа, чтобы читать только новое.
    """
    check_auth(x_api_key)
    async with browser() as api:
        s = await api.stream_out(chat_id, max_seconds=seconds,
                                 last_event_id=last_event_id)
        st = api.stream_state(s.get("events") or [])
        ev = st.pop("events")
        return {"chatId": chat_id, "status": s.get("status"),
                "timeout": s.get("timeout"), "error": s.get("error"),
                "lastEventId": s.get("lastEventId"),
                "events": ev if include_events else None, **st}


@app.post("/chats/{chat_id}/wait")
async def chat_wait(chat_id: str, timeout: int = Query(600, le=3600),
                    x_api_key: Optional[str] = Header(None)):
    """Дождаться завершения хода агента и вернуть его ответ."""
    check_auth(x_api_key)
    tr = await wait_reply(chat_id, timeout)
    return {"chatId": chat_id, "reply": transcript_text(tr, only_last=True),
            "messages": [light_message(m) for m in (tr.get("messages") or [])]}


@app.post("/chats/{chat_id}/stop")
async def chat_stop(chat_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.stop(chat_id)
        return {"ok": r["status"] < 400, "status": r["status"],
                "body": (r["body"] or "")[:300]}


@app.patch("/chats/{chat_id}")
async def chat_rename(chat_id: str, payload: dict = Body(...),
                      x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    title = payload.get("title")
    if not title:
        raise HTTPException(400, "нужно поле title")
    async with browser() as api:
        r = await api.rename(chat_id, title, type=payload.get("type") or "agentic")
        if r["status"] >= 400:
            raise HTTPException(r["status"], (r["body"] or "")[:400])
        return json.loads(r["body"])


@app.post("/chats/{chat_id}/archive")
async def chat_archive(chat_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.archive(chat_id)
        return {"ok": r["status"] < 400, "status": r["status"]}


@app.post("/chats/{chat_id}/unarchive")
async def chat_unarchive(chat_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.unarchive(chat_id)
        return {"ok": r["status"] < 400, "status": r["status"]}


@app.delete("/chats/{chat_id}")
async def chat_delete(chat_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.delete(chat_id)
        return {"ok": r["status"] < 400, "status": r["status"]}


@app.post("/chats/{chat_id}/feedback")
async def chat_feedback(chat_id: str, payload: dict = Body(...),
                        x_api_key: Optional[str] = Header(None)):
    """payload: {node_id, text} — отзыв; или {node_id, action} — check-in."""
    check_auth(x_api_key)
    node = payload.get("node_id")
    if not node:
        raise HTTPException(400, "нужно поле node_id (id последнего сообщения)")
    async with browser() as api:
        if payload.get("text") is not None:
            r = await api.feedback(chat_id, node, payload["text"])
        else:
            r = await api.review_feedback(chat_id, node,
                                          action=payload.get("action"),
                                          feedback=payload.get("feedback"))
        return {"ok": r["status"] < 400, "status": r["status"], "body": r["body"]}


# ------------------------------------------------------------------ песочница

@app.get("/chats/{chat_id}/files")
async def chat_files(chat_id: str, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        return await api.workspace_latest(chat_id)


@app.get("/chats/{chat_id}/files/{node_id}")
async def chat_file(chat_id: str, node_id: str, path: Optional[str] = None,
                    x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    async with browser() as api:
        r = await api.workspace_file(chat_id, node_id, path=path)
        if r["status"] >= 400:
            raise HTTPException(r["status"], (r["body"] or "")[:400])
        ct = (r.get("headers") or {}).get("content-type") or "text/plain"
        if ct.startswith("text/") or "json" in ct or "javascript" in ct:
            return PlainTextResponse(r["body"] or "", media_type=ct.split(";")[0])
        import base64
        return JSONResponse({"nodeId": node_id, "contentType": ct,
                             "base64": base64.b64encode(
                                 (r["body"] or "").encode("utf-8", "replace")).decode()})


@app.post("/upload")
async def upload_file(request: Request, content_type: str = "text/plain",
                      x_api_key: Optional[str] = Header(None)):
    """Загрузить файл в CAS агента (тело запроса = байты файла)."""
    check_auth(x_api_key)
    data = await request.body()
    if not data:
        raise HTTPException(400, "пустое тело")
    async with browser() as api:
        return await api.upload(data, content_type)


# ------------------------------------------------------------------- экспорт

@app.get("/export")
async def export_status(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    st = dict(_state["export"] or {})
    chats = DATA_DIR / "chats"
    st["downloaded"] = len(list(chats.glob("*.json"))) if chats.exists() else 0
    st["indexed"] = len(read_index()["entries"])
    if st.get("log"):
        p = Path(st["log"])
        if p.exists():
            st["logTail"] = p.read_text().splitlines()[-15:]
    running = False
    if st.get("pid"):
        try:
            os.kill(int(st["pid"]), 0)
            running = True
        except Exception:
            running = False
    if not running:                            # экспорт мог быть запущен вручную
        try:
            out = subprocess.run(["pgrep", "-f", "export_chats.py"],
                                 capture_output=True, text=True, timeout=5).stdout
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
            pids = [x for x in pids if x != os.getpid()]
            if pids:
                running, st["pid"], st["startedBy"] = True, pids[0], "external"
        except Exception:
            pass
    lock = DATA_DIR / ".export.lock"
    if not running and lock.exists():
        try:                                   # экспорт запущен не через сервис
            lpid = int(json.loads(lock.read_text()).get("pid", 0))
            os.kill(lpid, 0)
            running, st["pid"], st["startedBy"] = True, lpid, "external"
        except Exception:
            pass
    st["running"] = running
    return st


@app.post("/export")
async def export_start(payload: dict = Body(default={}),
                       x_api_key: Optional[str] = Header(None)):
    """Запустить фоновый экспорт чатов в data/arena (отдельный процесс, своя вкладка).

    payload: {limit?, force?, skip_existing?, include_archived?, sleep?}
    """
    check_auth(x_api_key)
    st = _state["export"] or {}
    if st.get("pid"):
        try:
            os.kill(int(st["pid"]), 0)
            raise HTTPException(409, "экспорт уже идёт, PID %s" % st["pid"])
        except ProcessLookupError:
            pass
    ts = time.strftime("%Y%m%d_%H%M%S")
    log = ROOT / "logs" / ("arena_export_%s.log" % ts)
    log.parent.mkdir(exist_ok=True)
    cmd = [str(ROOT / ".venv" / "bin" / "python"),
           str(ROOT / "arena_export" / "export_chats.py")]
    if payload.get("limit"):
        cmd += ["--limit", str(payload["limit"])]
    if payload.get("force"):
        cmd += ["--force"]
    if payload.get("skip_existing", True):
        cmd += ["--skip-existing"]
    if payload.get("include_archived"):
        cmd += ["--include-archived"]
    if payload.get("sleep") is not None:
        cmd += ["--sleep", str(payload["sleep"])]
    with open(log, "w") as f:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True)
    _state["export"] = {"pid": proc.pid, "log": str(log), "cmd": " ".join(cmd),
                        "startedAt": ts}
    return {"ok": True, **_state["export"]}


@app.post("/shutdown", include_in_schema=False)
async def shutdown(x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key)
    if _state["tab"]:
        try:
            await _state["tab"].close()
        except Exception:
            pass
    os._exit(0)


def main():
    import uvicorn
    print("[arena-api] %s:%d  auth=%s  data=%s" % (HOST, PORT, AUTH, DATA_DIR))
    print("[arena-api] токен: %s" % (TOKEN if AUTH else "отключён"))
    print("[arena-api] swagger: http://%s:%d/docs" % (HOST, PORT))
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
