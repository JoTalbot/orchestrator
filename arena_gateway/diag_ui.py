#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика: отвергает ли арена и штатный UI (флаг аккаунта/IP) или только наши запросы.

Шаги:
  1. состояние страницы (URL, композер, iframe'ы reCAPTCHA, модалка проверки);
  2. отправка сообщения ШТАТНЫМ способом (фокус → Input.insertText → Enter);
  3. наблюдение за DOM 60 с: появился ли ответ модели, ошибка или модалка v2.

Ничего не парсит и не чинит — только факты.
"""
import asyncio, json, os, sys

sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, "/opt/orchestrator/arena_gateway")
import config as C                      # noqa: E402
from engine import ArenaEngine          # noqa: E402

PROBE = """(() => {
  const ta = document.querySelector('textarea');
  const ifr = [...document.querySelectorAll('iframe')].map(f => (f.src || '').slice(0, 90));
  const txt = document.body.innerText || '';
  const bad = /Security Verification|Проверка безопасности|recaptcha|went wrong|что-то пошло не так|не удалось/i;
  return JSON.stringify({
    url: location.href.slice(0, 120),
    hasTextarea: !!ta,
    placeholder: ta ? (ta.placeholder || '').slice(0, 60) : null,
    iframes: ifr.filter(s => s.indexOf('recaptcha') >= 0 || s.indexOf('bframe') >= 0),
    nIframes: ifr.length,
    textLen: txt.length,
    suspicious: (txt.match(new RegExp(bad, 'gi')) || []).slice(0, 4),
    tail: txt.slice(-320)
  });
})()"""


async def snap(eng, label):
    raw = await eng.js(PROBE, timeout=30)
    d = json.loads(raw) if isinstance(raw, str) else raw
    print("--- %s" % label)
    print("    url:", d["url"])
    print("    композер:", d["hasTextarea"], "| placeholder:", d["placeholder"])
    print("    iframe'ов:", d["nIframes"], "| recaptcha-iframe:", d["iframes"])
    print("    подозрительный текст:", d["suspicious"])
    print("    хвост текста:", repr(d["tail"][-200:]))
    return d


async def main():
    eng = ArenaEngine(C)
    await eng.start()
    await snap(eng, "до отправки")

    # фокус на композер
    focused = await eng.js("""(() => {
      const ta = document.querySelector('textarea');
      if (!ta) return 'no-textarea';
      ta.focus(); ta.click && ta.click();
      return document.activeElement === ta ? 'focused' : 'not-focused';
    })()""", timeout=20)
    print("--- фокус:", focused)
    if focused != "focused":
        print("композер не фокусируется — UI-тест невозможен")
        return

    await eng.tab.cmd("Input.insertText", {"text": "Скажи ОК"})
    await asyncio.sleep(1.0)
    await eng.tab.cmd("Input.dispatchKeyEvent", {
        "type": "keyDown", "key": "Enter", "code": "Enter",
        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    await eng.tab.cmd("Input.dispatchKeyEvent", {
        "type": "keyUp", "key": "Enter", "code": "Enter",
        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    print("--- отправлено через UI, наблюдаю 60 с")

    prev = None
    for i in range(12):
        await asyncio.sleep(5)
        d = await snap(eng, "t+%d с" % ((i + 1) * 5))
        sig = (d["textLen"], tuple(d["iframes"]), tuple(d["suspicious"]))
        if prev and sig == prev:
            print("    (без изменений)")
        prev = sig
        # ранний выход, если появились явные признаки верификации
        if d["suspicious"] and any("verification" in s.lower() or "безопасности" in s.lower()
                                   for s in d["suspicious"]):
            print(">>> модалка проверки безопасности — флаг на аккаунте подтверждён")
            break


if __name__ == "__main__":
    asyncio.run(main())
