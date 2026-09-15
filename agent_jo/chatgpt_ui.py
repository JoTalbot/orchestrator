#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatGPT UI — управление веб-интерфейсом через CDP (реальный Chrome).

Браузер сам решает proof-of-work и turnstile, поэтому здесь не нужны
обходы — просто печатаем сообщения как человек и читаем ответы.

v2 — живучесть браузерного слоя:
  * драйвер держит ОДНУ свою вкладку (tab_id в состоянии) и переиспользует
    её между циклами и перезапусками службы — вкладки не накапливаются;
  * ensure_alive()/recover() восстанавливают работу после обрыва CDP или
    перезапуска Chromium: reconnect к своей вкладке, иначе новая;
  * отправка и ожидание ответа сами переподключаются при обрыве, вместо
    вечного dead-loop «ошибка цикла: ConnectionClosedError».
"""

import asyncio
import json
import re
import time

from cdp import CDPBrowser, CDPConnectionLost, CDPTab


class ChatGPTUI:
    def __init__(self, cdp_base: str = "http://127.0.0.1:9224",
                 log=print, poll_sec: float = 8.0):
        self.cdp = CDPBrowser(cdp_base)
        self.tab = None
        self.tab_id = None          # id НАШЕЙ вкладки (переиспользуем её)
        self.desired_url = None     # куда вкладка должна вернуться после сбоя
        self.log = log
        self.poll_sec = poll_sec
        self.conversation_id = None

    # ---------------------------------------------------------- управление

    async def start(self, url: str = "https://chatgpt.com/", tab_id: str = ""):
        """Вкладка + загрузка SPA. Если tab_id жив — переподключаемся к ней."""
        return await self.ensure_alive(url=url, tab_id=tab_id,
                                       force_new=not tab_id)

    @staticmethod
    def _same_url(a: str, b: str) -> bool:
        """Сравнение URL без query-хвоста и хвостового слеша."""
        def norm(u: str) -> str:
            return (u or "").split("?")[0].split("#")[0].rstrip("/")
        na, nb = norm(a), norm(b)
        return bool(na) and (na == nb or na.startswith(nb + "/")
                             or nb.startswith(na + "/"))

    async def ensure_alive(self, url: str | None = None,
                           tab_id: str | None = None,
                           force_new: bool = False) -> bool:
        """Гарантировать живое соединение со СВОЕЙ вкладкой ChatGPT.

        1) переподключиться к своей вкладке по id (если она ещё существует);
        2) если соединение живо — ничего не делать;
        3) иначе открыть новую вкладку и запомнить её id.

        Возвращает True, если есть рабочая вкладка; исключений не бросает.
        """
        if url:
            self.desired_url = url
        target = url or self.desired_url or self._fallback_url()

        # --- 1) своя вкладка по id
        tid = tab_id or self.tab_id
        if tid and not force_new:
            t = self.cdp.tab_by_id(tid)
            if t:
                if self.tab is not None and self.tab.is_open \
                        and self.tab.target_id == tid:
                    try:
                        cur = await self.current_url()
                        if self._needs_nav(cur, target):
                            await self.tab.navigate(target, timeout=120)
                        return True
                    except Exception as e:
                        self.log(f"соединение с вкладкой сломалось ({e}) — reconnect")
                else:
                    try:
                        old = self.tab
                        self.tab = await CDPTab(
                            t["webSocketDebuggerUrl"], target_id=tid).connect()
                        self.tab_id = tid
                        if old is not None:
                            await old.close()
                        cur = await self.current_url()
                        self.log(f"переподключился к своей вкладке {str(tid)[:8]}… "
                                 f"({str(cur)[:60]})")
                        if self._needs_nav(cur, target):
                            await self.tab.navigate(target, timeout=120)
                        return True
                    except Exception as e:
                        self.log(f"переподключение к вкладке {str(tid)[:8]}… "
                                 f"не удалось: {type(e).__name__}: {e}")
            else:
                self.log(f"своей вкладки {str(tid)[:8]}… уже нет — открою новую")
                self.tab_id = None

        # --- 2) соединение живо
        if self.tab is not None and self.tab.is_open and not force_new:
            return True

        # --- 3) новая вкладка
        try:
            tab = self.cdp.open_tab(target)
            self.tab = await tab.connect()
            self.tab_id = tab.target_id
            self.log(f"открыл вкладку {str(self.tab_id)[:8]}… ({target[:60]})")
            await asyncio.sleep(4)
            return True
        except Exception as e:
            self.log(f"не удалось открыть вкладку: {type(e).__name__}: {e}")
            self.tab = None
            self.tab_id = None
            return False

    @staticmethod
    def _needs_nav(cur: str | None, target: str | None) -> bool:
        """Нужно ли вести вкладку на target: только если она не на ChatGPT."""
        if not target:
            return False
        cur = str(cur or "")
        if not cur or cur.startswith("about:"):
            return True
        return "chatgpt.com" not in cur

    def _require_open(self):
        """Упасть с CDPConnectionLost, если соединение/вкладка уже мертвы."""
        if self.tab is None or not self.tab.is_open:
            raise CDPConnectionLost("CDP-соединение закрыто")

    def _fallback_url(self) -> str:
        if self.conversation_id:
            return f"https://chatgpt.com/c/{self.conversation_id}"
        return "https://chatgpt.com/"

    async def navigate_to(self, url: str, timeout: float = 120):
        """Перейти на url внутри своей вкладки (с восстановлением при обрыве)."""
        self.desired_url = url
        if not await self.ensure_alive(url=url, tab_id=self.tab_id):
            raise CDPConnectionLost("нет живой вкладки для перехода")
        cur = await self.current_url()
        if cur and self._same_url(cur, url):
            return cur
        await self.tab.navigate(url, timeout=timeout)
        return await self.current_url()

    async def recover(self, why: str, url: str | None = None) -> bool:
        """Восстановление после обрыва CDP: лог + reconnect/новая вкладка."""
        self.log(f"обрыв CDP ({why}) — восстанавливаю соединение")
        return await self.ensure_alive(url=url or self.desired_url,
                                       tab_id=self.tab_id)

    async def close(self):
        """Закрыть соединение (вкладку НЕ трогаем — её переиспользуем)."""
        if self.tab:
            await self.tab.close()

    async def close_own_tab(self):
        """Закрыть соединение и свою вкладку (для разовых запусков --once)."""
        await self.close()
        if self.tab_id:
            self.cdp.close_tab(self.tab_id)
            self.tab_id = None

    async def session_user(self) -> str:
        try:
            txt = await self.tab.evaluate(
                "fetch('/api/auth/session').then(r=>r.json()).then(d=>JSON.stringify(d))",
                await_promise=True, timeout=60)
            d = json.loads(txt or "{}")
            return (d.get("user") or {}).get("email", "?")
        except CDPConnectionLost:
            raise
        except Exception as e:
            self.log(f"сессию прочитать не удалось: {type(e).__name__}: {e}")
            return "?"

    async def attach_to_conversation(self, conv_id: str) -> bool:
        """Присоединиться к УЖЕ открытой вкладке этого чата (если есть).

        Полезно при нехватке памяти: новая вкладка может не отрендериться,
        а старая уже полностью загружена.
        """
        for t in self.cdp.tabs():
            if t.get("type") == "page" and f"/c/{conv_id}" in t.get("url", ""):
                old = self.tab
                self.tab = await CDPTab(t["webSocketDebuggerUrl"],
                                        target_id=t.get("id")).connect()
                if old is not None and old is not self.tab:
                    await old.close()
                self.tab_id = t.get("id")
                self.desired_url = t.get("url")
                self.conversation_id = conv_id
                self.log(f"присоединился к существующей вкладке чата {conv_id[:8]}…")
                return True
        return False

    async def attach_to_project_chat(self, project_url: str) -> bool:
        """Присоединиться к вкладке нового чата папки проекта.

        SPA может открыть созданный чат в отдельной вкладке — тогда наша
        вкладка остаётся на /project, а сообщения надо слать в ту, где
        реально есть чат: {project_url}/c/<id>.
        """
        base = (project_url or "").split("?")[0].split("#")[0].rstrip("/")
        if not base:
            return False
        for t in self.cdp.tabs():
            url = (t.get("url") or "").split("?")[0].split("#")[0].rstrip("/")
            if t.get("type") == "page" and url.startswith(base + "/c/"):
                old = self.tab
                self.tab = await CDPTab(t["webSocketDebuggerUrl"],
                                        target_id=t.get("id")).connect()
                if old is not None and old is not self.tab:
                    await old.close()
                self.tab_id = t.get("id")
                self.desired_url = url
                self.conversation_id = self._conv_id_from_url(url)
                self.log(f"присоединился к вкладке нового чата проекта "
                         f"{str(self.conversation_id)[:8]}…")
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
                self._require_open()  # обрыв CDP -> наверх, цикл восстановится
                # 1) точное совпадение href
                try:
                    clicked = await self.tab.evaluate(
                        f"() => {{ const a = document.querySelector("
                        f"'a[href*=\"/c/{conv_id}\"]');"
                        f" if (!a) return false; a.click(); return true; }}")
                except CDPConnectionLost:
                    raise
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
            "(() => { const links = Array.from(document.querySelectorAll('a[href*=\"/g/g-p-\"]'))"
            " .map(a => a.getAttribute('href') || '')"
            " .filter(h => h && h.indexOf('/c/') === -1);"  # именно ПАПКА, не чат
            " return links.length ? links[0] : null; })()")
        if href:
            full = href if href.startswith("http") else "https://chatgpt.com" + href.split("?")[0]
            full = full.split("/c/")[0].rstrip("/")  # срезаем хвост чата, если попал
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
        self._require_open()
        start = time.time()
        reloaded = False
        ok = False
        while time.time() < deadline:
            try:
                ok = await self.tab.evaluate(
                    "() => { const el = document.querySelector('#prompt-textarea')"
                    " || document.querySelector('div[contenteditable=\"true\"]');"
                    " if (!el) return false; el.focus(); return true; }")
            except CDPConnectionLost:
                raise
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
            try:
                dbg = await self.tab.evaluate(
                    "() => location.href + ' | body:' + !!document.body + "
                    " ' | editable:' + document.querySelectorAll('div[contenteditable=\"true\"]').length")
            except CDPConnectionLost:
                raise
            except Exception as e:
                dbg = f"(диагностика недоступна: {e})"
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
        # отправка: клик по кнопке Send реальными mouse-событиями
        # (Enter после перезапуска браузера перестал отправлять — клик надёжнее)
        btn = None
        for _ in range(10):
            btn = await self.tab.evaluate(
                "() => { const b = document.querySelector("
                "'button[data-testid=\"send-button\"]');"
                " if (!b || b.disabled) return null;"
                " const r = b.getBoundingClientRect();"
                " return {x: Math.round(r.x + r.width/2),"
                "         y: Math.round(r.y + r.height/2)}; }")
            if btn:
                break
            await asyncio.sleep(2)
        if btn:
            for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                try:
                    await self.tab.cmd("Input.dispatchMouseEvent", {
                        "type": ev, "x": btn["x"], "y": btn["y"],
                        "button": "left", "clickCount": 1})
                except Exception:
                    pass
        else:
            self.log("кнопка Send не найдена, пробую Enter")
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
        # счётчик user-сообщений ДО отправки (для проверки, что ушло)
        try:
            before = await self.tab.evaluate(
                "() => document.querySelectorAll("
                "'[data-message-author-role=\"user\"]').length")
        except Exception:
            before = None
        sent = False
        for attempt in range(3):
            try:
                await self._type_and_send(text)
            except CDPConnectionLost as e:
                if not await self.recover(f"обрыв при вводе: {e}"):
                    self.log("браузер не восстановился — сообщение не отправлено")
                    return None
                await asyncio.sleep(5)
                continue
            # подтверждение: user-сообщений стало больше или пошла генерация
            for _ in range(10):
                await asyncio.sleep(4)
                try:
                    gen = await self.tab.evaluate(
                        "() => !!document.querySelector('[data-testid=\"stop-button\"]')")
                    if gen:
                        sent = True
                        break
                    n = await self.tab.evaluate(
                        "() => document.querySelectorAll("
                        "'[data-message-author-role=\"user\"]').length")
                    if before is not None and n > before:
                        sent = True
                        break
                except CDPConnectionLost as e:
                    await self.recover(f"обрыв при проверке отправки: {e}")
                except Exception:
                    pass
            if sent:
                break
            self.log("сообщение не ушло, повторяю ввод")
            await self._dismiss_popups()
            await asyncio.sleep(8)
        if not sent:
            self.log("сообщение не удалось отправить за 3 попытки")
            return None
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
        # начальная пауза: дать SPA отправить запрос и начать генерацию,
        # иначе можно схватить старый стабилизировавшийся ответ
        await asyncio.sleep(12)
        last_sig = None
        stable = 0
        while time.time() - start < timeout:
            try:
                gen = await self.is_generating()
                msgs = await self.assistant_messages()
            except CDPConnectionLost as e:
                await self.recover(f"обрыв при опросе DOM: {e}")
                await asyncio.sleep(self.poll_sec)
                continue
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
