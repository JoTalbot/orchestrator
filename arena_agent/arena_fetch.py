#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_fetch.py — HTTP-запросы к arena.ai ИЗНУТРИ живой вкладки (CDP fetch).

Cloudflare (cf_clearance + reCAPTCHA Enterprise) режет прямой HTTP с
дата-центрового IP, поэтому запросы делает сама страница: у неё правильные
куки, TLS-отпечаток и origin.

Использование:
  arena_fetch.py GET  '/api/me'
  arena_fetch.py GET  '/api/history/unified?limit=5&includeArchived=false'
  arena_fetch.py POST '/api/chat' --body '{"messages":[...]}'
  arena_fetch.py POST '/api/chat' --body-file payload.json --stream
"""
import argparse
import asyncio
import json
import sys
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"


def arena_tab():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        tabs = json.loads(r.read().decode())
    for t in tabs:
        if t.get("type") == "page" and "arena.ai" in (t.get("url") or ""):
            return t
    return None


class Tab:
    def __init__(self, ws_url):
        self.ws_url, self.id, self.pending, self.ws = ws_url, 0, {}, None

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
        except Exception:
            pass

    async def cmd(self, method, params=None, timeout=120):
        self.id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.id] = fut
        await self.ws.send(json.dumps({"id": self.id, "method": method,
                                       "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def js(self, expr, timeout=120):
        r = await self.cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
            "timeout": int(timeout * 1000)}, timeout=timeout + 10)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            ed = res["exceptionDetails"]
            raise RuntimeError("JS error: %s" % json.dumps(ed)[:600])
        rr = res.get("result", {})
        if rr.get("subtype") == "error":
            raise RuntimeError("JS: %s" % rr.get("description"))
        return rr.get("value")

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


async def do_fetch(tab, method, path, body=None, headers=None, stream=False):
    """fetch() в контексте страницы; возвращает {status, headers, body}."""
    js_headers = json.dumps(headers or {})
    payload = {
        "method": method,
        "path": path,
        "headers": js_headers,
        "body": body if body is not None else None,
        "stream": stream,
    }
    expr = """
(async (cfg) => {
  const init = {
    method: cfg.method,
    credentials: 'include',
    headers: Object.assign({'accept': 'application/json, text/plain, */*'},
                           JSON.parse(cfg.headers))
  };
  if (cfg.body != null) {
    init.body = cfg.body;
    if (!init.headers['content-type']) init.headers['content-type'] = 'application/json';
  }
  const r = await fetch(cfg.path, init);
  const out = {status: r.status, statusText: r.statusText,
               headers: Object.fromEntries(r.headers.entries())};
  if (!cfg.stream) {
    out.body = await r.text();
    return JSON.stringify(out);
  }
  // потоковый ответ: читаем первые ~200 КБ и закрываем
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let acc = '', chunks = 0;
  while (chunks < 4000) {
    const {done, value} = await reader.read();
    if (done) break;
    acc += dec.decode(value, {stream: true});
    chunks++;
    if (acc.length > 200000) break;
  }
  try { reader.cancel(); } catch (e) {}
  out.body = acc;
  out.chunks = chunks;
  return JSON.stringify(out);
})(%s)
""" % json.dumps(payload)
    raw = await tab.js(expr, timeout=180)
    return json.loads(raw)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("method")
    ap.add_argument("path")
    ap.add_argument("--body")
    ap.add_argument("--body-file")
    ap.add_argument("--header", action="append", default=[])
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--max", type=int, default=4000, help="сколько байт тела печатать")
    a = ap.parse_args()

    t = arena_tab()
    if not t:
        print("НЕТ вкладки arena.ai в CDP :9222")
        sys.exit(1)
    print("# вкладка:", t.get("url"), file=sys.stderr)
    tab = Tab(t["webSocketDebuggerUrl"])
    await tab.connect()
    await tab.cmd("Runtime.enable")

    headers = {}
    for h in a.header:
        k, _, v = h.partition(":")
        headers[k.strip().lower()] = v.strip()

    body = a.body
    if a.body_file:
        body = open(a.body_file).read()

    try:
        res = await do_fetch(tab, a.method.upper(), a.path, body=body,
                             headers=headers, stream=a.stream)
    finally:
        await tab.close()

    print("%s %s -> %s %s" % (a.method.upper(), a.path, res["status"],
                              res.get("statusText", "")))
    print("headers:", json.dumps(res.get("headers", {}), ensure_ascii=False)[:600])
    b = res.get("body", "") or ""
    print("body[%d байт]:" % len(b))
    print(b[:a.max])


if __name__ == "__main__":
    asyncio.run(main())
