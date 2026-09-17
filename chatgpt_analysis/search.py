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
        "SELECT m.chat_title, m.role, m.ts, m.text FROM msgs_fts f "
        "JOIN messages m ON m.id = f.rowid "
        f"WHERE msgs_fts MATCH ? {where} ORDER BY rank LIMIT ?",
        params + [args.limit]).fetchall()
    import datetime
    q = args.query.lower()
    for title, role, ts, text in rows:
        dt = datetime.datetime.fromtimestamp(ts or 0).strftime("%Y-%m-%d")
        low = text.lower()
        pos = low.find(q.split()[0])
        start = max(0, pos - 60) if pos >= 0 else 0
        snip = text[start:start + 240].replace("\n", " ")
        print(f"[{dt}] [{role:9s}] [{title[:45]}]")
        print(f"   …{snip}…")
        print()


if __name__ == "__main__":
    main()
