#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arena_ctl.py — управление чатами Arena AI (Agent Mode) из консоли.

Команды:
  me | pulse | balance                 профиль, лимит дня, кредиты
  list [--limit N] [--archived]        список чатов
  search "строка"                      поиск по чатам
  get <id> [--out f.json]              полный транскрипт (JSON)
  md  <id> [--out f.md]                транскрипт в Markdown
  files <id>                           файлы песочницы чата (workspace)
  cat <id> <nodeId> [--path p]         содержимое файла из песочницы
  cost <id>                            сколько стоил чат
  archive <id> | unarchive <id>        архив
  delete <id>                          удалить чат
  create "текст" [--wait]              создать чат и отправить первое сообщение
  send <id> "текст" [--wait]           отправить сообщение в существующий чат
  stop <id>                            остановить генерацию
  wait <id> [--timeout 600]            ждать завершения ответа агента
  stream <id> [--seconds 30]           живой SSE-поток ответа агента
  rename <id> "заголовок"              переименовать чат
  token <id>                           publicAccessToken сессии (Trigger.dev)
  upload <файл>                        загрузить файл в CAS агента

Всё работает через вкладку arena.ai в браузере (CDP :9222) — Cloudflare и
reCAPTCHA Enterprise не пускают прямой HTTP с серверного IP.
"""
import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_api import (ArenaAPI, connect, light_message,  # noqa: E402
                       transcript_text, wait_idle)


def jdump(o, n=None):
    s = json.dumps(o, ensure_ascii=False, indent=1)
    print(s[:n] if n else s)


def to_markdown(tr, title=""):
    out = ["# %s" % (title or tr.get("id")), ""]
    for m in tr.get("messages", []):
        lm = light_message(m)
        out.append("## %s" % lm["role"].upper())
        if lm.get("text"):
            out.append(lm["text"])
        for t in lm.get("tools", []):
            out.append("\n**%s** (%s)" % (t["tool"], t.get("state")))
            out.append("```json\n%s\n```" % t.get("input"))
            if t.get("output"):
                out.append("```json\n%s\n```" % t.get("output"))
        out.append("")
    return "\n".join(out)


async def main_async(a):
    tab = await connect(own=not a.use_current_tab, verbose=a.verbose)
    api = ArenaAPI(tab, verbose=a.verbose, own_tab=not a.use_current_tab)
    cmd = a.cmd
    try:
        if cmd == "me":
            jdump(await api.me())
        elif cmd == "pulse":
            jdump(await api.pulse())
        elif cmd == "balance":
            jdump(await api.balance())
        elif cmd == "list":
            entries = await api.history_all(limit=50,
                                            include_archived=a.archived,
                                            max_chats=a.limit or None)
            print("всего чатов: %d" % len(entries))
            for e in entries:
                print("%s | %-8s | %s | %s"
                      % (e["id"], e.get("productMode") or "-",
                         (e.get("updatedAt") or "")[:19],
                         (e.get("title") or "").replace("\n", " ")[:70]))
        elif cmd == "search":
            jdump(await api.search(a.text, limit=a.limit or 20))
        elif cmd in ("get", "md"):
            tr = await api.transcript_full(a.id, limit=50)
            print("сообщений: %d (страниц: %s)" % (len(tr["messages"]),
                                                    tr.get("pages")))
            if cmd == "md":
                data = to_markdown(tr, a.id)
            else:
                data = json.dumps(tr, ensure_ascii=False, indent=1)
            if a.out:
                open(a.out, "w").write(data)
                print("записано:", a.out)
            else:
                print(data[:a.max])
        elif cmd == "files":
            jdump(await api.workspace_latest(a.id), a.max)
        elif cmd == "cat":
            r = await api.workspace_file(a.id, a.node, path=a.path)
            print(r["status"])
            print((r["body"] or "")[:a.max])
        elif cmd == "cost":
            jdump(await api.cost(a.id), a.max)
        elif cmd == "archive":
            jdump(await api.archive(a.id))
        elif cmd == "unarchive":
            jdump(await api.unarchive(a.id))
        elif cmd == "delete":
            jdump(await api.delete(a.id))
        elif cmd == "stop":
            jdump(await api.stop(a.id))
        elif cmd == "wait":
            tr = await wait_idle(api, a.id, timeout=a.timeout, verbose=a.verbose)
            last = (tr.get("messages") or [{}])[-1]
            lm = light_message(last)
            print("\n=== последний ответ (%s) ===\n%s" % (last.get("role"),
                                                           (lm.get("text") or "")[:4000]))
        elif cmd == "stream":
            st = api.stream_state((await api.stream_out(
                a.id, max_seconds=a.seconds,
                last_event_id=a.last_event_id)).get("events") or [])
            print("ход завершён: %s | событий: %d | lastEventId: %s"
                  % (st["turnComplete"], len(st["events"]), st.get("lastEventId")))
            for t in st["tools"]:
                print("  инструмент: %s (%s)" % (t["toolName"], t.get("state")))
            print("\n=== текст ===\n%s" % (st["text"] or "(пусто)"))
            if a.verbose and st["reasoning"]:
                print("\n=== рассуждение ===\n%s" % st["reasoning"][:2000])
        elif cmd == "rename":
            r = await api.rename(a.id, a.text or "")
            print(r["status"], (r["body"] or "")[:300])
        elif cmd == "token":
            print(await api.trigger_token(a.id))
        elif cmd == "upload":
            data = open(a.id, "rb").read()
            jdump(await api.upload(data))
        elif cmd == "create":
            r = await api.create_chat(a.text, timezone=a.tz)
            print("создан чат:", json.dumps(r, ensure_ascii=False))
            cid = r.get("id")
            if cid:
                print("URL: https://arena.ai/agent/%s" % cid)
                if a.wait:
                    await wait_idle(api, cid, timeout=a.timeout, verbose=True)
        elif cmd == "send":
            r = await api.send_message(a.id, a.text, timezone=a.tz)
            print("статус:", r["status"])
            print((r["body"] or "")[:a.max])
            if r["status"] < 400 and a.wait:
                await wait_idle(api, a.id, timeout=a.timeout, verbose=True)
        else:
            print("неизвестная команда")
    finally:
        if api.own_tab and tab.target_id:
            import urllib.request
            try:
                urllib.request.urlopen("http://127.0.0.1:9222/json/close/"
                                       + tab.target_id, timeout=10)
            except Exception:
                pass
        else:
            await tab.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd")
    ap.add_argument("id", nargs="?", help="id чата")
    ap.add_argument("text", nargs="?", help="текст сообщения / поисковый запрос")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--archived", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--max", type=int, default=4000)
    ap.add_argument("--node")
    ap.add_argument("--path")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--tz", default="Europe/Kiev")
    ap.add_argument("--seconds", type=int, default=30, help="для stream")
    ap.add_argument("--last-event-id", default=None, help="для stream: продолжить")
    ap.add_argument("--use-current-tab", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    # для search/create/send текст может прийти вторым аргументом
    if a.cmd in ("search", "create", "rename") and a.text is None and a.id:
        a.text, a.id = a.id, None
    asyncio.run(main_async(a))


if __name__ == "__main__":
    main()
