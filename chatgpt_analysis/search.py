#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RAG-поиск по всем чатам (SQLite FTS5).
Использование:
  .venv/bin/python chatgpt_analysis/search.py "blob sha" [--limit 10]
"""
import argparse
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--role", default="", help="user|assistant")
    args = ap.parse_args()

    db = sqlite3.connect("/opt/orchestrator/data/chat_index.db")
    where = ""
    params = [args.query]
    if args.role in ("user", "assistant"):
        where = "AND m.role = ?"
        params.append(args.role)
    rows = db.execute(
        "SELECT m.chat_title, m.role, m.ts, snippet(msgs_fts, 0, '<b>', "
        "</b>', '…', 24) FROM msgs_fts f JOIN messages m ON m.id = f.rowid "
        f"WHERE msgs_fts MATCH ? {where} ORDER BY rank LIMIT ?",
        params + [args.limit]).fetchall()
    import datetime
    for title, role, ts, snip in rows:
        dt = datetime.datetime.fromtimestamp(ts or 0).strftime("%Y-%m-%d")
        print(f"[{dt}] [{role:9s}] [{title[:45]}]")
        print(f"   {snip}")
        print()


if __name__ == "__main__":
    main()
