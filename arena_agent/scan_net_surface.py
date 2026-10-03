#!/usr/bin/env python3
"""Перехват сетевых запросов arena.ai — какие эндпоинты реально зовёт фронтенд.

Второй шаг разведки после scan_api_surface.py: вместо чтения бандлов слушаем
CDP Network.* на живой вкладке /text/direct и печатаем все XHR/fetch к arena.ai
(метод + URL + первые байты тела ответа). Так находится эндпоинт, которым
фронтенд получает список моделей (`initialModels` в RSC теперь `$undefined`).

Только чтение: страница просто перезагружается, никаких промптов.
Пауза шлюза при этом не мешает — это отдельные запросы браузера.
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, "/opt/orchestrator")
from arena_agent import arena_api  # noqa: E402

WATCH = ("/api/", "model", "chat", "rsc", "trpc", "graphql", "leaderboard")


async def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    new = arena_api.open_arena_tab("https://arena.ai/text/direct")
    t = await arena_api._wait_ready(new.get("id"), verbose=True)
    tab = arena_api.CDPTab(t["webSocketDebuggerUrl"], t.get("id"))
    await tab.connect()
    await tab.cmd("Network.enable", {})
    await tab.cmd("Page.enable", {})
    try:
        await tab.cmd("Page.bringToFront")
    except Exception:
        pass
    await tab.cmd("Page.reload", {"ignoreCache": True})

    seen = {}         # url -> (method, type)
    posts = {}        # url -> тело запроса
    ids = {}          # requestId -> url (чтобы вытащить ответ)
    bodies = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            ev = await asyncio.wait_for(tab.events.get(), timeout=2)
        except asyncio.TimeoutError:
            continue
        m = ev.get("method")
        p = ev.get("params") or {}
        if m == "Network.requestWillBeSent":
            req = p.get("request") or {}
            url = req.get("url") or ""
            typ = p.get("type") or ""
            if "arena.ai" not in url:
                continue
            if not any(w in url.lower() for w in WATCH):
                continue
            seen[url.split("#")[0]] = (req.get("method"), typ)
            ids[p.get("requestId")] = url
            if req.get("method") == "POST" and req.get("postData"):
                posts[url] = req["postData"][:300]
        elif m == "Network.responseReceived":
            url = p.get("response", {}).get("url", "")
            if url in seen and any(w in url.lower() for w in ("/api/", "rsc")):
                ids.setdefault(p.get("requestId"), url)
                rid = p.get("requestId")
                if rid and len(bodies) < 6:
                    try:
                        r = await tab.cmd("Network.getResponseBody",
                                          {"requestId": rid}, timeout=20)
                        body = r.get("result", {}).get("body", "")
                        if body:
                            bodies.append((url, body[:600]))
                    except Exception:
                        pass
    await tab.close()
    try:
        arena_api.close_tab(t.get("id"))
    except Exception:
        pass

    print("\n=== запросы фронтенда к arena.ai (уникальные) ===")
    for url, (meth, typ) in sorted(seen.items()):
        print("  %-5s %-10s %s" % (meth, typ, url[:150]))
    print("\n=== POST-тела (усечены) ===")
    for url, body in posts.items():
        print("  %s\n      %s" % (url[:120], body[:280]))
    print("\n=== тела ответов (усечены) ===")
    for url, body in bodies:
        print("  %s\n      %s" % (url[:120], body[:500].replace("\n", " ")))
    with open("/tmp/net_surface.json", "w", encoding="utf-8") as f:
        json.dump({"seen": {k: list(v) for k, v in seen.items()},
                   "posts": posts,
                   "bodies": [list(b) for b in bodies]},
                  f, ensure_ascii=False, indent=1)
    print("\nполные данные: /tmp/net_surface.json")


if __name__ == "__main__":
    asyncio.run(main())
