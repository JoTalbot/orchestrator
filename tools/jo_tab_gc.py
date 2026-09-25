#!/usr/bin/env python3
"""jo_tab_gc.py - автозакрытие старых чат-вкладок ChatGPT (chatgpt.com/c/*).

Закрывает чат-вкладки, которые НЕ принадлежат активным Jo-агентам
(tab_id из /opt/orchestrator/agent_jo/state_*.json, обновлённые < 5 мин назад).
Проектные вкладки (/g/...), chatgpt.com/, arena.ai и остальные сайты не трогаем.
Запуск: systemd-таймер jo-tab-gc.timer (каждые 5 минут).
Лог: /var/log/jo-tab-gc.log
"""
import json
import re
import time
import urllib.request
from pathlib import Path

CDP = "http://127.0.0.1:9222"
STATE_DIR = Path("/opt/orchestrator/agent_jo")
STATE_MAX_AGE = 600  # агент «активен», если state обновлялся < N секунд назад
CHAT_RE = re.compile(r"^https://chatgpt\.com/(g/[^/?#]+/)?c/[0-9a-fA-F-]{32,40}")
LOG = Path("/var/log/jo-tab-gc.log")


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def cdp_targets():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


def cdp_close(tab_id):
    urllib.request.urlopen(CDP + "/json/close/" + tab_id, timeout=10)


def active_tab_ids():
    ids = set()
    now = time.time()
    for p in STATE_DIR.glob("state_*.json"):
        try:
            if now - p.stat().st_mtime > STATE_MAX_AGE:
                continue
            data = json.loads(p.read_text())
            tid = data.get("tab_id")
            if tid:
                ids.add(tid)
        except Exception:
            pass
    return ids


def main():
    try:
        targets = cdp_targets()
    except Exception as e:
        log("CDP недоступен: %s" % e)
        return
    active = active_tab_ids()
    closed, kept = [], 0
    for t in targets:
        if t.get("type") != "page":
            continue
        url = (t.get("url") or "").split("?")[0].split("#")[0]
        if not CHAT_RE.match(url):
            continue
        if t.get("id") in active:
            kept += 1
            continue
        try:
            cdp_close(t.get("id"))
            closed.append((t.get("title") or url)[:60])
        except Exception as e:
            log("не закрылась %s: %s" % (t.get("id"), e))
    log("active=%d closed=%d kept=%d %s" % (len(active), len(closed), kept,
        "; ".join(closed) if closed else ""))


if __name__ == "__main__":
    main()
