#!/usr/bin/env python3
"""
Уборщик осиротевших вкладок в host-chrome-proxy (CDP :9222).

Причина появления: вкладка закрывалась только websocket-ом (CDPTab.close()),
а не через /json/close, поэтому на каждую неудачную проверку здоровья
arena-api.service в браузере оставалась вкладка вместе с renderer и worker.

ВАЖНО про определение «своей» вкладки:
  /json/list этого Chrome НЕ отдаёт createdAt/attached/openerId
  (проверено: ключи = description, devtoolsFrontendUrl, faviconUrl, id,
  parentId, title, type, url, webSocketDebuggerUrl). Определять старейшую
  вкладку нельзя — поэтому закрываем только точное совпадение с шаблоном
  утечки и никогда не трогаем остальные.

Штатная вкладка сервиса: host-chrome-proxy.service ExecStart открывает
https://arena.ai/text/direct — под шаблон утечки (/agent) она не попадает.

Запуск:
  python3 chrome_tab_janitor.py --dry-run
  python3 chrome_tab_janitor.py
  python3 chrome_tab_janitor.py --url-pattern arena.ai/agent --keep 1
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

CDP = "http://127.0.0.1:9222"
DEFAULT_PATTERN = "arena.ai/agent"
# эти URL не закрываем никогда, даже если совпали с шаблоном
DEFAULT_PROTECT = ("arena.ai/text/direct",)


def targets() -> list[dict]:
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--url-pattern", default=DEFAULT_PATTERN,
                    help="подстрока URL вкладок-кандидатов (по умолчанию %(default)s)")
    ap.add_argument("--protect", action="append", default=None,
                    help="подстрока URL, которую нельзя закрывать (можно несколько)")
    ap.add_argument("--keep", type=int, default=0,
                    help="сколько подходящих вкладок оставить (по умолчанию 0)")
    args = ap.parse_args()

    protect = tuple(args.protect) if args.protect else DEFAULT_PROTECT

    try:
        ts = targets()
    except Exception as exc:
        print(f"CDP недоступен: {exc}", file=sys.stderr)
        return 2

    pages = [t for t in ts if t.get("type") == "page"]
    matched, protected = [], []
    for t in pages:
        url = t.get("url") or ""
        if args.url_pattern not in url:
            continue
        if any(p in url for p in protect):
            protected.append(t)
        else:
            matched.append(t)

    print(f"всего таргетов {len(ts)} | страниц {len(pages)}")
    print(f"подходит под '{args.url_pattern}': {len(matched)}"
          f" | защищено: {len(protected)}")
    for t in protected:
        print(f"  ЗАЩИЩЕНО: {(t.get('url') or '')[:60]}")

    if len(matched) <= args.keep:
        print("убирать нечего")
        return 0

    victims = matched[args.keep:]
    closed = 0
    for t in victims:
        url = (t.get("url") or "")[:56]
        print(f"  {'БУДЕТ ЗАКРЫТА' if args.dry_run else 'закрываю'}: {url} id={t.get('id')}")
        if args.dry_run:
            continue
        try:
            urllib.request.urlopen(f"{CDP}/json/close/{t.get('id')}", timeout=10)
            closed += 1
        except Exception as exc:
            print(f"    не удалось: {exc}")

    if not args.dry_run:
        # Chrome убирает таргеты не мгновенно: если опросить сразу, счётчик
        # покажет старые значения и будет выглядеть так, будто ничего не закрылось
        # (наблюдали: «закрыто 7 | осталось 7», хотя через секунду осталось 0).
        import time

        left = None
        for _ in range(10):
            time.sleep(0.7)
            snap = targets()
            left = [t for t in snap
                    if t.get("type") == "page" and args.url_pattern in (t.get("url") or "")]
            if len(left) <= args.keep:
                break
        pages = len([t for t in targets() if t.get("type") == "page"])
        print(f"закрыто {closed} | осталось '{args.url_pattern}': {len(left or [])} | "
              f"страниц всего: {pages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
