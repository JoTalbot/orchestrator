#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-P2 сырьё: привязка чатов к проектам, хронология, бэклог, спеки.
Результат: agent_profile/knowledge/raw.json, BACKLOG.md, specs/*.md"""
import json
import re
import os
from collections import defaultdict
from datetime import datetime

DATA = "/opt/orchestrator/data/chat_list.json"
CHATS_DIR = "/opt/orchestrator/data/chats"
OUT_DIR = "/opt/orchestrator/agent_profile"
os.makedirs(f"{OUT_DIR}/knowledge", exist_ok=True)
os.makedirs(f"{OUT_DIR}/specs", exist_ok=True)

# gizmo_id (префикс) -> проект
GIZMO_MAP = {
    "g-p-6a94053b811c8191a46daaaa059f9dd5": "ukraine",
    "g-p-6a982ca8412c8191b9e2c19782081f44": "game",
    "g-p-6aa2953b903c8191a63ff8ce5022b803": "logistics",
    "g-p-6aa1f7aab6c481918b77db4ff1402794": "fs",
    "g-p-6aa503caf3bc81918f621eb057e4964a": "transcribe",
    "g-p-6a5b509194748191bf0dc9e4f5ad2d92": "aios",
    "g-p-6a983f2427f4819193a59e49b09267ca": "madworld",
    "g-p-6a51381aa730819183ee5afeba8b87d1": "octopus",
    "g-6a46e43fb01c819189deff6696bbcea3": "octopus-cc",
    "g-p-6a987ee039e081919e53860a39fe1756": "scan",
    "g-p-6a9817d2ff888191b25f15cac5a22904": "td",
    "g-p-6a9343f20ce48191a9c63ae041d29233": "uasep",
    "g-p-6a9994a348ac819181177a610d88efed": "browser",
    "g-p-6aa6bac22e708191b5d5341f577e3a55": "laba",
    "g-p-6aa03e82e1b08191af46b250abe58284": "words",
    "g-p-693b59ca2a0081919ad80b102661cf01": "tg",
    "g-p-6aa786d85ca481919d2c6376e9a64dde": "ukraine-old",
}
TITLE_HINTS = {
    "ukraine": ["ukraine", "украин", "закон", "суд", "правов", "data.gov",
                "edrsr", "legal", "єдр"],
    "game": ["game", "игр", "арен", "arena", "mad world", "мадворлд", "word"],
    "logistics": ["logistics", "логістик", "логистик", "перевоз", "пошта"],
    "fs": ["fs", "файлов", "filesystem", "storage", "диск"],
    "transcribe": ["transcrib", "транскриб", "аудио", "whisper", "распознаван"],
    "aios": ["aios", "вечность", "бессмерти", "нод"],
    "octopus": ["octopus", "осьминог"],
    "scan": ["scan", "скан", "ocr"],
    "uasep": ["uasep"],
    "browser": ["браузер", "browser", "rpa", "playwright", "cdp"],
    "laba": ["laba", "лаба"],
    "tg": ["telegram", "телеграм", "бот", "tgclone"],
    "words": ["words", "словесн"],
    "madworld": ["madworld", "mad world", "мадворлд"],
}


def msg_text(msg):
    if not msg or not msg.get("content"):
        return ""
    return "".join(p for p in msg["content"].get("parts", [])
                   if isinstance(p, str))


def extract_messages(conv):
    users, assts = [], []
    for node in conv.get("mapping", {}).values():
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role")
        text = msg_text(msg)
        if text:
            ts = msg.get("create_time")
            (users if role == "user" else assts).append((ts, text))
    users.sort(key=lambda x: x[0] or 0)
    assts.sort(key=lambda x: x[0] or 0)
    return users, assts


def project_of(conv):
    gid = conv.get("gizmo_id") or ""
    for prefix, proj in GIZMO_MAP.items():
        if gid.startswith(prefix):
            return proj
    title = (conv.get("title") or "").lower()
    snippet = (conv.get("snippet") or "").lower()
    blob = title + " " + snippet
    for proj, kws in TITLE_HINTS.items():
        if any(k in blob for k in kws):
            return proj
    return None


def main():
    meta_list = json.load(open(DATA))
    meta = {x.get("id"): x for x in meta_list if x.get("id")}
    # полные сообщения — из chats/*.json
    full = {}
    for f in os.listdir(CHATS_DIR):
        if not f.endswith(".json"):
            continue
        try:
            d = json.load(open(f"{CHATS_DIR}/{f}"))
        except Exception:
            continue
        cid = d.get("conversation_id")
        if cid and d.get("mapping"):
            full[cid] = d
    print(f"метаданных: {len(meta)}, полных чатов: {len(full)}")
    projects = defaultdict(list)
    unfinished = []
    long_tasks = []
    for cid, conv in full.items():
        m = meta.get(cid, {})
        conv = {**m, **conv}  # метаданные + mapping
        users, assts = extract_messages(conv)
        proj = project_of(conv)
        title = conv.get("title") or "?"
        ct = conv.get("create_time")
        dt = datetime.fromtimestamp(ct).strftime("%Y-%m-%d") if ct else "?"
        # последнее содержательное задание (>=30 симв.)
        last_task = None
        for _, t in reversed(users):
            if len(t.strip()) >= 30:
                last_task = t.strip()
                break
        # последний итог ассистента
        last_asst = assts[-1][1] if assts else None
        entry = {
            "id": cid, "title": title, "date": dt,
            "n_user": len(users), "n_asst": len(assts),
            "last_task": (last_task or "")[:400],
            "last_asst_tail": (last_asst or "")[-400:],
            "unfinished": bool(users and (not assts or users[-1][0] > assts[-1][0])),
        }
        if proj:
            projects[proj].append(entry)
        # длинные задания — спеки
        if users and len(users[0][1]) >= 4000:
            long_tasks.append({
                "title": title, "date": dt, "project": proj or "прочее",
                "chars": len(users[0][1]), "text": users[0][1]})

    # сортировка: свежие чаты в конце
    for proj in projects:
        projects[proj].sort(key=lambda x: x["date"])

    raw = {"projects": {k: v for k, v in sorted(projects.items())}}
    json.dump(raw, open(f"{OUT_DIR}/knowledge/raw.json", "w"),
              ensure_ascii=False, indent=1)

    # ---------- BACKLOG.md ----------
    backlog = []
    for cid, conv in full.items():
        m = meta.get(cid, {})
        conv = {**m, **conv}
        users, assts = extract_messages(conv)
        if users and (not assts or users[-1][0] > assts[-1][0]):
            last = users[-1][1].strip()
            if len(last) >= 3:
                proj = project_of(conv) or "прочее"
                backlog.append({
                    "project": proj,
                    "title": conv.get("title") or "?",
                    "date": datetime.fromtimestamp(
                        conv.get("create_time") or 0).strftime("%Y-%m-%d"),
                    "last_user": last[:200].replace("\n", " ")})
    with open(f"{OUT_DIR}/BACKLOG.md", "w") as f:
        f.write("# Бэклог незавершённого (из чатов)\n\n")
        f.write("Чаты, где последнее сообщение пользователя осталось без ответа.\n")
        f.write("Автогенерация: `chatgpt_analysis/build_raw.py`.\n\n")
        cur = None
        for b in sorted(backlog, key=lambda x: (x["project"], x["date"])):
            if b["project"] != cur:
                cur = b["project"]
                f.write(f"\n## {cur}\n")
            f.write(f"- **{b['title']}** ({b['date']}): {b['last_user']}\n")
    print(f"BACKLOG: {len(backlog)} пунктов")

    # ---------- спеки ----------
    long_tasks.sort(key=lambda x: -x["chars"])
    seen_titles = set()
    n_specs = 0
    for lt in long_tasks:
        slug = re.sub(r"[^a-z0-9]+", "-", (lt["title"] or "spec").lower()
                      ).strip("-")[:40] or "spec"
        if slug in seen_titles:
            slug = f"{slug}-{n_specs}"
        seen_titles.add(slug)
        with open(f"{OUT_DIR}/specs/{slug}.md", "w") as f:
            f.write(f"# {lt['title']}\n\n")
            f.write(f"Источник: чат ChatGPT, {lt['date']}, проект: {lt['project']}\n")
            f.write(f"Объём: {lt['chars']} символов\n\n---\n\n")
            f.write(lt["text"])
        n_specs += 1
    print(f"SPECS: {n_specs} файлов (>=4000 симв.)")

    # сводка проектов
    print("\n== ПРОЕКТЫ (чатов) ==")
    for proj, chats in sorted(projects.items(), key=lambda x: -len(x[1])):
        unf = sum(1 for c in chats if c["unfinished"])
        print(f"  {proj:14s} чатов={len(chats):3d}  незавершённых={unf}")


if __name__ == "__main__":
    main()
