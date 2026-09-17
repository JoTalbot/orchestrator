#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_api_test.py — проверяет, отвечает ли arena.ai API с сервера
(curl_cffi + TLS-отпечаток Chrome + куки сессии, включая cf_clearance)."""
import json
import sys

from curl_cffi import requests

AUTH = "/opt/orchestrator/.secrets/arena_auth.json"

d = json.load(open(AUTH))
tok = d["access_token"]
cookies = d.get("cookies", {})

print("impersonate targets:", [t for t in dir(requests) if t.startswith("Browser")] or "")
try:
    from curl_cffi.requests import BrowserType
    print("доступные:", [b for b in dir(BrowserType) if not b.startswith("_")][:40])
except Exception as e:
    print("BrowserType:", e)

s = requests.Session(impersonate="chrome")
s.headers.update({
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "authorization": "Bearer " + tok,
    "origin": "https://arena.ai",
    "referer": "https://arena.ai/agent",
    "user-agent": ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
})
for k, v in cookies.items():
    s.cookies.set(k, v, domain=".arena.ai")

TESTS = [
    ("GET", "/api/me", None),
    ("GET", "/api/history/unified?limit=5&includeArchived=false", None),
    ("GET", "/api/history/unified?limit=5", None),
    ("GET", "/api/coding-agent/sessions", None),
    ("GET", "/api/connectors/status", None),
]

for method, path, body in TESTS:
    url = "https://arena.ai" + path
    try:
        r = s.request(method, url, timeout=45,
                      data=json.dumps(body) if body else None,
                      headers={"content-type": "application/json"} if body else {})
        txt = r.text
        print("\n--- %s %s -> %s (%d bytes)" % (method, path, r.status_code, len(txt)))
        print(txt[:900])
    except Exception as e:
        print("\n--- %s %s -> ERROR %s: %s" % (method, path, type(e).__name__, e))
