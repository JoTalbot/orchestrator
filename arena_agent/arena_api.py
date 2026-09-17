#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_api.py — клиент внутреннего API arena.ai (Agent Mode).

Официального API у Arena нет. Весь веб-интерфейс защищён Cloudflare
(cf_clearance + reCAPTCHA Enterprise), поэтому прямой HTTP с дата-центрового
IP получает 429 «Just a moment...». Запросы делает САМА страница arena.ai
через CDP (Runtime.evaluate + fetch): у неё правильные куки, TLS-отпечаток,
origin и доступ к grecaptcha.enterprise.

Что умеет (проверено на живом аккаунте 17.09.2026):

  чтение
    GET  /api/me                                     профиль
    GET  /api/me/pulse                               «пульс» (лимит/остаток дня)
    GET  /api/billing/balance                        кредиты
    GET  /api/history/unified?cursor&limit&type      список всех чатов
    GET  /api/history/search?q=                      поиск по чатам
    GET  /agent/{id}                                 RSC-пейлоад: последние
                                                     ~20 сообщений + курсор
    GET  /api/chat/{id}/messages?cursor&limit        более ранние сообщения
    GET  /api/chat/{id}/cost?includeSession=true     стоимость чата
    GET  /api/chat/{id}/workspace/latest?includeManifest=true   файлы песочницы
    GET  /api/chat/{id}/workspace/{nodeId}?path=     содержимое файла
    GET  /api/chat/{id}/preview                      состояние песочницы

  управление
    POST   /api/chat/{id}/archive | /unarchive       архив
    DELETE /api/chat/{id}                            удаление чата
    POST   /nextjs-api/stream/stop/{id}              остановить генерацию

  запись (создание чата и сообщения)
    POST /nextjs-api/stream/create-chat              новый чат + первое сообщение
    POST /api/chat                                   следующее сообщение (AI SDK)
    требуется reCAPTCHA v3 токен (действия agentic_chat_submit / chat_submit)
"""
import asyncio
import json
import re
import time
import urllib.parse
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
ORIGIN = "https://arena.ai"
RECAPTCHA_SITEKEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"


# --------------------------------------------------------------------- CDP

def cdp_targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


def find_arena_tab():
    for t in cdp_targets():
        if t.get("type") == "page" and "arena.ai" in (t.get("url") or ""):
            return t
    return None


def open_arena_tab(url=ORIGIN + "/agent"):
    """Открыть вкладку arena.ai (нужна, если в браузере её нет)."""
    req = urllib.request.Request(
        CDP + "/json/new?" + urllib.parse.quote(url, safe=":/?=&"),
        method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


class CDPTab:
    """Минимальный клиент к page-level websocket вкладки."""

    def __init__(self, ws_url, target_id=None):
        self.ws_url, self.target_id = ws_url, target_id
        self.ws = None
        self._id = 0
        self._pending = {}
        self._listener = None
        self.closed = True
        self.events = asyncio.Queue()

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url, max_size=512 * 1024 * 1024, ping_interval=30,
            open_timeout=30, close_timeout=5)
        self.closed = False
        self._listener = asyncio.create_task(self._listen())
        await self.cmd("Runtime.enable")
        return self

    async def _listen(self):
        try:
            async for m in self.ws:
                d = json.loads(m)
                if "id" in d and d["id"] in self._pending:
                    f = self._pending.pop(d["id"])
                    if not f.done():
                        f.set_result(d)
                elif "method" in d:
                    self.events.put_nowait(d)
        except Exception:
            pass
        finally:
            self.closed = True

    async def cmd(self, method, params=None, timeout=180):
        if self.closed or self.ws is None:
            raise RuntimeError("CDP-соединение закрыто")
        self._id += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send(json.dumps({"id": self._id, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def js(self, expr, timeout=180):
        r = await self.cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True},
            timeout=timeout)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError("JS-ошибка: %s"
                               % json.dumps(res["exceptionDetails"],
                                            ensure_ascii=False)[:400])
        out = res.get("result", {})
        if out.get("subtype") == "error":
            raise RuntimeError("JS: %s" % out.get("description"))
        return out.get("value")

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


# ------------------------------------------------------- RSC (flight) парсер

ROW_RE = re.compile(r"(?:^|\n)([0-9a-f]{1,8}):")
_DEC = json.JSONDecoder()


def unpack_rsc(html: str) -> str:
    """Склеить self.__next_f.push([1,"..."]) в один flight-поток."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    return "".join(json.loads('"%s"' % c) for c in chunks)


def parse_transcript(blob: str):
    """Достаёт {messages, pagination, session} из RSC-пейлоада страницы чата."""
    rows = {m.group(1): m.end() for m in ROW_RE.finditer(blob)}

    def resolve(v, depth=0):
        if depth > 8:
            return v
        if isinstance(v, str) and v.startswith("$") and len(v) < 24:
            rid = v[1:]
            if rid in rows:
                try:
                    obj, _ = _DEC.raw_decode(blob, rows[rid])
                except Exception:
                    return v
                return resolve(obj, depth + 1)
            return v
        if isinstance(v, dict):
            return {k: resolve(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [resolve(x, depth + 1) for x in v]
        return v

    best = None
    for m in re.finditer(r'\{"messages":\[', blob):
        try:
            obj, _ = _DEC.raw_decode(blob, m.start())
        except Exception:
            continue
        if isinstance(obj, dict) and "messages" in obj:
            if best is None or len(obj["messages"]) > len(best["messages"]):
                best = obj
    if best is None:
        return None
    return resolve(best)


# ------------------------------------------------------------------- клиент

class TabLost(RuntimeError):
    """Вкладка пропала/перезагрузилась посреди запроса."""


class ArenaAPI:
    """Все вызовы идут через fetch() внутри вкладки arena.ai."""

    def __init__(self, tab: CDPTab, verbose=False, own_tab=False):
        self.tab = tab
        self.verbose = verbose
        self.own_tab = own_tab
        self.calls = 0

    async def reconnect(self):
        """Переподключиться: своя вкладка — поднять заново, чужая — найти живую."""
        try:
            await self.tab.close()
        except Exception:
            pass
        if self.own_tab and self.tab.target_id:
            try:
                urllib.request.urlopen(CDP + "/json/close/" + self.tab.target_id,
                                       timeout=10)
            except Exception:
                pass
        self.tab = await connect(open_if_missing=True, own=self.own_tab,
                                 verbose=self.verbose)

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    async def fetch(self, method, path, body=None, headers=None, timeout=180,
                    retries=4):
        """→ {status, headers, body(text)}

        Cloudflare периодически кидает челлендж (429 + cf-mitigated: challenge,
        HTML «Just a moment...»). Обычно страница проходит его сама за
        несколько секунд — поэтому ждём и повторяем.
        """
        for attempt in range(retries):
            try:
                out = await self._fetch_once(method, path, body, headers, timeout)
            except TabLost as e:
                self.log("  вкладка потеряна (%s) — переподключаюсь" % str(e)[:80])
                await self.reconnect()
                continue
            if not self._is_challenge(out):
                return out
            wait = 8 * (attempt + 1)
            self.log("  cloudflare-челлендж на %s, жду %d с (попытка %d/%d)"
                     % (path[:60], wait, attempt + 1, retries))
            await asyncio.sleep(wait)
        return out

    @staticmethod
    def _is_challenge(out):
        h = out.get("headers") or {}
        body = out.get("body") or ""
        return (h.get("cf-mitigated") == "challenge"
                or out.get("status") in (403, 429, 503)
                and "Just a moment" in body[:400])

    async def _fetch_once(self, method, path, body=None, headers=None,
                          timeout=180):
        """→ {status, headers, body(text)}

        Ответ страницы чата достигает 2+ МБ, а CDP returnByValue на таких
        объёмах ненадёжен — поэтому тело складываем в window.__arenaBuf и
        вычитываем кусками.
        """
        cfg = {"method": method, "path": path,
               "headers": json.dumps(headers or {}),
               "body": body if isinstance(body, str) or body is None
               else json.dumps(body, ensure_ascii=False)}
        expr = """
(async (cfg) => {
  const init = {method: cfg.method, credentials: 'include', cache: 'no-store',
                headers: Object.assign({'accept':'application/json, text/plain, */*',
                                        'cache-control':'no-cache'},
                                       JSON.parse(cfg.headers))};
  if (cfg.body != null) {
    init.body = cfg.body;
    if (!init.headers['content-type']) init.headers['content-type'] = 'application/json';
  }
  const r = await fetch(cfg.path, init);
  const text = await r.text();
  window.__arenaBuf = text;
  return JSON.stringify({status: r.status,
                         headers: Object.fromEntries(r.headers.entries()),
                         len: text.length});
})(%s)
""" % json.dumps(cfg)
        self.calls += 1
        try:
            raw = await self.tab.js(expr, timeout=timeout)
        except RuntimeError as e:
            msg = str(e)
            if ("Failed to parse URL" in msg or "Cannot find context" in msg
                    or "Execution context was destroyed" in msg):
                raise TabLost(msg[:140])
            raise
        if raw is None:
            raise TabLost("вкладка не вернула результат fetch (%s %s)"
                          % (method, path))
        head = json.loads(raw)
        total, parts = head["len"], []
        # позицию храним в JS (code units), иначе эмодзи сбивают смещение
        while True:
            piece = await self.tab.js(
                "(() => { if (window.__arenaPos == null) window.__arenaPos = 0;"
                " const s = window.__arenaBuf.substr(window.__arenaPos, 700000);"
                " window.__arenaPos += s.length; return s; })()", timeout=60)
            if piece is None:
                raise TabLost("вкладка пропала на вычитывании тела (%s)" % path)
            if not piece:
                break
            parts.append(piece)
        pos = await self.tab.js("window.__arenaPos")
        await self.tab.js("window.__arenaPos = null; window.__arenaBuf = null;")
        out = {"status": head["status"], "headers": head["headers"],
               "body": "".join(parts)}
        if (pos or 0) < total:
            self.log("  (!) тело прочитано не полностью: %s/%s code units"
                     % (pos, total))
        self.log("  %s %s -> %s (%d байт)" % (method, path[:80], out["status"],
                                              len(out["body"])))
        return out

    async def fetch_json(self, method, path, body=None, headers=None):
        r = await self.fetch(method, path, body=body, headers=headers)
        if r["status"] >= 400:
            raise RuntimeError("%s %s -> %s: %s" % (method, path, r["status"],
                                                    (r["body"] or "")[:300]))
        try:
            return json.loads(r["body"])
        except Exception:
            return r["body"]

    async def recaptcha(self, action="chat_submit"):
        """Токен reCAPTCHA Enterprise v3 под нужное действие.

        В свежей вкладке скрипт ещё может быть не загружен — догружаем сами
        и ждём grecaptcha.enterprise.ready().
        """
        expr = """
(async (SK) => {
  const load = () => new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + SK;
    s.onload = res; s.onerror = () => rej(new Error('script load failed'));
    document.head.appendChild(s);
  });
  try {
    for (let i = 0; i < 3; i++) {
      if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise
          && typeof grecaptcha.enterprise.execute === 'function') break;
      if (typeof grecaptcha === 'undefined') await load();
      await new Promise(r => setTimeout(r, 1500));
    }
    if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise)
      return JSON.stringify({ok: false, err: 'grecaptcha.enterprise недоступна'});
    if (typeof grecaptcha.enterprise.ready === 'function')
      await new Promise((res) => {
        try { grecaptcha.enterprise.ready(res); } catch (e) { setTimeout(res, 800); }
      });
    const a = %s;
    const t = await grecaptcha.enterprise.execute(SK, {action: a});
    return JSON.stringify({ok: true, token: t, action: a});
  } catch (e) { return JSON.stringify({ok: false, err: String(e)}); }
})(%s)
""" % (json.dumps(action), json.dumps(RECAPTCHA_SITEKEY))
        for attempt in range(3):
            out = json.loads(await self.tab.js(expr, timeout=90) or "{}")
            if out.get("ok"):
                return out
            self.log("  reCAPTCHA: %s (попытка %d)" % (out.get("err"), attempt + 1))
            await asyncio.sleep(3)
        return out

    # ------------------------------------------------------------ чтение

    async def me(self):
        return await self.fetch_json("GET", "/api/me")

    async def pulse(self):
        return await self.fetch_json("GET", "/api/me/pulse")

    async def balance(self):
        return await self.fetch_json("GET", "/api/billing/balance")

    async def history(self, cursor=None, limit=50, include_archived=False,
                      type=None):
        q = ["limit=%d" % min(limit, 50),
             "includeArchived=%s" % ("true" if include_archived else "false")]
        if cursor:
            q.append("cursor=" + urllib.parse.quote(str(cursor)))
        if type:
            q.append("type=" + type)
        return await self.fetch_json("GET", "/api/history/unified?" + "&".join(q))

    async def history_all(self, limit=50, include_archived=False, type=None,
                          max_chats=None):
        out, cursor = [], None
        while True:
            d = await self.history(cursor=cursor, limit=limit,
                                   include_archived=include_archived, type=type)
            out.extend(d.get("entries", []))
            pg = d.get("pagination", {})
            if max_chats and len(out) >= max_chats:
                return out[:max_chats]
            if not pg.get("hasMore") or not pg.get("cursor"):
                return out
            cursor = pg["cursor"]

    async def search(self, q, cursor=None, limit=50, include_archived=False):
        params = ["q=" + urllib.parse.quote(q), "limit=%d" % min(limit, 50),
                  "includeArchived=%s" % ("true" if include_archived else "false")]
        if cursor:
            params.append("cursor=" + urllib.parse.quote(str(cursor)))
        return await self.fetch_json("GET", "/api/history/search?" + "&".join(params))

    async def page_html(self, chat_id):
        r = await self.fetch("GET", "/agent/" + chat_id,
                             headers={"accept": "text/html,application/xhtml+xml"})
        if r["status"] != 200:
            raise RuntimeError("страница чата %s -> %s" % (chat_id, r["status"]))
        return r["body"]

    async def transcript_latest(self, chat_id):
        """Последняя страница сообщений (из RSC) + курсор для более ранних."""
        blob = unpack_rsc(await self.page_html(chat_id))
        return parse_transcript(blob), blob

    async def messages_earlier(self, chat_id, cursor, limit=50):
        return await self.fetch_json(
            "GET", "/api/chat/%s/messages?cursor=%s&limit=%d"
            % (chat_id, urllib.parse.quote(str(cursor)), min(limit, 50)))

    async def transcript_full(self, chat_id, limit=50, max_pages=200):
        """Весь транскрипт: последняя страница + все более ранние."""
        latest, _ = await self.transcript_latest(chat_id)
        if not latest:
            return {"messages": [], "pagination": None, "session": None}
        pages = [latest.get("messages", [])]
        pg = latest.get("pagination") or {}
        cursor = pg.get("cursor")
        n = 0
        while cursor and pg.get("hasMore") and n < max_pages:
            d = await self.messages_earlier(chat_id, cursor, limit=limit)
            msgs = d.get("messages", [])
            if not msgs:
                break
            pages.append(msgs)
            pg = d.get("pagination") or {}
            cursor = pg.get("cursor")
            n += 1
        all_msgs, seen = [], set()
        for page in reversed(pages):          # старые страницы идут первыми
            for m in page:
                if m.get("id") in seen:
                    continue
                seen.add(m.get("id"))
                all_msgs.append(m)
        return {"messages": all_msgs, "pagination": pg,
                "session": latest.get("session"), "pages": len(pages)}

    async def cost(self, chat_id, include_session=True):
        return await self.fetch_json(
            "GET", "/api/chat/%s/cost?includeSession=%s"
            % (chat_id, "true" if include_session else "false"))

    async def workspace_latest(self, chat_id):
        return await self.fetch_json(
            "GET", "/api/chat/%s/workspace/latest?includeManifest=true" % chat_id)

    async def workspace_file(self, chat_id, node_id, path=None):
        p = "/api/chat/%s/workspace/%s" % (chat_id, node_id)
        if path:
            p += "/dir?path=" + urllib.parse.quote(path)
        return await self.fetch("GET", p)

    async def preview(self, chat_id):
        return await self.fetch_json("GET", "/api/chat/%s/preview" % chat_id)

    # -------------------------------------------------------- управление

    async def archive(self, chat_id):
        return await self.fetch("POST", "/api/chat/%s/archive" % chat_id)

    async def unarchive(self, chat_id):
        return await self.fetch("POST", "/api/chat/%s/unarchive" % chat_id)

    async def delete(self, chat_id):
        return await self.fetch("DELETE", "/api/chat/%s" % chat_id)

    # ------------------------------------------------------------- запись

    async def create_chat(self, text, files=None, timezone="Europe/Kiev",
                          model_id=None, harness_id=None, connectors=None):
        """Новый чат Agent Mode. Возвращает {id: <session_id>}.

        Тело — ровно то, что шлёт веб-интерфейс:
        POST /nextjs-api/stream/create-chat
        """
        rc = await self.recaptcha("agentic_chat_submit")
        if not rc.get("ok"):
            raise RuntimeError("reCAPTCHA не выдала токен: %s" % rc)
        parts = []
        for f in (files or []):
            parts.append({"type": "file", "url": f["url"],
                          "mediaType": f["mediaType"], "filename": f["filename"]})
        if text:
            parts.append({"type": "text", "text": text})
        body = {
            "message": {"id": uuid7(), "role": "user", "parts": parts},
            "recaptchaV3Token": rc["token"],
            "timezone": timezone,
        }
        if connectors:
            body["enabledConnectors"] = connectors
        if model_id:
            body["modelId"] = model_id
        if harness_id:
            body["harnessId"] = harness_id
        r = await self.fetch("POST", "/nextjs-api/stream/create-chat", body=body,
                             timeout=240)
        if r["status"] >= 400:
            raise RuntimeError("create-chat -> %s: %s" % (r["status"],
                                                          (r["body"] or "")[:400]))
        try:
            return json.loads(r["body"])
        except Exception:
            return {"raw": r["body"][:500]}

    async def session_token(self, chat_id, transcript=None):
        """publicAccessToken сессии Trigger.dev — берётся из RSC-пейлоада чата."""
        tr = transcript or (await self.transcript_latest(chat_id))[0] or {}
        tok = (tr.get("session") or {}).get("publicAccessToken")
        if not tok:
            raise RuntimeError("не получил publicAccessToken для чата %s" % chat_id)
        return tok, tr

    async def append_input(self, chat_id, chunk, token):
        """POST /ai-proxy/realtime/v1/sessions/{id}/in/append — «вход» агента.

        Ровно то, что делает веб-клиент (TriggerChatTransport.appendInputChunk).
        """
        import uuid as _uuid
        headers = {"authorization": "Bearer " + token,
                   "x-trigger-source": "sdk",
                   "x-part-id": str(_uuid.uuid4())}
        return await self.fetch(
            "POST", "/ai-proxy/realtime/v1/sessions/%s/in/append" % chat_id,
            body=json.dumps(chunk, ensure_ascii=False), headers=headers,
            timeout=120)

    async def send_message(self, chat_id, text, timezone="Europe/Kiev",
                           files=None, token=None):
        """Следующее сообщение в существующий агент-чат.

        Агент-режим НЕ ходит в /api/chat (403 «Route not allowed»): сообщение
        кладётся во входной поток Trigger.dev-сессии чата.
        """
        if token is None:
            token, _ = await self.session_token(chat_id)
        rc = await self.recaptcha("chat_submit")
        mid = uuid7()
        parts = []
        for f in (files or []):
            parts.append({"type": "file", "url": f["url"],
                          "mediaType": f["mediaType"], "filename": f["filename"]})
        parts.append({"type": "text", "text": text})
        meta = {"timezone": timezone, "submissionSource": "chat_input"}
        msg = {"id": mid, "role": "user", "parts": parts,
               "metadata": dict(meta, recaptchaV3Token=rc.get("token"))}
        chunk = {"kind": "message",
                 "payload": {"message": msg, "chatId": chat_id,
                             "trigger": "submit-message", "messageId": mid,
                             "metadata": meta}}
        return await self.append_input(chat_id, chunk, token)

    async def stop(self, chat_id, token=None):
        """Остановить генерацию: во входной поток уходит chunk {kind:"stop"}."""
        if token is None:
            token, _ = await self.session_token(chat_id)
        return await self.append_input(chat_id, {"kind": "stop"}, token)


# ------------------------------------------------------------------- utils

def uuid7():
    """UUIDv7 (как generateSafeUUIDv7 в веб-клиенте: время + случайные биты)."""
    import os
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = bytearray(ms.to_bytes(6, "big") + rand)
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


async def _wait_ready(target_id=None, tries=45, verbose=False):
    """Ждём вкладку arena.ai, которая прошла Cloudflare-челлендж."""
    for i in range(tries):
        for t in cdp_targets():
            if t.get("type") != "page" or "arena.ai" not in (t.get("url") or ""):
                continue
            if target_id and t.get("id") != target_id:
                continue
            title = (t.get("title") or "")
            if "Just a moment" in title:
                if verbose:
                    print("  ...челлендж Cloudflare, жду", flush=True)
                continue
            return t
        await asyncio.sleep(1)
    return None


async def connect(open_if_missing=True, verbose=False, own=False):
    """Подключиться к вкладке arena.ai.

    own=True — открыть СВОЮ вкладку (экспорт не зависит от того, что
    пользователь делает в своей: переключение чатов рвёт контекст страницы).
    """
    t = None
    if own:
        if verbose:
            print("открываю свою вкладку arena.ai", flush=True)
        new = open_arena_tab()
        t = await _wait_ready(new.get("id"), verbose=verbose)
    if not t:
        t = await _wait_ready(verbose=verbose)
    if not t and open_if_missing:
        new = open_arena_tab()
        t = await _wait_ready(new.get("id"), verbose=verbose)
    if not t:
        raise RuntimeError("вкладка arena.ai не найдена/не прошла челлендж (CDP :9222)")
    tab = CDPTab(t["webSocketDebuggerUrl"], t.get("id"))
    await tab.connect()
    if own:
        # в фоновой вкладке Cloudflare-челлендж может не решаться вовсе
        try:
            await tab.cmd("Page.enable")
            await tab.cmd("Page.bringToFront")
        except Exception:
            pass
    # страница должна реально загрузиться И пройти Cloudflare: на about:blank
    # относительный fetch падает, на «Just a moment...» нет grecaptcha и CSP
    # не пускает внешние скрипты.
    href = state = title = None
    for _ in range(120):
        try:
            href = await tab.js("location.href", timeout=15)
            state = await tab.js("document.readyState", timeout=15)
            title = await tab.js("document.title", timeout=15)
        except Exception:
            href = state = title = None
        if (href and href.split("?")[0].startswith(ORIGIN) and state == "complete"
                and "Just a moment" not in (title or "")):
            break
        await asyncio.sleep(1)
    else:
        raise RuntimeError("вкладка arena.ai не готова (href=%s title=%s)"
                           % (href, title))
    if verbose:
        print("вкладка:", href, flush=True)
    return tab


def light_message(m):
    """Выжимка сообщения: роль, текст, инструменты (без больших выводов)."""
    out = {"id": m.get("id"), "role": m.get("role"), "text": [], "tools": []}
    meta = m.get("metadata") or {}
    if meta.get("nodeId"):
        out["nodeId"] = meta["nodeId"]
    for p in m.get("parts") or []:
        t = p.get("type")
        if t == "text":
            out["text"].append(p.get("text") or "")
        elif t == "reasoning":
            out.setdefault("reasoning", []).append((p.get("text") or "")[:2000])
        elif isinstance(t, str) and t.startswith("tool-"):
            inp = json.dumps(p.get("input"), ensure_ascii=False)[:600]
            o = p.get("output")
            out["tools"].append({"tool": t, "state": p.get("state"),
                                 "input": inp,
                                 "output": (json.dumps(o, ensure_ascii=False)[:600]
                                            if o is not None else None)})
    out["text"] = "\n".join(x for x in out["text"] if x)
    return out
