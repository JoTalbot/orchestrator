#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""solve_v2_human.py — human-in-the-loop помощник для moдалки «Security Verification».

Решение челленджа принимает ЧЕЛОВЕК: скрипт лишь доводит страницу до картинки,
приносит скриншот с пронумерованной сеткой 4×4 и выполняет клики по указанным
 человеком тайлам. Автономного «решателя» здесь нет и быть не должно.

Команды:
  trigger   отправить сообщение штатным UI, дождаться челленджа, снять скриншот
            (/tmp/arena_v2_human.png) и распечатать карту сетки + геометрию.
            Состояние сохраняется в /tmp/arena_v2_human_state.json.
  click 2,3,6,7
            кликнуть указанные тайлы (нумерация 1..16, сверху вниз слева направо),
            затем кнопку Verify/Skip, снять скриншот результата и сообщить исход.
  status    показать текущее состояние (модалка/челлендж/ответ).

Коды выхода: 0 = проверка пройдена (модалка исчезла), 1 = ошибка, 2 = нужен человек
(челлендж на экране), 3 = новый раунд челленджа.
"""
import asyncio, base64, json, os, sys, time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, "/opt/orchestrator/arena_gateway")
import config as C                      # noqa: E402
from engine import ArenaEngine          # noqa: E402

STATE = "/tmp/arena_v2_human_state.json"
SHOT = "/tmp/arena_v2_human.png"
V2_KEY = "6Le3_cYs"

TOP_PROBE = """(() => {
  const pick = f => { const r = f.getBoundingClientRect();
    return {src: (f.src || '').slice(0, 80), x: r.x, y: r.y, w: r.width, h: r.height}; };
  const ifr = [...document.querySelectorAll('iframe')].map(pick);
  const t = document.body.innerText || '';
  return JSON.stringify({
    url: location.href.slice(0, 110),
    modal: /Security Verification|quick security check/i.test(t),
    v2: ifr.filter(i => i.src.indexOf('%s') >= 0),
    bframe: ifr.filter(i => i.src.indexOf('bframe') >= 0 && i.w > 100
                      && i.h > 200 && i.y > -500),
    answered: /Ask followup/i.test(t)
  });
})()""" % V2_KEY

# карта DOM внутри iframe челленджа (выполняется в isolated world этого фрейма)
INNER_MAP = """(() => {
  const out = [];
  const add = (el, kind) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    out.push({kind, id: el.id || null,
              cls: (el.className || '').toString().slice(0, 40),
              txt: (el.innerText || '').trim().slice(0, 30),
              x: Math.round(r.x), y: Math.round(r.y),
              w: Math.round(r.width), h: Math.round(r.height)});
  };
  document.querySelectorAll('.rc-imageselect-tile, td.rc-imageselect-tile, '
    + '.rc-imageelect-tile, #recaptcha-verify-button, .rc-button-red, '
    + '#recaptcha-verify-button, button, .rc-imageselect-header').forEach(
      el => add(el, el.id ? 'id:' + el.id : (el.tagName || '').toLowerCase()));
  const hdr = document.querySelector('.rc-imageselect-header, .rc-imageelect-header');
  return JSON.stringify({tiles: out.filter(o => /tile/i.test(o.cls)),
                         buttons: out.filter(o => o.kind !== 'tiles' &&
                           !/tile/i.test(o.cls)).slice(0, 12),
                         prompt: hdr ? hdr.innerText.replace(/\\s+/g, ' ').slice(0, 90) : null});
})()"""


def save(state):
    with open(STATE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


async def shot(eng):
    r = await eng.tab.cmd("Page.captureScreenshot", {"format": "png"})
    data = r.get("data") or (r.get("result") or {}).get("data") if isinstance(r, dict) else None
    if not data:
        return None
    with open(SHOT, "wb") as f:
        f.write(base64.b64decode(data))
    return SHOT


async def top(eng):
    raw = await eng.js(TOP_PROBE, timeout=30)
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


async def frame_id(eng, needle):
    tree = (await eng.tab.cmd("Page.getFrameTree")).get("frameTree", {})
    stack, found = [tree], None
    while stack:
        n = stack.pop()
        fr = n.get("frame") or {}
        if needle in (fr.get("url") or ""):
            found = fr.get("id")
        stack.extend(n.get("childFrames") or [])
    return found


async def inner(eng, fid):
    world = await eng.tab.cmd("Page.createIsolatedWorld",
                              {"frameId": fid, "worldName": "arena_v2_human"})
    ctx = world.get("executionContextId")
    r = await eng.tab.cmd("Runtime.evaluate",
                          {"expression": INNER_MAP, "contextId": ctx,
                           "returnByValue": True})
    val = (r.get("result") or {}).get("value")
    return json.loads(val) if isinstance(val, str) else (val or {})


async def click_at(eng, x, y):
    for t, extra in (("mouseMoved", {}),
                     ("mousePressed", {"button": "left", "clickCount": 1}),
                     ("mouseReleased", {"button": "left", "clickCount": 1})):
        await eng.tab.cmd("Input.dispatchMouseEvent",
                          dict({"type": t, "x": int(x), "y": int(y)}, **extra))
        await asyncio.sleep(0.12)


async def ui_send(eng):
    box = await eng.js("""(() => {
      const ta = [...document.querySelectorAll('textarea')]
        .find(t => /Ask anything|Ask followup/.test(t.getAttribute('placeholder') || ''));
      if (!ta) return '';
      const r = ta.getBoundingClientRect();
      return JSON.stringify([Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]);
    })()""", timeout=20)
    if not box:
        return False
    cx, cy = json.loads(box)
    await click_at(eng, cx, cy)
    await asyncio.sleep(0.8)
    await eng.tab.cmd("Input.insertText", {"text": "Скажи ОК"})
    await asyncio.sleep(0.8)
    btn = await eng.js("""(() => {
      const b = [...document.querySelectorAll('button')]
        .find(b => /send message/i.test(b.getAttribute('aria-label') || ''));
      if (!b || b.disabled) return '';
      const r = b.getBoundingClientRect();
      return JSON.stringify([Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]);
    })()""", timeout=20)
    if btn:
        x, y = json.loads(btn)
        await click_at(eng, x, y)
    else:
        for t in ("keyDown", "keyUp"):
            await eng.tab.cmd("Input.dispatchKeyEvent", {
                "type": t, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    return True


async def cmd_trigger(eng):
    d = await top(eng)
    if d.get("bframe"):
        print("челлендж уже на экране — снимаю карту")
    else:
        if not d.get("modal"):
            print("отправляю сообщение штатным UI…")
            if not await ui_send(eng):
                print("НЕТ КОМПОЗЕРА"); return 1
            for _ in range(15):
                await asyncio.sleep(3)
                d = await top(eng)
                if d.get("bframe") or d.get("modal"):
                    break
                if d.get("answered"):
                    print("ОТВЕТ ПРИШЁЛ БЕЗ МОДАЛКИ — флаг уже снят!"); return 0
        if not d.get("modal"):
            print("модалка не появилась"); return 1
        if not d.get("bframe"):
            # кликаем чекбокс v2, чтобы развернуть челлендж
            fr = (d.get("v2") or [{}])[0]
            if not fr.get("w"):
                print("нет v2-iframe"); return 1
            await click_at(eng, fr["x"] + min(30, fr["w"] // 8), fr["y"] + fr["h"] / 2)
            for _ in range(10):
                await asyncio.sleep(3)
                d = await top(eng)
                if d.get("bframe"):
                    break
    if not d.get("bframe"):
        print("челлендж не развернулся"); await shot(eng); return 1
    bf = d["bframe"][0]
    fid = await frame_id(eng, "bframe")
    m = await inner(eng, fid) if fid else {}
    state = {"at": time.time(), "bframe": bf, "frame_id": fid, "map": m,
             "top": d}
    save(state)
    p = await shot(eng)
    print("скриншот:", p)
    print("bframe: x=%.0f y=%.0f w=%.0f h=%.0f" % (bf["x"], bf["y"], bf["w"], bf["h"]))
    print("задание:", (m or {}).get("prompt"))
    tiles = (m or {}).get("tiles") or []
    print("тайлов в DOM:", len(tiles))
    for i, t in enumerate(tiles, 1):
        print("  %2d: x=%d y=%d %dx%d" % (i, t["x"], t["y"], t["w"], t["h"]))
    for b in (m or {}).get("buttons") or []:
        print("  кнопка: id=%s cls=%s txt=%r x=%d y=%d %dx%d"
              % (b["id"], b["cls"], b["txt"], b["x"], b["y"], b["w"], b["h"]))
    return 2


# Геометрия челленджа относительно bframe (400x580), измерена по скриншотам:
# сетка 4x4 начинается в (1.6% w, 23.5% h) и занимает (96.6% w, 70.4% h);
# кнопка Verify/Skip — правый нижний угол панели (~85.6% w, ~98% h).
GRID_DX, GRID_DY, GRID_W, GRID_H = 0.0163, 0.2348, 0.9655, 0.7043
BTN_X, BTN_Y = 0.856, 0.98


def ratio_tiles(bf):
    out = []
    tw, th = bf["w"] * GRID_W / 4, bf["h"] * GRID_H / 4
    for i in range(4):
        for j in range(4):
            out.append({"x": bf["w"] * GRID_DX + j * tw,
                        "y": bf["h"] * GRID_DY + i * th,
                        "w": tw, "h": th})
    return out


async def cmd_click(eng, tiles_arg):
    st = load()
    if not st:
        print("нет состояния — сначала trigger"); return 1
    bf, m = st["bframe"], st.get("map") or {}
    tiles = m.get("tiles") or []
    if not tiles:
        tiles = ratio_tiles(bf)
        print("карта DOM пуста — считаю тайлы по пропорциям сетки")
    idx = [int(x) for x in tiles_arg.replace(",", " ").split()]
    for i in idx:
        if not 1 <= i <= len(tiles):
            print("тайл %d вне диапазона 1..%d" % (i, len(tiles))); return 1
    for i in idx:
        t = tiles[i - 1]
        x, y = bf["x"] + t["x"] + t["w"] / 2, bf["y"] + t["y"] + t["h"] / 2
        print("клик по тайлу %d → %d,%d" % (i, x, y))
        await click_at(eng, x, y)
        await asyncio.sleep(0.5)
    # кнопка verify/skip — правая нижняя в панели челленджа
    btn = None
    for b in m.get("buttons") or []:
        if b["y"] > bf["h"] - 90 and b["x"] > bf["w"] / 2:
            btn = b
    if btn:
        x, y = bf["x"] + btn["x"] + btn["w"] / 2, bf["y"] + btn["y"] + btn["h"] / 2
        print("клик по кнопке %r → %d,%d" % (btn.get("txt") or btn.get("id"), x, y))
    else:
        x, y = bf["x"] + bf["w"] * BTN_X, bf["y"] + bf["h"] * BTN_Y
        print("кнопка по пропорциям → %d,%d" % (x, y))
    await click_at(eng, x, y)
    for _ in range(10):
        await asyncio.sleep(3)
        d = await top(eng)
        if d.get("bframe"):
            fid = await frame_id(eng, "bframe")
            m2 = await inner(eng, fid) if fid else {}
            save({"at": time.time(), "bframe": d["bframe"][0], "frame_id": fid,
                  "map": m2, "top": d})
            print("НОВЫЙ РАУНД челленджа:", m2.get("prompt"))
            await shot(eng)
            return 3
        if not d.get("modal"):
            await asyncio.sleep(8)
            d2 = await top(eng)
            print("МОДАЛКА ИСЧЕЗЛА; ответ получен:", d2.get("answered"))
            await shot(eng)
            return 0
    await shot(eng)
    print("состояние после кликов неясно — скриншот снят")
    return 2


async def cmd_status(eng):
    d = await top(eng)
    print(json.dumps(d, ensure_ascii=False, indent=1))
    await shot(eng)
    return 2 if d.get("bframe") else (0 if d.get("answered") else 1)


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "trigger"
    eng = ArenaEngine(C)
    await eng.start()
    try:
        await eng.tab.cmd("Page.enable")
    except Exception:
        pass
    if cmd == "trigger":
        return await cmd_trigger(eng)
    if cmd == "click":
        return await cmd_click(eng, sys.argv[2])
    if cmd == "status":
        return await cmd_status(eng)
    print(__doc__); return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
