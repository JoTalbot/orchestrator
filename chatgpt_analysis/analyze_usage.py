#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Анализ содержимого всех скачанных чатов: темы, артефакты, риски,
незавершённые задачи, проектная привязка. Результат — JSON + дайджест."""
import json
import re
import glob
from collections import Counter, defaultdict
from datetime import datetime

CHATS = "/opt/orchestrator/data/chats"
OUT = "/opt/orchestrator/data/chat_usage_metrics.json"


def msg_text(msg):
    if not msg or not msg.get("content"):
        return ""
    parts = msg["content"].get("parts", [])
    return "".join(p for p in parts if isinstance(p, str))


def extract(mapping):
    """Возвращает (user_msgs, asst_msgs) — списки (ts, text)."""
    users, assts = [], []
    for node in mapping.values():
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role")
        text = msg_text(msg)
        if not text:
            continue
        ts = msg.get("create_time")
        (users if role == "user" else assts).append((ts, text))
    users.sort(key=lambda x: x[0] or 0)
    assts.sort(key=lambda x: x[0] or 0)
    return users, assts


# ---------------- классификация тем ----------------
def classify(title, text_sample):
    blob = (title + "\n" + text_sample[:3000]).lower()
    rules = [
        ("ukraine/legal/data", ["ukraine", "data.gov", "ua-legal", "закон",
                                "суд", "правов", "юридич", "legislation",
                                "registry", "єдр", "експорт даних"]),
        ("kaggle/ml", ["kaggle", "colab", "huggingface", "dataset", "fine-tun",
                       "llm", "ml ", "нейро", "обучени", "kernel", "parquet",
                       "torch", "tokenizer"]),
        ("game", ["game", "unity", "mad world", "мадворлд", "матч", "арена",
                  "грав", "игров"]),
        ("logistics", ["logistics", "логістик", "логистик", "delivery",
                       "перевоз", "нова пошта", "кур'єр"]),
        ("fs/files", ["filesystem", "файлов", "storage", "fuse", "бэкап",
                      "backup", "архив"]),
        ("transcribe/audio", ["transcrib", "транскриб", "whisper", "аудио",
                              "speech", "stt", "распознаван"]),
        ("telegram/bots", ["telegram", "телеграм", "tg ", "бот", "tgclone"]),
        ("browser/rpa", ["браузер", "browser", "playwright", "selenium",
                         "puppeteer", "cdp", "rpa", "автоматизац"]),
        ("server/infra", ["сервер", "ssh", "docker", "oracle", "deploy",
                          "nginx", "systemd", "vps", "облако", "ubuntu"]),
        ("parsing/ocr", ["парс", "parser", "скрейп", "scrap", "ocr", "скан",
                         "распознавани"]),
        ("payments/refund", ["refund", "возврат", "платёж", "aliexpress",
                             "casino", "казино", "stripe", "paypal"]),
        ("web/frontend", ["react", "next.js", "верстк", "вёрстк", "css",
                          "frontend", "vercel", "tailwind"]),
    ]
    hits = []
    for label, kws in rules:
        if any(k in blob for k in kws):
            hits.append(label)
    return hits


PROJECT_RE = re.compile(
    r"(?:github\.com/)?JoTalbot/([A-Za-z0-9_\-]+)")
CODE_RE = re.compile(r"```[^`]*```")
URL_RE = re.compile(r"https?://[^\s\)\]\"]+")
CMD_RE = re.compile(r"^\s*(\$|>)\s*(\S.*)$", re.M)
SECRET_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|xox[bap]-[A-Za-z0-9\-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}|ya29\.[A-Za-z0-9_\-]{20,})")

stats = {
    "chats": 0,
    "user_msgs": 0,
    "asst_msgs": 0,
    "user_chars": 0,
    "asst_chars": 0,
    "code_blocks": 0,
    "urls": Counter(),
    "projects": Counter(),
    "commands": Counter(),
    "themes": Counter(),
    "secrets": [],
    "by_month": Counter(),
    "chat_themes": {},
    "long_tasks": [],
    "unfinished": [],
    "langs": Counter(),
}

UA_CYRILLIC = set("єіїґ")
RU_CYRILLIC = set("ъыэё")

for f in sorted(glob.glob(f"{CHATS}/*.json")):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    stats["chats"] += 1
    title = d.get("title") or ""
    users, assts = extract(d.get("mapping", {}))
    u_texts = [t for _, t in users]
    a_texts = [t for _, t in assts]
    all_text = "\n".join(u_texts + a_texts)

    stats["user_msgs"] += len(u_texts)
    stats["asst_msgs"] += len(a_texts)
    stats["user_chars"] += sum(len(t) for t in u_texts)
    stats["asst_chars"] += sum(len(t) for t in a_texts)
    stats["code_blocks"] += len(CODE_RE.findall(all_text))

    # ссылки
    for u in URL_RE.findall(all_text):
        host = u.split("/")[2] if u.count("/") >= 2 else u
        stats["urls"][host] += 1
    # проекты
    for p in PROJECT_RE.findall(all_text):
        stats["projects"][p.lower()] += 1
    # команды
    for _, cmd in CMD_RE.findall(all_text):
        first = cmd.split()[0] if cmd.split() else "?"
        stats["commands"][first] += 1
    # темы
    sample = "\n".join(u_texts[:2] + a_texts[:1])[:3000]
    themes = classify(title, sample)
    if themes:
        for t in themes:
            stats["themes"][t] += 1
        stats["chat_themes"][title] = themes
    # секреты
    for m in SECRET_RE.finditer(all_text):
        stats["secrets"].append({"chat": title, "match": m.group(0)[:40]})
    # месяцы
    if d.get("create_time"):
        try:
            dt = datetime.fromtimestamp(d["create_time"])
            stats["by_month"][dt.strftime("%Y-%m")] += 1
        except Exception:
            pass
    # язык (по юзер-сообщениям)
    ua = ru = 0
    for t in u_texts:
        chars = set(t.lower())
        ua += len(chars & UA_CYRILLIC)
        ru += len(chars & RU_CYRILLIC)
    if ua > ru:
        stats["langs"]["uk"] += 1
    elif ru > ua:
        stats["langs"]["ru"] += 1
    # длинные задания (первое user-сообщение ≥ 200 символов)
    if u_texts and len(u_texts[0]) >= 200:
        stats["long_tasks"].append({
            "chat": title, "chars": len(u_texts[0]),
            "text": u_texts[0][:180].replace("\n", " ")})
    # незавершённые: последнее — user без ответа
    if users and (not assts or users[-1][0] > assts[-1][0]):
        stats["unfinished"].append({
            "chat": title,
            "last_user": u_texts[-1][:120].replace("\n", " ")})

# ---------- итоги ----------
stats["long_tasks"].sort(key=lambda x: -x["chars"])
out = {
    "totals": {
        "chats": stats["chats"],
        "user_msgs": stats["user_msgs"],
        "asst_msgs": stats["asst_msgs"],
        "user_chars": stats["user_chars"],
        "asst_chars": stats["asst_chars"],
        "code_blocks": stats["code_blocks"],
    },
    "top_urls": stats["urls"].most_common(25),
    "projects": stats["projects"].most_common(30),
    "commands": stats["commands"].most_common(25),
    "themes": stats["themes"].most_common(30),
    "by_month": dict(sorted(stats["by_month"].items())),
    "langs": dict(stats["langs"]),
    "secrets_count": len(stats["secrets"]),
    "secret_samples": stats["secrets"][:10],
    "long_tasks_top20": stats["long_tasks"][:20],
    "unfinished_count": len(stats["unfinished"]),
    "unfinished_top15": stats["unfinished"][:15],
    "themed_chats": len(stats["chat_themes"]),
}
json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)

print("=" * 60)
t = out["totals"]
print(f"чатов: {t['chats']} | user-сообщений: {t['user_msgs']} "
      f"({t['user_chars']//1000}K симв.) | ответов: {t['asst_msgs']} "
      f"({t['asst_chars']//1000}K симв.) | код-блоков: {t['code_blocks']}")
print("\n== ТЕМЫ ==")
for k, v in out["themes"]:
    print(f"  {k:20s} {v}")
print("\n== ПРОЕКТЫ (упоминания JoTalbot/*) ==")
for k, v in out["projects"]:
    print(f"  {k:25s} {v}")
print("\n== ТОП ССЫЛОК (домены) ==")
for k, v in out["top_urls"][:12]:
    print(f"  {k:35s} {v}")
print("\n== ТОП КОМАНД ==")
for k, v in out["commands"][:12]:
    print(f"  {k:15s} {v}")
print("\n== ПО МЕСЯЦАМ ==")
print("  " + " ".join(f"{k}:{v}" for k, v in out["by_month"].items()))
print(f"\n== ЯЗЫК ЧАТОВ: uk={out['langs'].get('uk',0)} "
      f"ru={out['langs'].get('ru',0)}")
print(f"\n== СЕКРЕТЫ В ЧАТАХ: {out['secrets_count']} находок ==")
for s in out["secret_samples"][:6]:
    print(f"  [{s['chat'][:40]}] {s['match']}")
print(f"\n== НЕЗАВЕРШЁННЫХ ЧАТОВ: {out['unfinished_count']} ==")
for s in out["unfinished_top15"][:8]:
    print(f"  [{s['chat'][:45]}] {s['last_user']}")
print("\n== ДЛИННЫЕ ЗАДАНИЯ (топ-6) ==")
for s in out["long_tasks_top20"][:6]:
    print(f"  [{s['chat'][:45]}] {s['chars']} симв.: {s['text']}")
print("\nJSON сохранён:", OUT)
