#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_scan_js.py — BFS-скан всех JS-чанков arena.ai: пути API + контекст."""
import json
import re
import sys
from collections import deque

from curl_cffi import requests

ORIGIN = "https://arena.ai"
START_PAGES = ["/", "/agent", "/history/search", "/leaderboard/agent", "/coding"]
CHUNK_RE = re.compile(r'/_next/static/chunks/[A-Za-z0-9_%\.\-\[\]/]+\.js')
API_RE = re.compile(r'["\'`](/api/[A-Za-z0-9_\-/${}\.\[\]:]*)["\'`]')
API_CONCAT_RE = re.compile(r'["\'`](/api/[a-z0-9\-]+/)["\'`]\s*\+')

s = requests.Session(impersonate="chrome")
queue = deque()
seen_pages = set()
seen_chunks = set()
apis = {}
ctx_hits = []

KEYWORDS = ["api/chat", "api/history", "api/v2", "coding-agent/sessions",
            "conversation", "resumeChat", "createChat"]


def enqueue_chunks(text):
    for m in CHUNK_RE.finditer(text):
        u = ORIGIN + m.group(0)
        if u not in seen_chunks:
            seen_chunks.add(u)
            queue.append(u)


for p in START_PAGES:
    if p in seen_pages:
        continue
    seen_pages.add(p)
    try:
        r = s.get(ORIGIN + p, timeout=45)
        enqueue_chunks(r.text)
    except Exception as e:
        print("page ERR", p, e)

print("чанков в очереди:", len(queue))
scanned = 0
while queue:
    u = queue.popleft()
    try:
        r = s.get(u, timeout=45)
        txt = r.text
    except Exception as e:
        continue
    scanned += 1
    name = u.split("/")[-1][:28]
    for m in API_RE.finditer(txt):
        apis.setdefault(m.group(1), set()).add(name)
    for kw in ("api/chat", "api/history", "api/v2/", "coding-agent"):
        for m in re.finditer(re.escape(kw), txt):
            a = max(0, m.start() - 160)
            ctx_hits.append((name, txt[a:m.end() + 260].replace("\n", " ")))
    enqueue_chunks(txt)

print("просканировано чанков:", scanned)
print("\n=== ВСЕ /api/ пути (%d) ===" % len(apis))
for p in sorted(apis):
    print("  %-46s %s" % (p, ",".join(sorted(apis[p])[:3])))

print("\n=== КОНТЕКСТ вокруг api/chat | api/history | api/v2 ===")
shown = set()
for name, c in ctx_hits:
    key = c[100:200]
    if key in shown:
        continue
    shown.add(key)
    print("\n[%s] ...%s..." % (name, c))
    if len(shown) > 25:
        break
