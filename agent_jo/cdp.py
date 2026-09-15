#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Минимальный CDP-клиент поверх page-level websocket (обход лимита
browser-level ws: Chromium держит только одно подключение на /devtools/browser).

v2 — устойчивость к обрывам. Соединение с Chrome рвётся в любой момент:
перезапуск Chromium (в контейнере liza-browser есть watchdog), тяжёлая
страница, обрыв через socat-прокси :9222 -> :9223. Раньше это давало вечный
dead-loop: цикл падал на мёртвом соединении, не переподключался и продолжал
падать каждые 3 минуты.

Теперь:
  * CDPTab отслеживает состояние соединения (self.closed / last_error);
  * любая команда на закрытом соединении даёт CDPConnectionLost;
  * CDPBrowser умеет найти вкладку по id, открыть/закрыть конкретную вкладку —
    чтобы драйвер переиспользовал СВОЮ вкладку, а не плодил новые.
"""

import asyncio
import json
import time
import urllib.parse
import urllib.request

import websockets


class CDPConnectionLost(RuntimeError):
    """Websocket-соединение с таргетом разорвано — нужен reconnect."""


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

    def __init__(self, ws_url: str, target_id: str | None = None):
        self.ws_url = ws_url
        self.target_id = target_id
        self.ws = None
        self.closed = True
        self.last_error = None
        self.connects = 0
        self._id = 0
        self._events = asyncio.Queue()
        self._pending = {}
        self._listener = None

    # ------------------------------------------------------------ состояние

    @property
    def is_open(self) -> bool:
        """Соединение живо и слушатель работает."""
        if self.ws is None or self.closed or self.ws is None:
            return False
        if self._listener is None or self._listener.done():
            return False
        try:
            state = getattr(self.ws, "state", None)
            if state is not None and str(state).endswith("CLOSED"):
                return False
        except Exception:
            pass
        return True

    # ------------------------------------------------------------ соединение

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url, max_size=64 * 1024 * 1024, ping_interval=30,
            open_timeout=30, close_timeout=10)
        self.closed = False
        self.last_error = None
        self.connects += 1
        self._listener = asyncio.create_task(self._listen())
        return self

    async def reconnect(self):
        """Переподключиться к тому же таргету (id вкладки не меняется)."""
        await self.close()
        self._pending.clear()
        self._events = asyncio.Queue()
        return await self.connect()

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
        except Exception as e:
            # обрыв без close-frame: Chrome умер/перезапустился, порвался прокси
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self.closed = True

    async def cmd(self, method: str, params: dict | None = None,
                  timeout: float = 30):
        if not self.is_open:
            raise CDPConnectionLost(
                self.last_error or "CDP-соединение закрыто (нужен reconnect)")
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self.ws.send(json.dumps(msg))
        except Exception as e:
            self._pending.pop(mid, None)
            self.closed = True
            self.last_error = f"send: {type(e).__name__}: {e}"
            raise CDPConnectionLost(self.last_error) from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise
        except CDPConnectionLost:
            raise
        except Exception as e:
            # соединение закрылось, пока ждали ответ
            self._pending.pop(mid, None)
            self.closed = True
            self.last_error = f"recv: {type(e).__name__}: {e}"
            raise CDPConnectionLost(self.last_error) from e

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
        finally:
            self.closed = True


class CDPBrowser:
    """Тонкая обёртка над HTTP CDP-эндпоинтом (список вкладок, новые вкладки)."""

    def __init__(self, base: str = "http://127.0.0.1:9224"):
        self.base = base.rstrip("/")

    def tabs(self) -> list[dict]:
        return sync_http_json(f"{self.base}/json/list")

    def tab_by_id(self, target_id: str) -> dict | None:
        """Найти вкладку по id (None — вкладки больше нет)."""
        if not target_id:
            return None
        try:
            for t in self.tabs():
                if t.get("id") == target_id and t.get("type") == "page":
                    return t
        except Exception:
            return None
        return None

    def open_tab(self, url: str = "about:blank") -> CDPTab:
        """Открыть вкладку; вернуть НЕподключённый CDPTab с target_id."""
        target = f"{self.base}/json/new?{urllib.parse.quote(url, safe='')}"
        last_err = None
        for attempt in range(5):
            try:
                d = sync_http_json(target, method="PUT", timeout=30)
                return CDPTab(d["webSocketDebuggerUrl"], target_id=d.get("id"))
            except Exception as e:
                last_err = e
                time.sleep(5)
        raise RuntimeError(f"не удалось создать вкладку: {last_err}")

    def new_tab(self, url: str = "about:blank") -> CDPTab:
        return self.open_tab(url)

    def close_tab(self, target_id: str) -> bool:
        """Закрыть ОДНУ конкретную вкладку (чужие не трогаем)."""
        if not target_id:
            return False
        try:
            urllib.request.urlopen(f"{self.base}/json/close/{target_id}",
                                   timeout=20)
            return True
        except Exception:
            return False

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
