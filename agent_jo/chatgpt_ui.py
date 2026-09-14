#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT UI — управление веб-интерфейсом через CDP (реальный Chrome).

Браузер сам решает proof-of-work и turnstile, поэтому здесь не нужны
обходы — просто печатаем сообщения как человек и читаем ответы.
"""

import asyncio
import json
import re
import time

from cdp import CDPBrowser


class ChatGPTUI:
    def __init__(self, cdp_base: str = "http://127.0.0.1:9224",
                 log=print, poll_sec: float = 8.0):
        self.cdp = CDPBrowser(cdp_base)
        self.tab = None
        self.log = log
        self.poll_sec = poll_sec
        self.conversation_id = None

    # ---------------------------------------------------------- управление

    async def start(self, url: str = "https://chatgpt.com/"):
        """Новая вкладка + загрузка SPA."""
        self.tab = await self.cdp.new_tab(url).connect()
        await self.tab.navigate(url, timeout=90)
        await asyncio.sleep(6)
        return self

    async def ensure_alive(self):
        """Переподключение, если вкладку закрыли (например, другой процесс)."""
        try:
            url = await self.tab.evaluate("location.href")
            return
        except Exception:
            self.log("вкладка мертва, создаём новую")
            target = "https://chatgpt.com/"
            if self.conversation_id:
                target = f"https://chatgpt.com/c/{self.conversation_id}"
            await self.start(target)
            await asyncio.sleep(4)

    async def close(self):
        if self.tab:
            await self.tab.close()

    async def session_user(self) -> str:
        txt = await self.tab.evaluate(
            "fetch('/api/auth/session').then(r=>r.json()).then(d=>JSON.stringify(d))",
            await_promise=True, timeout=60)
        d = json.loads(txt or "{}")
        return (d.get("user") or {}).get("email", "?")

    async def current_url(self) -> str:
        return await self.tab.evaluate("location.href")

    async def open_project_by_sidebar(self, project_name: str) -> str | None:
        """Фолбэк: найти папку проекта в сайдбаре и открыть её."""
        await self.tab.navigate("https://chatgpt.com/", timeout=90)
        await asyncio.sleep(7)
        for _ in range(3):
            clicked = await self.tab.evaluate(
                "() => { const btns = Array.from(document.querySelectorAll('button'));"
                " const b = btns.find(x => (x.innerText||'').trim().toLowerCase() === 'show more');"
                " if (b) { b.click(); return true; } return false; }")
            if not clicked:
                break
            await asyncio.sleep(1.2)
        # клик по строке проекта (раскрывает/открывает)
        ok = await self.tab.evaluate(
            f"() => {{ const rows = Array.from(document.querySelectorAll('div[role=\"button\"]'));"
            f" const r = rows.find(x => (x.innerText||'').trim().toLowerCase() === '{project_name.lower()}');"
            f" if (r) {{ r.click(); return true; }} return false; }}")
        if ok:
            await asyncio.sleep(3)
        href = await self.tab.evaluate(
            "(() => { const a = Array.from(document.querySelectorAll('a[href*=\"/g/g-p-\"]'))"
            " .find(x => (x.innerText||'').trim() !== ''); return a ? a.getAttribute('href') : null; })()")
        if href:
            full = href if href.startswith("http") else "https://chatgpt.com" + href.split("?")[0]
            await self.tab.navigate(full, timeout=120)
            await asyncio.sleep(8)
            return await self.current_url()
        return None

    # ---------------------------------------------------------- отправка

    async def _dismiss_popups(self):
        await self.tab.evaluate(
            "() => { const bad=['got it','okay','ok','dismiss','close','продолжить','понятно'];"
            " document.querySelectorAll('button').forEach(b=>{ const t=(b.innerText||'').trim().toLowerCase();"
            " if (bad.includes(t)) b.click(); }); }")

    async def _type_and_send(self, text: str):
        # дождаться редактора (страница могла ещё грузиться)
        deadline = time.time() + 90
        ok = False
        while time.time() < deadline:
            ok = await self.tab.evaluate(
                "() => { const el = document.querySelector('#prompt-textarea')"
                " || document.querySelector('div[contenteditable=\"true\"]');"
                " if (!el) return false; el.focus(); return true; }")
            if ok:
                break
            dbg = await self.tab.evaluate(
                "() => location.href + ' | body:' + !!document.body + "
                " ' | editable:' + document.querySelectorAll('div[contenteditable=\"true\"]').length")
            self.log(f"редактора нет: {dbg}")
            await asyncio.sleep(3)
        if not ok:
            raise RuntimeError("редактор ввода не найден на странице")
        await asyncio.sleep(0.5)
        await self.tab.cmd("Input.insertText", {"text": text})
        await asyncio.sleep(0.6)
        for _ in range(2):
            await self.tab.cmd("Input.dispatchKeyEvent",
                               {"type": "keyDown", "key": "Enter", "code": "Enter",
                                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            await self.tab.cmd("Input.dispatchKeyEvent",
                               {"type": "keyUp", "key": "Enter", "code": "Enter",
                                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            await asyncio.sleep(0.3)

    async def send(self, text: str, reply_timeout: float = 1200) -> str | None:
        """Отправляет сообщение и ждёт завершения хода ассистента."""
        await self._dismiss_popups()
        await self._type_and_send(text)
        await asyncio.sleep(3)
        self.conversation_id = self._conv_id_from_url(await self.current_url())
        reply = await self.wait_reply(reply_timeout)
        # после ответа URL гарантированно содержит id чата
        self.conversation_id = self._conv_id_from_url(await self.current_url())
        return reply

    @staticmethod
    def _conv_id_from_url(url: str) -> str | None:
        m = re.search(r"/c/([0-9a-fA-F-]{36})", url)
        return m.group(1) if m else None

    # ---------------------------------------------------------- чтение

    async def is_generating(self) -> bool:
        v = await self.tab.evaluate(
            "() => !!document.querySelector('[data-testid=\"stop-button\"]')")
        return bool(v)

    async def assistant_messages(self) -> list[str]:
        return await self.tab.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'[data-message-author-role=\"assistant\"]')).map(e => e.innerText || '')")

    async def user_messages(self) -> list[str]:
        return await self.tab.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'[data-message-author-role=\"user\"]')).map(e => e.innerText || '')")

    async def wait_reply(self, timeout: float = 1200) -> str | None:
        """Ждёт, пока генерация закончится и текст стабилизируется."""
        start = time.time()
        last_sig = None
        stable = 0
        while time.time() - start < timeout:
            try:
                gen = await self.is_generating()
                msgs = await self.assistant_messages()
            except Exception as e:
                self.log(f"ошибка опроса DOM: {e}")
                await asyncio.sleep(self.poll_sec)
                continue
            n = len(msgs)
            sig = (n, len(msgs[-1]) if msgs else 0)
            if not gen and msgs and sig == last_sig:
                stable += 1
                if stable >= 2:  # два стабильных замера подряд
                    return msgs[-1]
            else:
                stable = 0
            last_sig = sig
            await asyncio.sleep(self.poll_sec)
        return None  # таймаут — ход не завершился

    async def message_count(self) -> int:
        msgs = await self.assistant_messages()
        usr = await self.user_messages()
        return len(msgs) + len(usr)
