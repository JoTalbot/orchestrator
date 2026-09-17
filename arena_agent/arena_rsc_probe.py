#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_rsc_probe.py — тянет HTML страницы чата ИЗ браузера и ищет в
RSC-пейлоаде (self.__next_f.push) сообщения и курсор транскрипта."""
import asyncio
import json
import re
import sys

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_fetch import Tab, arena_tab  # noqa: E402


async def main():
    chat_id = sys.argv[1]
    t = arena_tab()
    tab = Tab(t["webSocketDebuggerUrl"])
    await tab.connect()
    await tab.cmd("Runtime.enable")
    expr = """
(async (u) => {
  const r = await fetch(u, {credentials: 'include',
    headers: {'accept': 'text/html,application/xhtml+xml'}});
  const t = await r.text();
  return JSON.stringify({status: r.status, len: t.length, html: t});
})(%s)
""" % json.dumps("https://arena.ai/agent/" + chat_id)
    raw = await tab.js(expr, timeout=120)
    d = json.loads(raw)
    html = d["html"]
    print("status", d["status"], "len", d["len"])
    open("/tmp/arena_page.html", "w").write(html)

    # распаковываем self.__next_f.push([1,"...."])
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    blob = "".join(json.loads('"' + c + '"') for c in chunks)
    open("/tmp/arena_rsc.txt", "w").write(blob)
    print("RSC-блоков:", len(chunks), "распаковано байт:", len(blob))
    for kw in ['"messages"', '"cursor"', '"role":"assistant"', '"role":"user"',
               '"parts"', '"transcript"', '"pagination"', 'hasMore']:
        print("  %-20s %d" % (kw, blob.count(kw)))
    # показать кусок вокруг первого вхождения messages
    i = blob.find('"messages"')
    if i >= 0:
        print("\n--- контекст messages ---")
        print(blob[max(0, i - 300):i + 1500])


asyncio.run(main())
