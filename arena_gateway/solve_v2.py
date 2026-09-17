#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Попытка автоматически пройти модалку «Security Verification» (reCAPTCHA v2).

Логика:
  1. открыть/подхватить вкладку arena.ai (режим direct);
  2. отправить сообщение ШТАТНЫМ способом (фокус → Input.insertText → Enter) —
     именно так сайт показывает модалку проверки;
  3. дождаться появления v2-anchor iframe (sitekey 6Le3_cYs…);
  4. кликнуть чекбокс по координатам (CDP Input.dispatchMouseEvent — работает
     сквозь cross-origin iframe);
  5. оценить результат: модалка исчезла / появился картинный челлендж (bframe
     видимый ~400×580) / пришёл ответ модели.

Картинный челлендж автоматически НЕ решается — скрипт просто фиксирует факт.
Скриншоты: /tmp/arena_v2_<шаг>.png
"""
import asyncio, base64, json, os, sys, time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, "/opt/orchestrator/arena_gateway")
import config as C                      # noqa: E402
from engine import ArenaEngine          # noqa: E402

V2_KEY = "6Le3_cYs"
OUT = "/tmp"

PROBE = """(() => {
  const ifr = [...document.querySelectorAll('iframe')].map(f => {
    const r = f.getBoundingClientRect();
    const cs = getComputedStyle(f);
    return {src: (f.src || '').slice(0, 90),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            visible: r.width > 4 && r.height > 4 && cs.visibility !== 'hidden'
                     && cs.display !== 'none'};
  });
  const t = document.body.innerText || '';
  return JSON.stringify({
    url: location.href.slice(0, 110),
    modal: /Security Verification|quick security check/i.test(t),
    v2: ifr.filter(i => i.src.indexOf('%s') >= 0),
    bframe: ifr.filter(i => i.src.indexOf('bframe') >= 0),
    nIframes: ifr.length,
    textLen: t.length,
    answered: /Ask followup/i.test(t),
    tail: t.slice(-260)
  });
})()""" % V2_KEY


async def activate(eng):
    """Вывести вкладку на передний план (иначе скриншот бывает пустым)."""
    try:
        import urllib.request
        urllib.request.urlopen(eng.cfg.CDP + "/json/activate/" + str(eng.target_id),
                               timeout=10).read()
    except Exception as e:
        print("    (activate: %s)" % e)
    try:
        await eng.tab.cmd("Page.enable")
    except Exception:
        pass


async def shot(eng, name):
    try:
        r = await eng.tab.cmd("Page.captureScreenshot", {"format": "png"})
        data = None
        if isinstance(r, dict):
            data = r.get("data") or (r.get("result") or {}).get("data")
        if not data:
            print("    (скриншот пустой, ответ CDP: %s)"
                  % (list(r.keys()) if isinstance(r, dict) else type(r).__name__))
            return None
        path = os.path.join(OUT, "arena_v2_%s.png" % name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        print("    скриншот:", path)
        return path
    except Exception as e:
        print("    скриншот не удался:", e); return None


async def snap(eng, label):
    raw = await eng.js(PROBE, timeout=30)
    d = json.loads(raw) if isinstance(raw, str) else (raw or {})
    print("--- %s" % label)
    print("    url: %s | модалка: %s | ответ получен: %s" %
          (d["url"], d["modal"], d["answered"]))
    print("    v2-iframe: %s" % json.dumps(d["v2"], ensure_ascii=False))
    print("    bframe:    %s" % json.dumps(d["bframe"], ensure_ascii=False))
    print("    хвост: %r" % d["tail"][-160:])
    return d


async def click_at(eng, x, y):
    for t, extra in (("mouseMoved", {}),
                     ("mousePressed", {"button": "left", "clickCount": 1}),
                     ("mouseReleased", {"button": "left", "clickCount": 1})):
        await eng.tab.cmd("Input.dispatchMouseEvent",
                          dict({"type": t, "x": int(x), "y": int(y)}, **extra))
        await asyncio.sleep(0.12)


async def main():
    eng = ArenaEngine(C)
    await eng.start()
    await activate(eng)
    await asyncio.sleep(1.5)
    print("=== шаг 1: состояние вкладки ===")
    d = await snap(eng, "до")
    await shot(eng, "01_before")

    if "/text/direct" not in d["url"]:
        print("=== шаг 1b: переход на /text/direct ===")
        await eng.tab.cmd("Page.navigate", {"url": "https://arena.ai/text/direct"})
        await asyncio.sleep(12)
        d = await snap(eng, "после навигации")

    print("=== шаг 2: отправка сообщения штатным UI ===")
    box = await eng.js("""(() => {
      const ta = [...document.querySelectorAll('textarea')]
        .find(t => /Ask anything|Ask followup/.test(t.getAttribute('placeholder') || ''));
      if (!ta) return '';
      const r = ta.getBoundingClientRect();
      return JSON.stringify([Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]);
    })()""", timeout=20)
    if not box:
        print("НЕТ ВИДИМОГО КОМПОЗЕРА — продолжать нечего"); return 1
    cx, cy = json.loads(box)
    print("    композер в %d,%d — кликаю" % (cx, cy))
    await click_at(eng, cx, cy)
    await asyncio.sleep(0.8)
    await eng.tab.cmd("Input.insertText", {"text": "Скажи ОК"})
    await asyncio.sleep(0.8)
    st = await eng.js("""(() => {
      const ta = [...document.querySelectorAll('textarea')]
        .find(t => /Ask anything|Ask followup/.test(t.getAttribute('placeholder') || ''));
      const b = [...document.querySelectorAll('button')]
        .find(b => /send message/i.test(b.getAttribute('aria-label') || ''));
      const br = b ? b.getBoundingClientRect() : null;
      return JSON.stringify({val: ta ? ta.value.slice(0, 40) : null,
                             send: br ? [Math.round(br.x + br.width / 2),
                                         Math.round(br.y + br.height / 2), b.disabled] : null});
    })()""", timeout=20)
    st = json.loads(st)
    print("    в композере: %r | кнопка Send: %s" % (st["val"], st["send"]))
    if not st["val"]:
        print("ТЕКСТ НЕ ПОПАЛ В КОМПОЗЕР — прерываю"); return 1
    if st["send"] and not st["send"][2]:
        print("    жму кнопку Send message в %d,%d" % (st["send"][0], st["send"][1]))
        await click_at(eng, st["send"][0], st["send"][1])
    else:
        print("    кнопка недоступна — жму Enter")
        for t in ("keyDown", "keyUp"):
            await eng.tab.cmd("Input.dispatchKeyEvent", {
                "type": t, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
            await asyncio.sleep(0.1)
    print("    отправлено")

    print("=== шаг 3: ждём модалку (до 45 с) ===")
    d = None
    for i in range(15):
        await asyncio.sleep(3)
        d = await snap(eng, "t+%d с" % ((i + 1) * 3))
        if d["v2"] or d["modal"]:
            break
        if d["answered"] and not d["modal"]:
            print(">>> ответ пришёл БЕЗ модалки — флаг снят!")
            await shot(eng, "02_answered")
            return 0

    if not (d and (d["v2"] or d["modal"])):
        print(">>> модалка не появилась (и ответа нет) — состояние неопределённое")
        await shot(eng, "02_nomodal")
        return 2
    await shot(eng, "03_modal")

    frames = [f for f in d["v2"] if f["visible"]] or d["v2"]
    if not frames:
        print(">>> модалка есть, но v2-iframe не найден в верхнем документе")
        return 2
    fr = frames[0]
    print("=== шаг 4: клик по чекбоксу v2 (%dx%d в %d,%d) ===" %
          (fr["w"], fr["h"], fr["x"], fr["y"]))
    if fr["w"] < 40 or fr["h"] < 20:
        print(">>> iframe слишком мал (%dx%d) — вероятно, невидимый v3; кликать некуда"
              % (fr["w"], fr["h"]))
        return 2
    cx = fr["x"] + min(30, fr["w"] // 8)
    cy = fr["y"] + fr["h"] // 2
    print("    координаты клика: %d,%d" % (cx, cy))
    await click_at(eng, cx, cy)

    print("=== шаг 5: результат (наблюдаю 40 с) ===")
    for i in range(8):
        await asyncio.sleep(5)
        d2 = await snap(eng, "после клика t+%d с" % ((i + 1) * 5))
        vis_b = [b for b in d2["bframe"] if b["visible"] and b["h"] > 200]
        if vis_b:
            print(">>> КАРТИНОЧНЫЙ ЧЕЛЛЕНДЖ %dx%d — автоматически не решается"
                  % (vis_b[0]["w"], vis_b[0]["h"]))
            await shot(eng, "04_image_challenge")
            return 3
        if not d2["modal"] and not [f for f in d2["v2"] if f["visible"]]:
            print(">>> модалка ИСЧЕЗЛА — похоже, проверка пройдена")
            await shot(eng, "05_cleared")
            await asyncio.sleep(10)
            d3 = await snap(eng, "контроль")
            if d3["answered"]:
                print(">>> модель ответила — флаг снят полностью")
            return 0
    await shot(eng, "06_timeout")
    print(">>> за 40 с ничего не изменилось")
    return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
