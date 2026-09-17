#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разведка DOM чата: поле ввода и кнопки отправки + отправка через Enter (CDP)."""
import asyncio
import json
import sys
import time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_api import CDPTab, open_arena_tab, ORIGIN  # noqa: E402

INTERESTING = ("/api/", "/nextjs-api/", "/ai-proxy")
SKIP = ("datadoghq", "google-analytics", "posthog", "/rpc/", "gstatic", "sentry")


async def ready_tab(url, tries=120):
    t = open_arena_tab(url)
    for _ in range(tries):
        await asyncio.sleep(1)
        try:
            tab = CDPTab(t["webSocketDebuggerUrl"], t.get("id"))
            await tab.connect()
            href = await tab.js("location.href")
            title = await tab.js("document.title")
            state = await tab.js("document.readyState")
            if (href or "").startswith(ORIGIN) and state == "complete" \
                    and "Just a moment" not in (title or ""):
                try:
                    await tab.cmd("Page.enable")
                    await tab.cmd("Page.bringToFront")
                except Exception:
                    pass
                return tab, t.get("id")
            await tab.close()
        except Exception:
            pass
    raise RuntimeError("вкладка не готова")


async def main():
    chat_id, text = sys.argv[1], sys.argv[2]
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 40
    tab, tid = await ready_tab("%s/agent/%s" % (ORIGIN, chat_id))
    await tab.cmd("Network.enable")
    print("# вкладка готова", tid, file=sys.stderr)

    info = await tab.js("""
(() => {
  const ta = document.querySelector('textarea');
  const btns = [...document.querySelectorAll('button')].map(b => ({
     label: b.getAttribute('aria-label'), title: b.title,
     text: (b.textContent||'').trim().slice(0,30), disabled: b.disabled,
     type: b.type, cls: (b.className||'').slice(0,60)}));
  return JSON.stringify({hasTa: !!ta, taPlaceholder: ta && ta.placeholder,
     btnCount: btns.length,
     btns: btns.filter(b => b.label || b.title || b.text).slice(-14)});
})()
""")
    print("DOM:", info[:2000])

    typed = await tab.js("""
(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return 'no textarea';
  const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, %s);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.focus();
  return 'ok:' + ta.value.length;
})()
""" % json.dumps(text))
    print("# ввод:", typed, file=sys.stderr)
    await asyncio.sleep(1.0)

    # Enter через CDP (React слушает keydown на поле)
    for etype, extra in (("rawKeyDown", {}), ("char", {"text": "\r"}),
                         ("keyUp", {})):
        await tab.cmd("Input.dispatchKeyEvent", dict({
            "type": etype, "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
            "unmodifiedText": "\r" if etype == "char" else None,
        }, **extra))
    print("# Enter отправлен", file=sys.stderr)

    reqs, order = {}, []
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            ev = await asyncio.wait_for(tab.events.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        m, p = ev.get("method"), ev.get("params", {})
        if m == "Network.requestWillBeSent":
            rq = p.get("request", {})
            u = rq.get("url", "")
            if not any(k in u for k in INTERESTING) or any(s in u for s in SKIP):
                continue
            rid = p.get("requestId")
            reqs[rid] = {"t": round(time.time() - t0, 1), "method": rq.get("method"),
                         "url": u, "post": rq.get("postData") or ""}
            order.append(rid)
        elif m == "Network.responseReceived" and p.get("requestId") in reqs:
            r = p.get("response", {})
            reqs[p["requestId"]]["status"] = r.get("status")
            reqs[p["requestId"]]["mime"] = r.get("mimeType")

    posts = [reqs[i] for i in order if reqs[i]["method"] != "GET"]
    print("\n=== НЕ-GET запросы (%d) ===" % len(posts))
    print(json.dumps(posts, ensure_ascii=False, indent=1)[:14000])
    import urllib.request
    urllib.request.urlopen("http://127.0.0.1:9222/json/close/" + tid, timeout=10)


asyncio.run(main())
