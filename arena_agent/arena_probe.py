#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_probe.py — разведка API arena.ai из живой вкладки через CDP.

Не трогает страницу (не кликает, не отправляет): только читает
performance resource-timings, localStorage и куки.
"""
import asyncio
import json
import sys
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
URL_FILTER = "arena.ai"


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


class Tab:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.id = 0
        self.pending = {}
        self.ws = None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024,
                                           open_timeout=30)
        asyncio.create_task(self._listen())

    async def _listen(self):
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                if "id" in data and data["id"] in self.pending:
                    self.pending.pop(data["id"]).set_result(data)
        except Exception:
            pass

    async def cmd(self, method, params=None, timeout=30):
        self.id += 1
        mid = self.id
        fut = asyncio.get_event_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def js(self, expr, timeout=30):
        r = await self.cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
            "timeout": int(timeout * 1000)})
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"__error__": str(res["exceptionDetails"])[:500]}
        return res.get("result", {}).get("value")


async def main():
    tlist = [t for t in targets() if t.get("type") == "page"
             and URL_FILTER in (t.get("url") or "")]
    if not tlist:
        print("нет вкладки arena.ai")
        return
    t = tlist[0]
    print("TAB:", t.get("url"))
    tab = Tab(t["webSocketDebuggerUrl"])
    await tab.connect()
    await tab.cmd("Runtime.enable")
    await tab.cmd("Network.enable")

    out = {}
    out["href"] = await tab.js("location.href")
    out["resources"] = await tab.js(
        "JSON.stringify(performance.getEntriesByType('resource')"
        ".map(e=>e.name).filter(u=>!/\\.(png|jpg|jpeg|gif|svg|woff2?|css|ico)(\\?|$)/.test(u)))")
    out["localStorage_keys"] = await tab.js(
        "JSON.stringify(Object.keys(localStorage))")
    out["sessionStorage_keys"] = await tab.js(
        "JSON.stringify(Object.keys(sessionStorage))")
    out["cookie_header"] = await tab.js("document.cookie")
    ck = await tab.cmd("Network.getCookies", {"urls": ["https://arena.ai/"]})
    out["cookies_all"] = [
        {"name": c["name"], "domain": c["domain"], "httpOnly": c["httpOnly"],
         "secure": c["secure"], "len": len(c.get("value", "")),
         "value_head": c.get("value", "")[:24]}
        for c in ck.get("result", {}).get("cookies", [])]
    # заголовки, которые реально уходят на API (через fetch-обёртку не трогаем,
    # просто посмотрим, есть ли в localStorage токен)
    for k in json.loads(out["localStorage_keys"] or "[]"):
        v = await tab.js("localStorage.getItem(%s)" % json.dumps(k))
        s = str(v)
        out["ls_" + k] = (s[:200] + ("...<len %d>" % len(s) if len(s) > 200 else ""))
    print(json.dumps(out, ensure_ascii=False, indent=1))


asyncio.run(main())
