#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_api.py — полный поиск эндпоинтов arena.ai по JS-бандлам + зондирование.

Что ищет:
  1. вызовы типизированного RPC-клиента (`F.chat[":id"].messages.$get(...)`) —
     клиент собран как hc("/api"), поэтому цепочка свойств = маршрут;
  2. строковые и шаблонные литералы `/api/...`, `/nextjs-api/...`,
     `/ai-proxy/...`, `/realtime/v1/...`, `https://api.trigger.dev/...`;
  3. вызовы `fetch(...)` с явным URL и методом.

Режимы:
  scan_api.py scan                — собрать карту в api_map.json + API_MAP.md
  scan_api.py scan --download     — сначала обновить кэш бандлов (.jscache)
  scan_api.py probe [--chat-id X] — осторожно дёрнуть GET-маршруты из вкладки
                                    и проставить реальные статус-коды

Зондируются ТОЛЬКО безопасные методы: GET (и HEAD). Маршруты со словами
delete/stop/sign-out/disconnect/create/archive не трогаем вовсе.
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".jscache"
OUT_JSON = HERE / "api_map.json"
OUT_MD = HERE.parent / "docs" / "ARENA_API.md"
NOTES = HERE / "api_notes.json"          # ручные пометки: "METHOD path" -> текст
MANUAL = HERE / "manual_routes.json"     # маршруты, которых нет в бандлах как литералы

RPC_HEAD = re.compile(r"\.F\.")
SEG_PLAIN = re.compile(r"[A-Za-z0-9_$]+")
SEG_BRACKET = re.compile(r'\["([^"]+)"\]')
LITERAL = re.compile(r"""["'`](/(?:api|nextjs-api|ai-proxy|realtime)/[^"'`\s]{0,120})["'`]""")
ABSOLUTE = re.compile(r"""["'`](https://[a-z0-9.\-]*(?:arena\.ai|trigger\.dev)/[^"'`\s]{0,120})["'`]""")
FETCH = re.compile(r"""fetch\(\s*([`"'])([^`"']{2,140})\1\s*(?:,\s*\{\s*method:\s*"([A-Z]+)")?""")
FETCH_TPL = re.compile(r"fetch\(`([^`]{2,160})`\s*(?:,\s*\{\s*method:\s*\"([A-Z]+)\")?")

DANGEROUS = ("delete", "stop", "sign-out", "signout", "disconnect", "archive",
             "unarchive", "create", "rerun", "resample", "retry", "remove")


def norm(seg):
    """:id / :manifestNodeId → {id} / {manifestNodeId}"""
    if seg.startswith(":"):
        return "{" + seg[1:] + "}"
    if seg.startswith("$"):
        return None
    return seg


def parse_rpc(blob, start):
    """Разбирает цепочку свойств после `.F.` → (method, path, usage).

    Цепочка выглядит как `history.unified.$get` или
    `chat[":id"].workspace[":manifestNodeId"].dir.$get`.
    """
    m = SEG_PLAIN.match(blob, start)
    if not m:
        return None
    segs = [m.group(0)]
    i = m.end()
    while i < len(blob):
        if blob[i] == ".":
            m2 = SEG_PLAIN.match(blob, i + 1)
            if not m2:
                break
            segs.append(m2.group(0))
            i = m2.end()
            continue
        if blob[i] == "[":
            m2 = SEG_BRACKET.match(blob, i)
            if not m2:
                break
            segs.append(m2.group(1))
            i = m2.end()
            continue
        break
    if not segs or not segs[-1].startswith("$"):
        return None
    method = segs[-1][1:].lower()
    route = [norm(s) for s in segs[:-1]]
    if any(r is None for r in route):
        return None
    usage = blob[i:i + 320].replace("\n", " ")
    return method, "/api/" + "/".join(route), usage


def scan(chunks):
    found = {}

    def add(method, path, kind, chunk, usage=""):
        path = path.split("?")[0]
        path = re.sub(r"\$\{[^}]*\}", "{param}", path)
        path = path.rstrip(".")
        if not path or len(path) < 4:
            return
        key = (method.upper(), path)
        rec = found.setdefault(key, {"method": method.upper(), "path": path,
                                     "kinds": set(), "chunks": set(),
                                     "usage": usage[:300]})
        rec["kinds"].add(kind)
        rec["chunks"].add(chunk)
        if usage and not rec["usage"]:
            rec["usage"] = usage[:300]

    for f in sorted(chunks):
        blob = f.read_text(errors="replace")
        name = f.name[:40]
        for m in RPC_HEAD.finditer(blob):
            r = parse_rpc(blob, m.end())
            if r:
                add(r[0], r[1], "rpc", name, r[2])
        for m in LITERAL.finditer(blob):
            add("GET", m.group(1), "literal", name)
        for m in ABSOLUTE.finditer(blob):
            add("GET", m.group(1), "literal", name)
        for m in FETCH_TPL.finditer(blob):
            add(m.group(2) or "GET", m.group(1), "fetch", name)
        for m in FETCH.finditer(blob):
            u = m.group(2)
            if u.startswith("/") or u.startswith("http"):
                add(m.group(3) or "GET", u, "fetch", name)
    skip = ("/blog/", "/company/", "status.arena.ai", "help.arena.ai",
            "trigger.dev/contact", "firecrawl", "/api/v2/", "/careers")
    out = []
    for (meth, path), rec in found.items():
        if any(k in path for k in skip):
            continue
        rec["kinds"] = sorted(rec["kinds"])
        rec["chunks"] = sorted(rec["chunks"])[:3]
        out.append(rec)
    out.sort(key=lambda r: (r["path"], r["method"]))
    return out


def download_chunks():
    from concurrent.futures import ThreadPoolExecutor
    from curl_cffi import requests as cfr
    import urllib.request
    ORIGIN = "https://arena.ai"
    CACHE.mkdir(parents=True, exist_ok=True)
    s = cfr.Session(impersonate="chrome")
    CH = re.compile(r"/_next/static/chunks/[^\"\\ '<>]+?\.js")
    seen, queue = set(), []
    pages = ["/", "/agent", "/history/search", "/coding", "/text/direct",
             "/text/side-by-side", "/leaderboard/agent", "/settings", "/billing"]
    # id любого чата — чтобы подтянуть бандлы страницы чата
    idx = HERE.parent / "data" / "arena" / "index.json"
    if idx.exists():
        try:
            e = json.loads(idx.read_text())["entries"][0]
            pages.append("/agent/" + e["id"])
        except Exception:
            pass
    for p in pages:
        try:
            r = s.get(ORIGIN + p, timeout=60)
            for m in CH.finditer(r.text):
                u = ORIGIN + m.group(0)
                if u not in seen:
                    seen.add(u)
                    queue.append(u)
        except Exception as e:
            print("  ERR", p, e)

    def dl(u):
        try:
            r = s.get(u, timeout=90)
            n = re.sub(r"[^A-Za-z0-9_.-]", "_", u.split("/chunks/")[-1].split("?")[0])
            (CACHE / n).write_text(r.text)
            return True
        except Exception:
            return False
    with ThreadPoolExecutor(6) as ex:
        ok = sum(1 for x in ex.map(dl, queue) if x)
    print("бандлов в кэше: %d (%.1f МБ)" % (len(list(CACHE.glob('*.js'))),
          sum(f.stat().st_size for f in CACHE.glob("*.js")) / 1e6))
    return ok


PRIVACY = re.compile(r"^(2\d\d)\s+[\{\[<\"a-zA-Z0-9]")


def sanitize_probe(probe):
    """Тела успешных ответов в карту не пишем: там личные данные аккаунта
    (email, заголовки чатов, репозитории). Остаётся только код статуса."""
    if not probe:
        return probe
    if probe.startswith("skip") or probe.startswith("ERR"):
        return probe[:120]
    m = re.match(r"^(\d{3})\s+(.*)$", probe, re.S)
    if not m:
        return probe[:120]
    code, body = m.group(1), m.group(2)
    if code.startswith("2") and body[:1] in "{[<\"":
        return code + " (тело не публикуем: личные данные)"
    if code.startswith(("4", "5")):
        return code + " " + body[:120]
    return code + " " + body[:60]


def write_docs(entries):
    for e in entries:
        if e.get("probe"):
            e["probe"] = sanitize_probe(e["probe"])
    OUT_JSON.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Карта API arena.ai (реверс-инжиниринг)", "",
             "Собрано автоматически `arena_agent/scan_api.py` %s из %d JS-бандлов."
             % (time.strftime("%Y-%m-%d %H:%M"), len(list(CACHE.glob('*.js')))), "",
             "Колонка «статус» — результат зондирования GET-запросом из вкладки "
             "браузера (`scan_api.py probe`): `200` работает, `400/422` маршрут есть, "
             "но нужны параметры, `401/403` нужен контекст/права, `404` — нет такого "
             "маршрута (или нужен другой метод).", "",
             "| Метод | Путь | Источник | Статус | Заметка |",
             "|---|---|---|---|---|"]
    notes = json.loads(NOTES.read_text()) if NOTES.exists() else {}
    for e in entries:
        note = notes.get("%s %s" % (e["method"], e["path"]), "")
        lines.append("| %s | `%s` | %s | %s | %s |"
                     % (e["method"], e["path"], ",".join(e["kinds"]),
                        e.get("probe") or "", note))
    lines += ["", "## Контекст вызовов (как их дёргает веб-клиент)", ""]
    for e in entries:
        if e.get("usage"):
            lines.append("### %s %s" % (e["method"], e["path"]))
            lines.append("```js")
            lines.append(e["usage"][:280])
            lines.append("```")
            lines.append("")
    OUT_MD.write_text("\n".join(lines))
    print("записано:", OUT_JSON, "и", OUT_MD)


async def probe(entries, chat_id):
    sys.path.insert(0, str(HERE))
    from arena_api import ArenaAPI, connect
    tab = await connect(own=True, verbose=True)
    # Вкладка своя — закрывать обязаны даже если конструктор API или
    # зондирование упадут (конструктор раньше стоял до try: падал — вкладка текла).
    import urllib.request
    try:
        api = ArenaAPI(tab, verbose=False, own_tab=True)
        return await _probe_with(api, entries, chat_id)
    finally:
        if tab.target_id:
            try:
                urllib.request.urlopen("http://127.0.0.1:9222/json/close/"
                                       + tab.target_id, timeout=10)
            except Exception:
                pass


async def _probe_with(api, entries, chat_id):
    subs = {"{id}": chat_id, "{param}": "00000000-0000-0000-0000-000000000000",
            "{manifestNodeId}": "00000000-0000-0000-0000-000000000000",
            "{messageId}": "00000000-0000-0000-0000-000000000000"}
    n = 0
    for e in entries:
        p = e["path"]
        low = p.lower()
        if any(d in low for d in DANGEROUS):
            e["probe"] = "skip (небезопасно зондировать)"
            continue
        if p.startswith("http"):
            e["probe"] = "skip (внешний хост)"
            continue
        url = p
        for k, v in subs.items():
            url = url.replace(k, v)
        if "{" in url:
            e["probe"] = "skip (неизвестный параметр)"
            continue
        try:
            r = await api.fetch("GET", url, timeout=60, retries=1)
            body = (r.get("body") or "")[:160].replace("\n", " ")
            e["probe"] = "%s %s" % (r["status"], body[:90])
        except Exception as ex:
            e["probe"] = "ERR %s" % str(ex)[:80]
        n += 1
        print("  [%d] %-6s %-58s %s" % (n, e["method"], url[:58],
                                        e["probe"][:70]), flush=True)
        await asyncio.sleep(0.3)
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "probe"])
    ap.add_argument("--download", action="store_true",
                    help="обновить кэш JS-бандлов перед сканом")
    ap.add_argument("--chat-id", help="реальный id чата для подстановки в {id}")
    a = ap.parse_args()

    if a.mode == "scan" or a.download:
        if a.download or not list(CACHE.glob("*.js")):
            download_chunks()
    entries = scan(list(CACHE.glob("*.js")))
    if MANUAL.exists():
        have = {"%s %s" % (e["method"], e["path"]) for e in entries}
        for m in json.loads(MANUAL.read_text()):
            key = "%s %s" % (m["method"], m["path"])
            if key in have:
                continue
            m.setdefault("kinds", ["manual"])
            m.setdefault("usage", "")
            entries.append(m)
        entries.sort(key=lambda e: (e["path"], e["method"]))
        print("добавлено ручных маршрутов: %d"
              % len(json.loads(MANUAL.read_text())))
    if OUT_JSON.exists():
        prev = {"%s %s" % (x["method"], x["path"]): x
                for x in json.loads(OUT_JSON.read_text())}
        kept = 0
        for e in entries:
            old_e = prev.get("%s %s" % (e["method"], e["path"]))
            if not old_e:
                continue
            for k in ("probe", "body"):
                if old_e.get(k) and not e.get(k):
                    e[k] = old_e[k]
                    kept += 1
        if kept:
            print("перенесено из прошлой карты полей: %d" % kept)
    print("найдено маршрутов: %d" % len(entries))

    if a.mode == "probe":
        if not a.chat_id:
            idx = HERE.parent / "data" / "arena" / "index.json"
            if idx.exists():
                a.chat_id = json.loads(idx.read_text())["entries"][0]["id"]
        if not a.chat_id:
            print("нужен --chat-id")
            return
        prev = {}
        if OUT_JSON.exists():
            for e in json.loads(OUT_JSON.read_text()):
                prev[(e["method"], e["path"])] = e
        entries = asyncio.run(probe(entries, a.chat_id))
    write_docs(entries)


if __name__ == "__main__":
    main()
