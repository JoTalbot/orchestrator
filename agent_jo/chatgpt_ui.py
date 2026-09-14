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

    async def attach_to_conversation(self, conv_id: str) -> bool:
        """Присоединиться к УЖЕ открытой вкладке этого чата (если есть).

        Полезно при нехватке памяти: новая вкладка может не отрендериться,
        а старая уже полностью загружена.
        """
        for t in self.cdp.tabs():
            if t.get("type") == "page" and f"/c/{conv_id}" in t.get("url", ""):
                from cdp import CDPTab
                self.tab = await CDPTab(t["webSocketDebuggerUrl"]).connect()
                self.conversation_id = conv_id
                self.log(f"присоединился к существующей вкладке чата {conv_id[:8]}…")
                return True
        return False

    async def open_chat(self, conv_id: str, project_url: str = "",
                        marker: str = "") -> bool:
        """Открыть чат в текущей вкладке любым способом (True = открыт).

        Основной путь — страница проекта и клик по карточке чата:
        сначала точное совпадение href, затем по тексту-маркеру (первое
        сообщение чата), с проверкой URL после каждого клика.
        """
        url = await self.current_url()
        if conv_id in url:
            return True
        if project_url:
            try:
                await self.tab.navigate(project_url, timeout=120)
            except Exception as e:
                self.log(f"переход на проект не удался: {e}")
            deadline = time.time() + 300
            while time.time() < deadline:
                # 1) точное совпадение href
                try:
                    clicked = await self.tab.evaluate(
                        f"() => {{ const a = document.querySelector("
                        f"'a[href*=\"/c/{conv_id}\"]');"
                        f" if (!a) return false; a.click(); return true; }}")
                except Exception:
                    clicked = False
                if clicked:
                    for _ in range(10):
                        await asyncio.sleep(6)
                        try:
                            if conv_id in await self.current_url():
                                return True
                        except Exception:
                            pass
                    # не открылся — вернёмся на проект и попробуем маркер
                    try:
                        await self.tab.navigate(project_url, timeout=120)
                    except Exception:
                        pass
                    await asyncio.sleep(10)
                # 2) клик по тексту-маркеру (карточки могут быть div без href —
                #    используем реальные mouse-события по координатам)
                if marker:
                    for cand_idx in range(5):
                        try:
                            found_el = await self.tab.evaluate(
                                "() => { const m = " + json.dumps(marker) + ";"
                                " const els = Array.from(document.querySelectorAll("
                                "'div, li, a, button'))"
                                " .filter(e => { const t = (e.innerText||'');"
                                "   return t.includes(m) && t.length >= 30 && t.length <= 400; });"
                                " els.sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);"
                                f" const e = els[{cand_idx}];"
                                " if (!e) return null; e.scrollIntoView({block:'center'});"
                                " return true; }")
                        except Exception:
                            found_el = None
                        if not found_el:
                            break
                        # ждём завершения плавной прокрутки и берём координаты
                        await asyncio.sleep(1.5)
                        try:
                            cand = await self.tab.evaluate(
                                "() => { const m = " + json.dumps(marker) + ";"
                                " const els = Array.from(document.querySelectorAll("
                                "'div, li, a, button'))"
                                " .filter(e => { const t = (e.innerText||'');"
                                "   return t.includes(m) && t.length >= 30 && t.length <= 400; });"
                                " els.sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);"
                                f" const e = els[{cand_idx}];"
                                " if (!e) return null;"
                                " const r = e.getBoundingClientRect();"
                                " return {x: Math.round(r.x + r.width/2),"
                                "         y: Math.round(r.y + r.height/2),"
                                "         len: (e.innerText||'').length,"
                                "         tag: e.tagName}; }")
                        except Exception:
                            cand = None
                        if not cand:
                            break
                        self.log(f"клик по карточке-кандидату {cand_idx} "
                                 f"(tag={cand['tag']}, len={cand['len']}) "
                                 f"x={cand['x']} y={cand['y']}")
                        await asyncio.sleep(0.5)
                        for ev_type in ("mousePressed", "mouseReleased"):
                            try:
                                await self.tab.cmd("Input.dispatchMouseEvent", {
                                    "type": ev_type,
                                    "x": cand["x"], "y": cand["y"],
                                    "button": "left", "clickCount": 1,
                                })
                            except Exception:
                                pass
                        for _ in range(8):
                            await asyncio.sleep(6)
                            try:
                                if conv_id in await self.current_url():
                                    return True
                            except Exception:
                                pass
                        # не открылся — назад на проект, следующий кандидат
                        try:
                            await self.tab.navigate(project_url, timeout=120)
                        except Exception:
                            pass
                        await asyncio.sleep(10)
                await asyncio.sleep(6)
            self.log("карточка чата не найдена на странице проекта")
        # 3) прямая URL-навигация (запасной вариант)
        target = (project_url.rstrip("/") + f"/c/{conv_id}" if project_url
                  else f"https://chatgpt.com/c/{conv_id}")
        try:
            await self.tab.navigate(target, timeout=120)
        except Exception as e:
            self.log(f"навигация не удалась: {e}")
        for _ in range(5):
            await asyncio.sleep(8)
            try:
                if conv_id in await self.current_url():
                    return True
            except Exception:
                pass
        return False

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
        # дождаться редактора (страница может грузиться долго; при нехватке
        # памяти на сервере — до 6 минут, с одной перезагрузкой страницы)
        deadline = time.time() + 360
        start = time.time()
        reloaded = False
        ok = False
        while time.time() < deadline:
            try:
                ok = await self.tab.evaluate(
                    "() => { const el = document.querySelector('#prompt-textarea')"
                    " || document.querySelector('div[contenteditable=\"true\"]');"
                    " if (!el) return false; el.focus(); return true; }")
            except Exception:
                ok = False
            if ok:
                break
            if not reloaded and time.time() - start > 150:
                try:
                    await self.tab.cmd("Page.reload", {})
                except Exception:
                    pass
                self.log("редактора нет >150с, перезагружаю страницу")
                reloaded = True
                await asyncio.sleep(20)
                continue
            dbg = await self.tab.evaluate(
                "() => location.href + ' | body:' + !!document.body + "
                " ' | editable:' + document.querySelectorAll('div[contenteditable=\"true\"]').length")
            self.log(f"редактора нет: {dbg}")
            await asyncio.sleep(6)
        if not ok:
            raise RuntimeError("редактор ввода не найден на странице")
        await asyncio.sleep(0.5)
        await self.tab.cmd("Input.insertText", {"text": text})
        await asyncio.sleep(0.8)
        # проверяем, что текст попал в редактор; если нет — вводим ещё раз
        got = await self.tab.evaluate(
            "() => { const el = document.querySelector('#prompt-textarea')"
            " || document.querySelector('div[contenteditable=\"true\"]');"
            " if (!el) return -1; const v = (el.value !== undefined && el.value !== null)"
            " ? el.value : (el.innerText || ''); return v.length; }")
        if got is not None and got < max(1, len(text) // 2):
            self.log(f"ввод не зафиксирован (len={got}), повторяю")
            await self.tab.cmd("Input.insertText", {"text": text})
            await asyncio.sleep(0.8)
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
        sent = False
        for attempt in range(3):
            await self._type_and_send(text)
            # проверяем, что сообщение реально ушло (URL стал /c/... или
            # появилось user-сообщение с нашим текстом)
            for _ in range(12):
                await asyncio.sleep(4)
                try:
                    if self._conv_id_from_url(await self.current_url()):
                        sent = True
                        break
                    users = await self.user_messages()
                    if users and text[:40] in (users[-1] or ""):
                        sent = True
                        break
                except Exception:
                    pass
            if sent:
                break
            self.log("сообщение не ушло, повторяю ввод")
            # фолбэк: кликнуть кнопку отправки (если Enter не сработал)
            try:
                await self.tab.evaluate(
                    "() => { const b = document.querySelector("
                    "'button[data-testid=\"send-button\"], "
                    "button[aria-label*=\"Send\"], button[aria-label*=\"Отправить\"]');"
                    " if (b) { b.click(); return true; } return false; }")
            except Exception:
                pass
            await self._dismiss_popups()
            await asyncio.sleep(8)
        self.conversation_id = self._conv_id_from_url(await self.current_url())
        reply = await self.wait_reply(reply_timeout)
        # после ответа URL гарантированно содержит id чата
        self.conversation_id = self._conv_id_from_url(await self.current_url())
        return reply

    @staticmethod
    def _conv_id_from_url(url: str) -> str | None:
        m = re.search(r"/c/([0-9a-fA-F-]{32,40})", url)
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
