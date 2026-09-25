#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discover_actions.py — добыть актуальные id React Server Actions с живой arena.ai.

Зачем
-----
gateway вызывает действия арены (удаление/архивация сессий) по id, которые
Next.js генерирует на каждый деплой. Id были захардкожены в engine.py, арена
задеплоилась — и cleanup стал падать со 100%-ной частотой:
`404 Server action not found` (замер: cleanup_fail 35 при ok 35).

Как
---
В бандлах действие объявлено так (проверено на живых файлах 19.09.2026):

    createServerReference("60d8f9be...",s.callServer,void 0,
                          s.findSourceMapURL,"deleteEvaluationSession")

То есть id — первый аргумент, имя — пятый. Регулярка берёт их парой, поэтому
в отличие от поиска «любой hex рядом с именем» даёт ровно один id на действие.

Доступ
------
Прямой HTTP с дата-центрового IP арена блокирует Cloudflare, поэтому запросы
идут через тот же socks-прокси, что использует браузер оркестратора.

Использование
-------------
    python3 discover_actions.py --print              # только показать
    python3 discover_actions.py --write              # обновить server_actions.json
    python3 discover_actions.py --write --diff       # и показать, что изменилось
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ORIGIN = "https://arena.ai"
ENTRY_PAGES = ("/agent", "/text/direct")
CHUNK_RE = re.compile(r"/_next/static/chunks/[A-Za-z0-9._/%-]+\.js")

# Разбор идёт в два шага, и это принципиально.
#
# В минифицированном бандле вызов обёрнут:
#   (0,s.createServerReference)("60d8f9be...",s.callServer,void 0,
#                               s.findSourceMapURL,"deleteEvaluationSession")
# то есть сразу после имени идёт ")" обёртки, а уже потом "(" аргументов.
#
# Первая версия искала "createServerReference(" — 0 совпадений на реальных бандлах.
# Вторая разрешила произвольную вставку через [^()]{0,80} — и перестала ловить
# вообще всё, потому что в вставке как раз есть скобки. Один паттерн здесь
# либо слишком узкий, либо начинает прыгать через несколько вызовов и приписывать
# id чужому действию. Поэтому:
#   NAME_RE  — находит само упоминание имени (отдельно, без скобок);
#   ARGS_RE  — разбирает список аргументов, начинающийся сразу после него.
NAME_RE = re.compile(r"createServerReference")

# Дойти до открывающей скобки аргументов, НЕ потребляя её.
#
# Lookahead здесь обязателен. Без него регулярка съедала саму "(", и ARGS_RE
# начинал разбор уже после скобки — с кавычки id — и не совпадал.
#
# Вставка у обёртки — это ")"; открывающая скобка внутри запрещена, иначе
# регулярка ушла бы на соседний вызов и приписала id чужому действию.
# Первая версия с [^()] не ловила вообще ничего, потому что запрещала и ")".
WRAPPER_RE = re.compile(r"[^\n(]{0,80}(?=\()")

# id — первый аргумент, имя — пятый; между ними callServer, void 0, findSourceMapURL.
# БЕЗ якоря "^" — и это принципиально.
# Разбор идёт через ARGS_RE.match(text, pos), а в Python "^" при ненулевом pos
# НЕ совпадает: якорь привязан к началу строки, а не к позиции. С "^" паттерн
# проходил в проверке re.match(pat, text[i:]) и молча возвращал None в
# re.match(pat, text, i) — ровно та ошибка, которую я и поймал.
# match() и так привязан к pos, поэтому якорь не нужен.
ARGS_RE = re.compile(
    r"""\s*\(\s*["']([0-9a-f]{32,64})["']\s*,"""
    r"""[^,]*,\s*void 0\s*,\s*[^,]*,\s*["']([A-Za-z0-9_$]+)["']\s*\)"""
)


def extract(text: str, out: dict[str, set[str]]) -> None:
    """Разобрать бандл: {имя действия: {id, ...}}.

    Каждый id привязан ровно к тому действию, в чьём списке аргументов он стоит,
    потому что аргументы берутся от скобки сразу после имени, а не из окна
    «где-то рядом» — окно давало по 2–3 кандидата на действие.

    Вставка между именем и "(" ограничена 80 символами без скобок: этого хватает
    на обёртку ")(..." и не даёт перескочить на аргументы соседнего вызова
    (проверено тестом test_does_not_jump_across_calls).
    """
    for m in NAME_RE.finditer(text):
        gap = WRAPPER_RE.match(text, m.end())
        if not gap:
            continue
        am = ARGS_RE.match(text, gap.end())
        if not am:
            continue
        aid, name = am.group(1), am.group(2)
        out.setdefault(name, set()).add(aid)

# действия, которые использует оркестратор; прочее тоже сохраняем — пригодится
WANTED = (
    "deleteEvaluationSession",
    "archiveEvaluationSession",
    "unarchiveEvaluationSession",
    "generateUploadUrl",
    "getSignedUrl",
    "createPairwiseFeedback",
    "getLeaderboardModels",
)

# их трогать не нужно, но в отчёде показываем отдельно: вызов по ошибке опасен
DANGEROUS = ("deleteAccount", "saveSecrets", "updateMarketingConsent")

DEFAULT_PROXY = "socks5h://127.0.0.1:1080"
DEFAULT_OUT = "/opt/orchestrator/data/arena/server_actions.json"


def curl(url: str, out_path: str | None, proxy: str, timeout: int = 40) -> tuple[int, bytes]:
    """Скачать через socks-прокси. Возвращает (код, тело).

    urllib тоже умеет socks, но требует PySocks; curl уже стоит и настроен
    тем же прокси, что и браузер, поэтому расхождений в отпечатке нет.
    """
    cmd = ["curl", "-s", "-m", str(timeout), "-A",
           "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/152.0 Safari/537.36",
           "-w", "\n%{http_code}", url]
    if proxy:
        cmd[1:1] = ["--proxy", proxy]
    if out_path:
        cmd[1:1] = ["-o", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    body = proc.stdout or ""
    code = 0
    if "\n" in body:
        body, _, tail = body.rpartition("\n")
        try:
            code = int(tail.strip())
        except ValueError:
            code = 0
    return code, body.encode("utf-8", "replace")


def collect_chunks(proxy: str, verbose: bool = False) -> list[str]:
    """Список URL чанков со всех стартовых страниц (без дублей)."""
    found: dict[str, None] = {}
    for page in ENTRY_PAGES:
        code, body = curl(ORIGIN + page, None, proxy)
        if code != 200:
            if verbose:
                print("  %s -> HTTP %s (пропускаю)" % (page, code))
            continue
        for chunk in CHUNK_RE.findall(body.decode("utf-8", "replace")):
            found.setdefault(ORIGIN + chunk, None)
        if verbose:
            print("  %s -> HTTP %s, всего чанков %d" % (page, code, len(found)))
    return list(found)


def discover(proxy: str, workers: int = 8, verbose: bool = False) -> dict[str, list[str]]:
    urls = collect_chunks(proxy, verbose)
    if not urls:
        raise RuntimeError("не удалось получить список чанков — арена не ответила")
    found: dict[str, set[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        def one(args):
            i, url = args
            path = os.path.join(tmp, "%03d.js" % i)
            code, _ = curl(url, path, proxy)
            if code != 200:
                return 0
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    extract(fh.read(), found)
            except OSError:
                return 0
            return 1

        with ThreadPoolExecutor(max_workers=workers) as pool:
            ok = sum(pool.map(one, enumerate(urls)))
    if verbose:
        print("  скачано %d из %d чанков" % (ok, len(urls)))
    if not found:
        raise RuntimeError("в чанках не нашлось ни одного createServerReference — "
                           "арена могла сменить формат сборки")
    return {k: sorted(v) for k, v in sorted(found.items())}


def load_existing(path: str) -> dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, list) and v:
            out[k] = v[0]
        elif isinstance(v, str):
            out[k] = v
    return out


def write_actions(path: str, actions: dict[str, list[str]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(actions, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Актуальные id Server Actions arena.ai")
    ap.add_argument("--proxy", default=DEFAULT_PROXY,
                    help="socks-прокси (по умолчанию %(default)s; пусто = напрямую)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="куда писать (по умолчанию %(default)s)")
    ap.add_argument("--write", action="store_true", help="обновить файл")
    ap.add_argument("--print", dest="do_print", action="store_true", help="показать таблицу")
    ap.add_argument("--diff", action="store_true", help="показать, что изменилось")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    if not (args.write or args.do_print or args.diff):
        args.do_print = True

    try:
        actions = discover(args.proxy, args.workers, verbose=True)
    except Exception as exc:
        print("ОШИБКА: %s" % exc, file=sys.stderr)
        return 1

    old = load_existing(args.out)
    current = {k: (v[0] if v else "") for k, v in actions.items()}

    missing = [n for n in WANTED if n not in current]
    ambiguous = {k: v for k, v in actions.items() if len(v) > 1}

    if args.do_print or args.diff:
        print("\n%-30s %-9s %s" % ("действие", "статус", "id"))
        for name in sorted(set(list(WANTED) + list(old))):
            new = current.get(name)
            prev = old.get(name)
            if not new:
                status = "НЕТ"
                shown = prev or "-"
            elif not prev:
                status, shown = "новый", new
            elif prev == new:
                status, shown = "тот же", new
            else:
                status, shown = "СМЕНІВСЯ" if False else "СМЕНИЛСЯ", "%s -> %s" % (prev, new)
            print("%-30s %-9s %s" % (name, status, shown))
        others = sorted(set(current) - set(WANTED) - set(old))
        if others:
            print("\nпрочих действий найдено: %d" % len(others))
            for name in others:
                mark = "  (ОСТОРОЖНО)" if name in DANGEROUS else ""
                print("  %-28s %s%s" % (name, current[name], mark))

    if ambiguous:
        print("\nВНИМАНИЕ: у этих действий больше одного id — нужна ручная проверка:",
              ", ".join(sorted(ambiguous)), file=sys.stderr)

    if missing:
        print("\nВНИМАНИЕ: не найдены нужные действия: %s" % ", ".join(missing),
              file=sys.stderr)

    if args.write:
        if missing and not any(n in current for n in WANTED):
            print("не пишу: не найдено ни одного нужного действия", file=sys.stderr)
            return 1
        write_actions(args.out, actions)
        changed = sum(1 for n in WANTED if old.get(n) and old[n] != current.get(n))
        print("\nзаписано %s: действий %d, сменилось %d из нужных"
              % (args.out, len(actions), changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
