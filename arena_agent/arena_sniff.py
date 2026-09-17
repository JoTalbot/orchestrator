#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_sniff.py — открывает СВОЮ вкладку arena.ai и пишет все /api/-запросы,
которые делает страница (метод, url, post-data, статус). Свою вкладку закрывает.

  arena_sniff.py 'https://arena.ai/agent/<chat_id>' 40
"""
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"


def http_json(path, method="GET"):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


class Browser:
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

    async def cmd(self, method, params=None, timeout=60):
        self.id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.id] = fut
        await self.ws.send(json.dumps({"id": self.id, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://arena.ai/agent"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 35
    only = sys.argv[3] if len(sys.argv) > 3 else "/api/"

    ver = http_json("/json/version")
    br = Browser(ver["webSocketDebuggerUrl"])
    await br.connect()
    r = await br.cmd("Target.createTarget", {"url": "about:blank"})
    target_id = r["result"]["targetId"]
    print("# открыта вкладка", target_id, file=sys.stderr)
    r = await br.cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    session = r["result"]["sessionId"]
    br.id += 1

    async def scmd(method, params=None, timeout=60):
        br.id += 1
        fut = asyncio.get_event_loop().create_future()
        br.pending[br.id] = fut
        await br.ws.send(json.dumps({"id": br.id, "method": method,
                                     "params": params or {},
                                     "sessionId": session}))
        return await asyncio.wait_for(fut, timeout)

    await scmd("Network.enable")
    await scmd("Page.enable")
    await scmd("Page.navigate", {"url": url})

    reqs = {}
    order = []
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            ev = await asyncio.wait_for(br.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        m = ev.get("method")
        p = ev.get("params", {})
        if m == "Network.requestWillBeSent":
            rq = p.get("request", {})
            u = rq.get("url", "")
            if only and only not in u:
                continue
            rid = p.get("requestId")
            reqs[rid] = {"method": rq.get("method"), "url": u,
                         "post": (rq.get("postData") or "")[:1200],
                         "headers": {k.lower(): v for k, v in
                                     (rq.get("headers") or {}).items()
                                     if k.lower() in ("authorization", "x-recaptcha-token",
                                                      "content-type", "accept", "x-agent-session",
                                                      "x-chat-id", "user-agent")}}
            order.append(rid)
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in reqs:
                reqs[rid]["status"] = p.get("response", {}).get("status")
                reqs[rid]["mime"] = p.get("response", {}).get("mimeType")

    print(json.dumps([reqs[i] for i in order], ensure_ascii=False, indent=1))
    await br.cmd("Target.closeTarget", {"targetId": target_id})
    print("# вкладка закрыта", file=sys.stderr)


asyncio.run(main())
