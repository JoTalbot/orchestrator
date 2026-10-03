#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cleanup_leftovers.py — убрать из аккаунта хвосты evaluation-сессий шлюза.

Зачем: каждый запрос шлюза создаёт сессию `create-evaluation`, а после ответа её
закрывает server action `deleteEvaluationSession`. Если id действия устарел
(арена перезаливает сборку — id меняются), закрытие падает с
`404 Server action not found`, и сессии копятся в истории аккаунта.

Лечение id: `python3 discover_actions.py --write` (обновляет server_actions.json),
затем этот скрипт — чтобы вычистить накопленное.

Использование:
    ../.venv/bin/python cleanup_leftovers.py --dry-run     # только показать
    ../.venv/bin/python cleanup_leftovers.py               # удалить
    ../.venv/bin/python cleanup_leftovers.py --limit 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "arena_agent"))

import config as C          # noqa: E402
from engine import ArenaEngine  # noqa: E402


async def history(eng, include_archived=True, limit=50, max_pages=40):
    """Все записи истории (постранично)."""
    out, cursor, page = [], None, 0
    while page < max_pages:
        q = ["limit=%d" % limit,
             "includeArchived=%s" % ("true" if include_archived else "false")]
        if cursor:
            q.append("cursor=" + urllib.parse.quote(str(cursor)))
        d = await eng.fetch_json("GET", "/api/history/unified?" + "&".join(q))
        # __agwFetch отдаёт {status, body}, где body — строка JSON: разбираем её
        if isinstance(d, dict) and isinstance(d.get("body"), str):
            try:
                d = json.loads(d["body"])
            except Exception:
                break
        if not isinstance(d, dict):
            break
        out.extend(d.get("entries") or [])
        pg = d.get("pagination") or {}
        cursor = pg.get("cursor")
        if not pg.get("hasMore") or not cursor:
            break
        page += 1
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="сколько максимум убрать")
    ap.add_argument("--all-types", action="store_true",
                    help="не только evaluation, а все чаты типа evaluation/direct")
    a = ap.parse_args()

    eng = ArenaEngine(C)
    await eng.ensure_tab()          # без этого JS-ядро __agwFetch/__agwAction не установлено
    mode = C.CLEANUP_MODE
    entries = await history(eng)
    targets = [e for e in entries if e.get("type") == "evaluation"]
    print("всего записей в истории: %d | evaluation-хвостов: %d (режим закрытия: %s)"
          % (len(entries), len(targets), mode))
    if a.limit:
        targets = targets[:a.limit]
    if a.dry_run:
        for e in targets[:20]:
            print("  %s  %s" % (e.get("id"), (e.get("title") or "")[:60].replace("\n", " ")))
        print("... (dry-run, ничего не удалено)")
        return 0

    ok = fail = 0
    for i, e in enumerate(targets, 1):
        cid = e.get("id")
        try:
            r = await eng.cleanup(cid)
        except Exception as exc:
            print("%3d/%d %s → исключение %s" % (i, len(targets), cid, str(exc)[:100]))
            fail += 1
            continue
        good = isinstance(r, dict) and r.get("status") in (200, 204)
        print("%3d/%d %s → %s" % (i, len(targets), cid,
                                  "ok" if good else "ОШИБКА %s" % json.dumps(r)[:120]))
        ok += good
        fail += (not good)
        await asyncio.sleep(0.4)

    left = [e for e in await history(eng) if e.get("type") == "evaluation"]
    print("\nитог: закрыто %d, ошибок %d | осталось evaluation-хвостов: %d"
          % (ok, fail, len(left)))
    print("счётчики движка:", dict(eng.counters))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
