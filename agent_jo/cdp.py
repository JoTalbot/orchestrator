#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минимальный CDP-клиент поверх page-level websocket (обход лимита
browser-level ws: Chromium держит только одно подключение на /devtools/browser)."""

import asyncio
import json
import urllib.request

import websockets


async def http_json(url: str, method: str = "GET", timeout: float = 10):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def sync_http_json(url: str, method: str = "GET", timeout: float = 30):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class CDPTab:
    """Управление одной вкладкой Chrome через page-level CDP websocket."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws = None
        self._id = 0
        self._events = asyncio.Queue()
        self._pending = {}
        self._listener = None

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url, max_size=64 * 1024 * 1024, ping_interval=30,
            open_timeout=30, close_timeout=10)
        self._listener = asyncio.create_task(self._listen())
        return self

    async def _listen(self):
        try:
            async for msg in self.ws:
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if "id" in data and data["id"] in self._pending:
                    fut = self._pending.pop(data["id"])
                    if not fut.done():
                        fut.set_result(data)
                elif "method" in data:
                    self._events.put_nowait(data)
        except Exception:
            pass

    async def cmd(self, method: str, params: dict | None = None,
                  timeout: float = 30):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def wait_event(self, method: str, timeout: float = 60):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"нет события {method}")
            try:
                ev = await asyncio.wait_for(self._events.get(), remaining)
            except asyncio.TimeoutError:
                raise
            if ev.get("method") == method:
                return ev

    async def navigate(self, url: str, timeout: float = 60):
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        ev_task = asyncio.create_task(self.wait_event("Page.loadEventFired", timeout))
        await self.cmd("Page.navigate", {"url": url})
        try:
            await ev_task
        except asyncio.TimeoutError:
            pass  # страница могла грузиться дольше — оценим отдельно

    async def evaluate(self, expression: str, await_promise: bool = False,
                       timeout: float = 60):
        # стрелочную функцию нужно вызвать: "() => ..." -> "(...)()"
        if expression.strip().startswith("() =>"):
            expression = f"({expression})()"
        res = await self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise,
             "returnByValue": True},
            timeout=timeout)
        if "result" in res and "value" in res["result"].get("result", {}):
            return res["result"]["result"].get("value")
        if res.get("result", {}).get("exceptionDetails"):
            raise RuntimeError(
                json.dumps(res["result"]["exceptionDetails"])[:400])
        return None

    async def body_text(self):
        return await self.evaluate("document.body ? document.body.innerText : ''")

    async def close(self):
        try:
            if self._listener:
                self._listener.cancel()
            if self.ws:
                await self.ws.close()
        except Exception:
            pass


class CDPBrowser:
    """Тонкая обёртка над HTTP CDP-эндпоинтом (список вкладок, новые вкладки)."""

    def __init__(self, base: str = "http://127.0.0.1:9224"):
        self.base = base.rstrip("/")

    def tabs(self) -> list[dict]:
        return sync_http_json(f"{self.base}/json/list")

    def new_tab(self, url: str = "about:blank") -> CDPTab:
        import urllib.parse
        import time
        target = f"{self.base}/json/new?{urllib.parse.quote(url, safe='')}"
        last_err = None
        for attempt in range(5):
            try:
                d = sync_http_json(target, method="PUT", timeout=30)
                return CDPTab(d["webSocketDebuggerUrl"])
            except Exception as e:
                last_err = e
                time.sleep(5)
        raise RuntimeError(f"не удалось создать вкладку: {last_err}")

    def version(self) -> dict:
        return sync_http_json(f"{self.base}/json/version")

    def close_tabs(self, fragment: str | None = None,
                   keep: str | None = None, exact: bool = False) -> int:
        """Закрыть вкладки по фрагменту URL (keep — не трогать).

        exact=True — точное совпадение URL (без учёта конечного слеша).
        """
        import urllib.request
        closed = 0
        for t in self.tabs():
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            if keep and keep in url:
                continue
            if fragment is None:
                continue
            match = (url.rstrip("/") == fragment.rstrip("/") if exact
                     else fragment in url)
            if not match:
                continue
            try:
                urllib.request.urlopen(f"{self.base}/json/close/{t['id']}",
                                       timeout=20)
                closed += 1
            except Exception:
                pass
        return closed
