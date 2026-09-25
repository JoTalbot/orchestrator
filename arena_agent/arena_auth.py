#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_auth.py — достаёт сессию arena.ai из живой вкладки CDP.

Пишет в .secrets/arena_auth.json (chmod 600):
  access_token, refresh_token, user, cookies (включая cf_clearance),
  expires_at.
В консоль — только метаданные (сроки, префиксы), сами токены не печатаются.
"""
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
OUT = "/opt/orchestrator/.secrets/arena_auth.json"
AUTH_COOKIE = "arena-auth-prod-v1.0"


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


class Tab:
    def __init__(self, ws_url):
        self.ws_url, self.id, self.pending, self.ws = ws_url, 0, {}, None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024,
                                           open_timeout=30)
        asyncio.create_task(self._listen())

    async def _listen(self):
        try:
            async for m in self.ws:
                d = json.loads(m)
                if "id" in d and d["id"] in self.pending:
                    self.pending.pop(d["id"]).set_result(d)
        except Exception:
            pass

    async def cmd(self, method, params=None, timeout=30):
        self.id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.id] = fut
        await self.ws.send(json.dumps({"id": self.id, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)


AUTH_PREFIX = "arena-auth-prod-v1."


def collect_auth_cookie(cookies: dict) -> str:
    """Кука сессии длинная и разбита приложением на части v1.0, v1.1, ...
    Склеиваем по номеру, у первой убираем префикс 'base64-'."""
    parts = []
    for name, val in cookies.items():
        if name.startswith(AUTH_PREFIX):
            try:
                num = int(name[len(AUTH_PREFIX):])
            except ValueError:
                continue
            parts.append((num, val))
    if not parts:
        return ""
    parts.sort()
    raw = "".join(v for _, v in parts)
    if raw.startswith("base64-"):
        raw = raw[len("base64-"):]
    return raw


def decode_auth(raw: str):
    pad = "=" * (-len(raw) % 4)
    return json.loads(base64.b64decode(raw + pad).decode())


async def main():
    pages = [t for t in targets() if t.get("type") == "page"
             and "arena.ai" in (t.get("url") or "")]
    if not pages:
        print("НЕТ вкладки arena.ai — откройте её в браузере (CDP :9222)")
        sys.exit(1)
    tab = Tab(pages[0]["webSocketDebuggerUrl"])
    await tab.connect()
    ck = await tab.cmd("Network.getAllCookies", {})
    cookies = {c["name"]: c["value"]
               for c in ck.get("result", {}).get("cookies", [])}
    raw = collect_auth_cookie(cookies)
    if not raw:
        print("нет куки %s* — нужно залогиниться на arena.ai" % AUTH_COOKIE)
        sys.exit(2)
    auth = decode_auth(raw)
    data = {
        "saved_at": int(time.time()),
        "access_token": auth.get("access_token"),
        "refresh_token": auth.get("refresh_token"),
        "token_type": auth.get("token_type"),
        "expires_in": auth.get("expires_in"),
        "expires_at": auth.get("expires_at"),
        "user": auth.get("user", {}).get("email"),
        "user_id": auth.get("user", {}).get("id"),
        "cookies": cookies,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(OUT, 0o600)
    now = int(time.time())
    exp = data["expires_at"] or 0
    print("OK ->", OUT)
    print("  user      :", data["user"])
    print("  expires_at:", exp, "(через %d с)" % (exp - now) if exp else "")
    print("  access    :", (data["access_token"] or "")[:12] + "...")
    print("  refresh   :", (data["refresh_token"] or "")[:6] + "...")
    print("  cookies   :", ", ".join(sorted(cookies)))


asyncio.run(main())
