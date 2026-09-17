#!/usr/bin/env python3
"""Монитор выбора модели в Agent Mode (флаг `agent-model-selector`).

Что проверяет (через локальный REST-сервис, браузер не трогает):
  GET /models                     — доступен ли список моделей арены
                                    (403 «Not allowed» = доступ закрыт)
  GET /flags?only=agent-model-selector — назначен ли аккаунту флаг

Как только арена откроет доступ (список моделей вернётся, либо флаг появится в
posthogFlags) — монитор пишет ALERT в лог, сохраняет список моделей в
`data/arena/models_available.json` и шлёт уведомление в Telegram.

Состояние: `data/arena/model_watch.json`, лог: `logs/model_watch.log`.

Запуск:
    model_watch.py --once              # одна проверка (для cron/systemd-таймера)
    model_watch.py --loop 900          # каждые 15 минут в foreground
    model_watch.py --self-test         # имитировать срабатывание и проверить алерт
    model_watch.py --send-test         # просто послать тестовое уведомление

Уведомления: используются те же реквизиты, что и у Hermes-алертов —
`/etc/hermes/telegram.env` (TELEGRAM_BOT_TOKEN) и `/etc/hermes/telegram.chats.json`
(chat_id). Можно переопределить переменными ARENA_WATCH_TELEGRAM_TOKEN /
ARENA_WATCH_TELEGRAM_CHAT или задать ARENA_WATCH_WEBHOOK (POST с JSON).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("ARENA_API_URL", "http://127.0.0.1:8790").rstrip("/")
DATA = Path(os.getenv("ARENA_DATA_DIR", str(ROOT / "data" / "arena")))
LOG = ROOT / "logs" / "model_watch.log"
STATE = DATA / "model_watch.json"
SELFTEST_STATE = DATA / "model_watch_selftest.json"
DUMP = DATA / "models_available.json"
FLAG = "agent-model-selector"


def api_token() -> str:
    if os.getenv("ARENA_API_KEY"):
        return os.getenv("ARENA_API_KEY")
    f = ROOT / ".secrets" / "arena_service_token.txt"
    return f.read_text().strip() if f.exists() else ""


TOKEN = api_token()


def log(msg: str):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get(path: str, timeout: int = 180):
    req = urllib.request.Request(BASE + path, headers={"X-API-Key": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_httpError": e.code, "_body": e.read().decode()[:300]}
    except Exception as e:
        return {"_error": str(e)[:200]}


# ------------------------------------------------------------ уведомления

def telegram_credentials():
    tok = os.getenv("ARENA_WATCH_TELEGRAM_TOKEN", "")
    chat = os.getenv("ARENA_WATCH_TELEGRAM_CHAT", "")
    if tok and chat:
        return tok, chat
    for path, need in (("/etc/hermes/telegram.env", "token"),
                       ("/etc/hermes/telegram.chats.json", "chat")):
        try:
            raw = Path(path).read_text()
        except PermissionError:                      # файлы root-only
            try:
                raw = subprocess.run(["sudo", "-n", "cat", path],
                                     capture_output=True, text=True,
                                     timeout=15).stdout
            except Exception:
                raw = ""
        except Exception:
            raw = ""
        if need == "token" and not tok:
            for line in raw.splitlines():
                if line.startswith(("TELEGRAM_BOT_TOKEN=", "BOT_TOKEN=")):
                    tok = line.split("=", 1)[1].strip()
        if need == "chat" and not chat:
            try:
                d = json.loads(raw)
                items = d if isinstance(d, list) else d.get("chats") or list(d.values())
                first = items[0] if items else None
                chat = str(first.get("chat_id") if isinstance(first, dict) else first)
            except Exception:
                pass
    return tok, chat


def notify(text: str) -> str:
    """Доставка алерта. Возвращает строку-отчёт о доставке."""
    sent = []
    tok, chat = telegram_credentials()
    if tok and chat:
        body = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                sent.append("telegram:%s" % r.status)
        except Exception as e:
            sent.append("telegram:ошибка %s" % str(e)[:120])
    else:
        sent.append("telegram:нет реквизитов")
    hook = os.getenv("ARENA_WATCH_WEBHOOK")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                sent.append("webhook:%s" % r.status)
        except Exception as e:
            sent.append("webhook:ошибка %s" % str(e)[:120])
    return ", ".join(sent)


# ---------------------------------------------------------------- проверка

def probe(simulate: bool = False) -> dict:
    """Один замер: доступен ли выбор модели."""
    if simulate:
        return {"available": True, "status": 200, "flag": "treatment-1",
                "modelSelector": True, "simulated": True,
                "models": [{"id": "00000000-0000-0000-0000-000000000001",
                            "publicName": "SIMULATED-MODEL",
                            "displayName": "Имитация для проверки монитора"}]}
    m = get("/models")
    f = get("/flags?only=%s" % FLAG)
    flags = (f or {}).get("flags") or {}
    out = {
        "available": bool(m.get("available")),
        "status": m.get("status") if "status" in m else None,
        "error": m.get("error") or m.get("_error") or m.get("_body"),
        "models": m.get("models"),
        "flagPresent": FLAG in flags,
        "flag": flags.get(FLAG),
        "modelSelector": bool((f or {}).get("modelSelector")),
        "flagsCount": (f or {}).get("count"),
    }
    if "_error" in (m or {}) or "_httpError" in (m or {}):
        out["probeError"] = m.get("_error") or ("HTTP %s" % m.get("_httpError"))
    return out


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, st: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(path)


def alert_text(res: dict, first: bool) -> str:
    names = []
    for m in (res.get("models") or [])[:20]:
        if isinstance(m, dict):
            names.append("%s (%s)" % (m.get("publicName") or m.get("displayName"),
                                      str(m.get("id"))[:13]))
    head = ("\U0001F9EA ТЕСТ МОНАТОРА (имитация, не настоящее срабатывание)\n"
            if res.get("simulated") else
            "\U0001F3AF Arena AI: выбор модели в Agent Mode СТАЛ ДОСТУПЕН\n")
    return (head +
            "флаг %s: %s\n"
            "GET /api/chat/agent-models → %s\n"
            "моделей: %d%s\n"
            "Создать чат с конкретной моделью:\n"
            "  curl -H \"X-API-Key: $TOKEN\" -H 'Content-Type: application/json' "
            "-d '{\"text\":\"задача\",\"model_id\":\"<id модели>\",\"wait\":true}' "
            "http://127.0.0.1:8790/chats"
            % (FLAG, res.get("flag") if res.get("flagPresent") else "отсутствует",
               res.get("status"), len(res.get("models") or []),
               ("\n" + "\n".join(names)) if names else "")
            + ("\n(первая проверка — состояние зафиксировано)" if first else ""))


def check(state_path: Path, simulate: bool = False, quiet_ok: bool = False) -> int:
    res = probe(simulate=simulate)
    prev = load_state(state_path)
    now = int(time.time())

    if res.get("probeError"):
        log("ОШИБКА проверки: %s (сервис arena-api жив?)" % res["probeError"])
        return 2

    opened = res["available"] or res["modelSelector"]
    was_opened = bool(prev.get("opened"))
    res["opened"] = opened
    res["checkedAt"] = now
    res["checks"] = int(prev.get("checks") or 0) + 1

    line = ("доступ: %s | /api/chat/agent-models → %s | флаг %s: %s | проверок: %d"
            % ("ДА" if opened else "нет", res.get("status"), FLAG,
               (res.get("flag") if res.get("flagPresent") else "не назначен"),
               res["checks"]))
    if simulate:
        line = "[ИМИТАЦИЯ] " + line

    if opened and not was_opened:
        log("ALERT " + line)
        if res.get("models"):
            save_state(DUMP, {"savedAt": now, "simulated": bool(simulate),
                              "models": res["models"]})
            log("  список моделей сохранён: %s" % DUMP)
        report = notify(alert_text(res, first=not prev))
        log("  уведомление: %s" % report)
        res["lastAlertAt"] = now
        res["alertDelivery"] = report
    elif opened:
        log("по-прежнему доступно: %s" % line)
    elif was_opened:
        log("ВНИМАНИЕ: доступ снова закрыт — %s" % line)
        notify("Arena AI: выбор модели снова недоступен (флаг %s пропал, "
               "/api/chat/agent-models → %s)" % (FLAG, res.get("status")))
        res["lastChangeAt"] = now
    else:
        if not quiet_ok:
            log(line)

    if not prev or prev.get("opened") != opened:
        res["lastChangeAt"] = now
    hist = (prev.get("history") or [])[-200:]
    hist.append({"at": now, "opened": opened, "status": res.get("status"),
                 "flagPresent": res.get("flagPresent"), "flag": res.get("flag"),
                 "simulated": bool(simulate)})
    res["history"] = hist
    save_state(state_path, res)
    return 0 if not opened else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.add_argument("--loop", type=int, default=0, metavar="СЕКУНД",
                    help="проверять каждые N секунд (0 = не запускать цикл)")
    ap.add_argument("--self-test", action="store_true",
                    help="имитировать включение доступа и пройти весь путь алерта")
    ap.add_argument("--send-test", action="store_true",
                    help="послать тестовое уведомление и выйти")
    ap.add_argument("--text", default=None, help="свой текст для --send-test")
    ap.add_argument("--quiet-ok", action="store_true",
                    help="не логировать проверки без изменений")
    a = ap.parse_args()

    if a.send_test:
        log("тестовое уведомление монитора выбора модели (arena-model-watch)")
        print(notify(a.text or
                     "TEST: монитор arena-model-watch проверяет доставку. "
                     "Флаг %s пока не назначен, /api/chat/agent-models → 403." % FLAG))
        return

    if a.self_test:
        log("=== SELF-TEST: имитирую включение выбора модели ===")
        code = check(SELFTEST_STATE, simulate=True, quiet_ok=a.quiet_ok)
        log("=== SELF-TEST завершён (код %d) ===" % code)
        st = load_state(SELFTEST_STATE)
        log("состояние self-test: opened=%s, уведомление=%s"
            % (st.get("opened"), st.get("alertDelivery")))
        return

    if not TOKEN:
        log("нет токена REST-сервиса (.secrets/arena_service_token.txt)")
        sys.exit(2)

    code = check(STATE, quiet_ok=a.quiet_ok)
    if a.loop:
        while True:
            time.sleep(a.loop)
            code = check(STATE, quiet_ok=a.quiet_ok) or code
    sys.exit(0 if code in (0, 1) else 2)


if __name__ == "__main__":
    main()
