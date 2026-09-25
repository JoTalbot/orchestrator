#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор всех чатов Arena AI (Agent Mode) через внутренний API arena.ai.

Официального API у Arena нет, а веб-интерфейс закрыт Cloudflare + reCAPTCHA
Enterprise: прямой HTTP с IP дата-центра получает 429 «Just a moment...».
Поэтому все запросы выполняет САМА вкладка arena.ai в браузере (CDP :9222,
контейнер liza-browser) — у неё правильные куки (cf_clearance), TLS-отпечаток
и доступ к grecaptcha.enterprise.

Что делает:
  * пагинация по всему списку чатов: GET /api/history/unified;
  * выгрузка каждого чата: последняя страница сообщений берётся из
    RSC-пейлоада /agent/{id}, более ранние — GET /api/chat/{id}/messages?cursor=;
  * возобновляемость: скачанные и не изменившиеся чаты пропускаются
    (сверка по updatedAt), индекс пишется каждые 10 чатов;
  * атомарная запись (tmp + rename) — можно прерывать в любой момент.

Выходные данные (--data-dir, по умолчанию data/arena/):
  index.json      — индекс всех чатов (id, title, createdAt, updatedAt, режим)
  chats/{id}.json — полный транскрипт (сообщения, части, tool-вызовы с выводом)
  light/{id}.json — выжимка: роль, текст, список инструментов
  errors.json     — чаты, которые не удалось скачать
  summary.json    — итоговая статистика

Требования:
  * в браузере (CDP :9222) открыта и залогинена вкладка https://arena.ai;
  * python-пакет websockets (уже стоит в .venv).

Примеры:
  .venv/bin/python arena_export/export_chats.py --limit 3          # тест
  .venv/bin/python arena_export/export_chats.py                    # всё
  .venv/bin/python arena_export/export_chats.py --only-meta        # только список
  .venv/bin/python arena_export/export_chats.py --chat-id 019f...   # один чат
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "arena_agent"))
from arena_api import ArenaAPI, connect, light_message, unpack_rsc, parse_transcript  # noqa: E402

ROOT = Path("/opt/orchestrator")


async def api_close(api):
    """Закрыть свою вкладку (чужую не трогаем)."""
    try:
        if api.own_tab and api.tab.target_id:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:9222/json/close/"
                                   + api.tab.target_id, timeout=10)
            log("своя вкладка закрыта")
        else:
            await api.tab.close()
    except Exception as e:
        log("не закрыл вкладку: %s" % e)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def save_json_atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def already_done(index_entry, data_dir: Path, force: bool, skip_existing: bool):
    """Уже сохранённый чат не перекачиваем.

    Пропускаем, если файл есть, он цел и
      * он не старше, чем updatedAt из индекса (обычный режим), либо
      * задан --skip-existing (не трогать сохранённое вообще никогда).
    """
    if force:
        return False
    p = data_dir / "chats" / ("%s.json" % index_entry["id"])
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text())
        light = json.loads((data_dir / "light" / p.name).read_text())
        if light.get("id") != d.get("id"):
            return False
    except Exception:
        return False                      # битый файл — перекачаем
    if not d.get("id"):
        return False                      # файл не наш — перекачаем
    # сообщений может legitimately не быть (картиночные/пустые чаты) —
    # файл пишется атомарно после успешной загрузки, значит чат сохранён
    if skip_existing:
        return True
    return d.get("updatedAt") == index_entry.get("updatedAt")


class Lock:
    """Защита от двух одновременных экспортов (вкладка браузера одна)."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        if self.path.exists():
            try:
                old = json.loads(self.path.read_text())
                pid = int(old.get("pid", 0))
                os.kill(pid, 0)
                raise SystemExit("экспорт уже запущен (PID %d, лог %s). "
                                 "Снимите блокировку: rm %s"
                                 % (pid, old.get("log", "?"), self.path))
            except (ProcessLookupError, PermissionError, ValueError):
                pass                      # мертвый lock — перезапишем
            except OSError:
                pass
        self.path.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}))
        return self

    def __exit__(self, *a):
        try:
            self.path.unlink()
        except Exception:
            pass


async def export_one(api: ArenaAPI, entry, data_dir: Path):
    cid = entry["id"]
    t0 = time.time()
    if entry.get("type") == "evaluation":
        response = await api.fetch("GET", "/c/" + cid,
                                   headers={"accept": "text/html"})
        if response.get("status") != 200:
            raise RuntimeError("evaluation page HTTP %s" % response.get("status"))
        tr = parse_transcript(unpack_rsc(response.get("body", "")))
        if tr is None:
            raise RuntimeError("evaluation transcript not found; refusing empty export")
        if (tr.get("pagination") or {}).get("hasMore"):
            raise RuntimeError("evaluation transcript requires pagination; refusing partial export")
    else:
        tr = await api.transcript_full(cid, limit=50)
    msgs = tr.get("messages") or []
    full = {
        "id": cid,
        "title": entry.get("title"),
        "type": entry.get("type"),
        "productMode": entry.get("productMode"),
        "codingSessionStatus": entry.get("codingSessionStatus"),
        "createdAt": entry.get("createdAt"),
        "updatedAt": entry.get("updatedAt"),
        "archivedAt": entry.get("archivedAt"),
        "messageCount": len(msgs),
        "messages": msgs,
        "session": tr.get("session"),
    }
    save_json_atomic(data_dir / "chats" / ("%s.json" % cid), full)
    light = {k: full[k] for k in ("id", "title", "productMode", "createdAt",
                                  "updatedAt", "messageCount")}
    light["messages"] = [light_message(m) for m in msgs]
    save_json_atomic(data_dir / "light" / ("%s.json" % cid), light)
    return len(msgs), time.time() - t0


async def main_async(args):
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    with Lock(data_dir / ".export.lock"):
        await run_export(args, data_dir)


async def run_export(args, data_dir):
    tab = await connect(verbose=args.verbose, own=not args.use_current_tab)
    # Своя вкладка должна закрываться при ЛЮБОМ выходе из функции, включая
    # досрочный return у --only-meta (строка была: return -> вкладка оставалась)
    # и падение конструктора API (раньше стоял до try).
    api = None          # иначе finally словит UnboundLocalError, если конструктор упадёт
    try:
        api = ArenaAPI(tab, verbose=args.verbose, own_tab=not args.use_current_tab)
        return await _run_export_body(api, args, data_dir)
    finally:
        if api is None:
            # конструктор не отработал — вкладки уже нет в API, закрываем напрямую
            try:
                import urllib.request
                if tab.target_id:
                    urllib.request.urlopen("http://127.0.0.1:9222/json/close/"
                                           + tab.target_id, timeout=10)
                    log("вкладка закрыта напрямую (конструктор API не отработал)")
            except Exception as ex:
                log("не закрыл вкладку: %s" % ex)
        else:
            await api_close(api)


async def _run_export_body(api, args, data_dir):
    me = await api.me()
    log("аккаунт: %s" % me.get("user", {}).get("email"))
    try:
        log("кредиты: %s" % json.dumps(await api.balance(), ensure_ascii=False))
    except Exception as e:
        log("баланс недоступен: %s" % e)

    if args.chat_id:
        entries = [{"id": args.chat_id, "title": "(one)", "type": "agentic",
                    "productMode": None, "createdAt": None, "updatedAt": None,
                    "archivedAt": None, "codingSessionStatus": None}]
    else:
        log("читаю список чатов ...")
        entries = await api.history_all(limit=50,
                                        include_archived=args.include_archived,
                                        max_chats=args.limit or None)
        uniq, seen_ids = [], set()
        for e in entries:
            if e["id"] in seen_ids:
                continue
            seen_ids.add(e["id"])
            uniq.append(e)
        if len(uniq) != len(entries):
            log("в списке были дубли: %d → %d" % (len(entries), len(uniq)))
        entries = uniq
        log("в списке чатов: %d" % len(entries))
        save_json_atomic(data_dir / "index.json",
                         {"savedAt": int(time.time()), "count": len(entries),
                          "entries": entries})
        if args.only_meta:
            log("--only-meta: готово")
            return

    if args.limit and not args.chat_id:
        entries = entries[:args.limit]

    errors, done, skipped, total_msgs = [], 0, 0, 0
    seen = set()
    for i, e in enumerate(entries, 1):
        cid = e["id"]
        if cid in seen:
            skipped += 1
            continue                                   # дубль в истории
        seen.add(cid)
        if already_done(e, data_dir, args.force, args.skip_existing):
            skipped += 1
            log("[%d/%d] %s — уже сохранён, пропускаю" % (i, len(entries), cid[:13]))
            continue
        try:
            n, dt = await export_one(api, e, data_dir)
            total_msgs += n
            done += 1
            log("[%d/%d] %s — %d сообщений за %.1f с | %s"
                % (i, len(entries), cid[:13], n, dt,
                   (e.get("title") or "").replace("\n", " ")[:52]))
        except Exception as ex:
            errors.append({"id": cid, "error": "%s: %s" % (type(ex).__name__, ex)})
            log("[%d/%d] %s — ОШИБКА %s" % (i, len(entries), cid[:13], ex))
        if i % 10 == 0:
            save_json_atomic(data_dir / "errors.json", errors)
        await asyncio.sleep(args.sleep)

    save_json_atomic(data_dir / "errors.json", errors)
    summary = {"savedAt": int(time.time()), "chats": len(entries),
               "downloaded": done, "skipped": skipped, "errors": len(errors),
               "messagesThisRun": total_msgs, "apiCalls": api.calls}
    save_json_atomic(data_dir / "summary.json", summary)
    log("готово: %s" % json.dumps(summary, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="Экспорт чатов Arena AI")
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "arena"))
    ap.add_argument("--limit", type=int, default=0, help="0 = все чаты")
    ap.add_argument("--chat-id", help="выгрузить один чат")
    ap.add_argument("--force", action="store_true", help="перекачать уже скачанные")
    ap.add_argument("--skip-existing", action="store_true",
                    help="не трогать уже сохранённые чаты, даже если они изменились")
    ap.add_argument("--only-meta", action="store_true", help="только список чатов")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--use-current-tab", action="store_true",
                    help="не открывать свою вкладку, работать в текущей arena.ai")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
