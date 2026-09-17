#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_sniff_live.py — пассивный перехват запросов в ЖИВОЙ вкладке arena.ai.

Ничего не кликает и не отправляет: только Network-события + тела ответов.
  arena_sniff_live.py 60 [фильтр-substr]
"""
import asyncio
import json
import sys
import time
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
SKIP = ("datadoghq", "google-analytics", "googletagmanager", "posthog",
        "/rpc/", "gstatic", "recaptcha", "/_next/static", "sentry")


def targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


class Tab:
    def __init__(self, ws_url):
        self.ws_url, self.id, self.pending, self.events, self.ws = \
            ws_url, 0, {}, asyncio.Queue(), None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=256 * 1024 * 1024,
                                           open_timeout=30, ping_interval=30)
        asyncio.create_task(self._listen())

    async def _listen(self):
        try:
            async for m in self.ws:
                d = json.loads(m)
                if "id" in d and d["id"] in self.pending:
                    self.pending.pop(d["id"]).set_result(d)
                elif "method" in d:
                    self.events.put_nowait(d)
        except Exception:
            pass

    async def cmd(self, method, params=None, timeout=90):
        self.id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.id] = fut
        await self.ws.send(json.dumps({"id": self.id, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)


async def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    flt = sys.argv[2] if len(sys.argv) > 2 else "arena.ai/"
    pages = [t for t in targets() if t.get("type") == "page"
             and "arena.ai" in (t.get("url") or "")]
    if not pages:
        print("нет вкладки arena.ai")
        return
    tab = Tab(pages[0]["webSocketDebuggerUrl"])
    await tab.connect()
    print("# вкладка:", pages[0]["url"], file=sys.stderr)
    await tab.cmd("Network.enable")

    reqs, order = {}, []
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            ev = await asyncio.wait_for(tab.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        m, p = ev.get("method"), ev.get("params", {})
        if m == "Network.requestWillBeSent":
            rq = p.get("request", {})
            u = rq.get("url", "")
            if flt not in u or any(s in u for s in SKIP):
                continue
            rid = p.get("requestId")
            reqs[rid] = {"t": round(time.time() - t0, 1), "method": rq.get("method"),
                         "url": u, "post": (rq.get("postData") or "")[:4000]}
            order.append(rid)
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in reqs:
                r = p.get("response", {})
                reqs[rid]["status"] = r.get("status")
                reqs[rid]["mime"] = r.get("mimeType")
                reqs[rid]["len"] = r.get("encodedDataLength")

    # дотягиваем тела JSON-ответов
    for rid in order:
        if reqs[rid].get("mime") not in ("application/json", "text/x-component",
                                         "text/event-stream", None):
            continue
        try:
            r = await tab.cmd("Network.getResponseBody", {"requestId": rid}, timeout=15)
            body = r.get("result", {}).get("body", "")
            reqs[rid]["resp"] = body[:4000]
        except Exception as e:
            reqs[rid]["resp_err"] = str(e)[:120]

    print(json.dumps([reqs[i] for i in order], ensure_ascii=False, indent=1))


asyncio.run(main())
