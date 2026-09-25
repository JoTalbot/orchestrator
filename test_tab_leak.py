#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка, что scan_api.probe() и export_chats.run_export() закрывают свою
вкладку при любом исходе. Никаких файлов-заглушек и __pycache__: заглушка
инжектируется в sys.modules, тестируемый модуль исполняется из строки."""
import asyncio
import io
import sys
import types
import urllib.request
from pathlib import Path

import os
HERE = Path(os.environ.get("TAB_LEAK_SRC", "/opt/orchestrator"))
CLOSE_CALLS = []


class FakeResp:
    def read(self):
        return b"ok"


def fake_urlopen(url, timeout=None):
    CLOSE_CALLS.append(str(url))
    return FakeResp()


urllib.request.urlopen = fake_urlopen


def make_fake_api(ctor_raises, me_raises, fetch_raises):
    mod = types.ModuleType("arena_api")

    class CDPTab:
        target_id = "FAKETAB123"

        async def close(self):
            pass

    class ArenaAPI:
        def __init__(self, tab, verbose=False, own_tab=False):
            if ctor_raises:
                raise RuntimeError("конструктор упал после создания вкладки")
            self.tab = tab
            self.own_tab = own_tab
            self.calls = 0

        async def me(self):
            if me_raises:
                raise RuntimeError("обрыв на /api/me")
            return {"user": {"email": "x@y.z"}}

        async def balance(self):
            return {}

        async def history_all(self, **kw):
            return []

        async def fetch(self, *a, **kw):
            if fetch_raises:
                raise RuntimeError("обрыв зонда")
            return {"status": 200, "body": "ok"}

    async def connect(own=False, verbose=False):
        return CDPTab()

    def light_message(m):
        return {"role": m.get("role"), "text": ""}

    mod.CDPTab, mod.ArenaAPI = CDPTab, ArenaAPI
    mod.connect, mod.light_message = connect, light_message
    mod.unpack_rsc = lambda html: html
    mod.parse_transcript = lambda blob: None
    sys.modules["arena_api"] = mod
    return mod


def load(name, filename):
    src = io.open(HERE / filename, encoding="utf-8").read()
    code = compile(src, str(HERE / filename), "exec")
    m = types.ModuleType(name)
    m.__file__ = str(HERE / filename)
    m.__name__ = name
    exec(code, m.__dict__)
    return m


def closed_count():
    return len([c for c in CLOSE_CALLS if "json/close" in c])


results = []


def check(label, fn):
    CLOSE_CALLS.clear()
    try:
        fn()
        outcome = "вернулась без исключения"
    except Exception as ex:
        outcome = "выбросила %s: %s" % (type(ex).__name__, str(ex)[:32])
    n = closed_count()
    ok = n == 1
    results.append(ok)
    print("  %-26s -> %-36s | /json/close=%d | %s"
          % (label, outcome, n, "ВКЛАДКА ЗАКРЫТА" if ok else "!!! УТЕЧКА"))


print("=== scan_api.probe() ===")
for label, ctor, fetch in (("норма", 0, 0), ("обрыв зонда", 0, 1), ("падение конструктора", 1, 0)):
    make_fake_api(ctor, 0, fetch)
    sa = load("sa", "arena_agent/scan_api.py")
    check(label, lambda sa=sa: asyncio.run(
        sa.probe([{"path": "/api/models", "method": "GET"}], "chat-1")))

print("=== export_chats.run_export() ===")
for label, ctor, me, only_meta in (("норма", 0, 0, False),
                                   ("--only-meta (был баг)", 0, 0, True),
                                   ("обрыв api.me", 0, 1, False),
                                   ("падение конструктора", 1, 0, False)):
    make_fake_api(ctor, me, 0)
    ec = load("ec", "arena_export/export_chats.py")

    class A:
        pass
    a = A()
    a.verbose = False
    a.use_current_tab = False
    a.chat_id = None
    a.only_meta = only_meta
    a.include_archived = False
    a.limit = 0
    a.force = False
    a.skip_existing = False
    a.sleep = 0
    dd = Path("/tmp/fakedata")
    dd.mkdir(parents=True, exist_ok=True)
    check(label, lambda ec=ec, a=a, dd=dd: asyncio.run(ec.run_export(a, dd)))

print()
print("проверок: %d | утечек: %d" % (len(results), results.count(False)))
sys.exit(1 if False in results else 0)
