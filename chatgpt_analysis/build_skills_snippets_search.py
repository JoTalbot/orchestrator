#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-P3: скилы из реальных решений (LLM по фактам чатов),
библиотека сниппетов, SQLite FTS5 RAG-индекс + CLI поиска."""
import json
import os
import re
import sqlite3
import time
import urllib.request
from collections import Counter, defaultdict

DATA = "/opt/orchestrator/data/chats"
OUT_DIR = "/opt/orchestrator/agent_profile"
LLM_URL = "http://127.0.0.1:8000/v1/chat/completions"
LLM_KEY = "mock-rpa-key"

SKILLS = {
    "ssh-server": {
        "кв": ["ssh", "scp", "systemctl", "nginx", "sudo", "chmod",
               "ufw", "rsync", "openssh", "сервер", "vps", "oracle cloud"],
        "домены": ["ubuntu.com", "docs.oracle.com"],
        "cmd": ["ssh", "scp", "rsync", "systemctl", "journalctl", "ufw",
                "chmod", "chown"],
    },
    "github-api": {
        "кв": ["api.github.com", "workflow", "release", "github actions",
               "pull request", "webhook", "blob sha", "github api"],
        "домены": ["api.github.com", "raw.githubusercontent.com"],
        "cmd": ["curl", "git", "gh"],
    },
    "deploy-railway-vercel": {
        "кв": ["railway", "vercel", "deploy", "nginx", "домен", "dns",
               "https", "certbot", "ssl"],
        "домены": ["railway.app", "vercel.app"],
        "cmd": ["railway", "vercel", "npm", "npx"],
    },
    "kaggle-colab": {
        "кв": ["kaggle", "colab", "gpu", "kernel", "dataset", "fine-tun",
               "обучени", "notebook", "huggingface"],
        "домены": ["kaggle.com", "colab.research.google.com",
                   "huggingface.co", "drive.google.com"],
        "cmd": ["kaggle", "python", "pip"],
    },
    "data-gov-ua": {
        "кв": ["data.gov.ua", "autosklo", "edrsr", "єдр", "реестр",
               "закон", "відкриті дані", "відкриті дан", "api"],
        "домены": ["data.gov.ua", "api.autosklo.org.ua"],
        "cmd": ["curl", "python"],
    },
    "telegram-bot": {
        "кв": ["telegram", "телеграм", "бот", "webhook", "aiogram",
               "pyrogram", "bot api", "tgclone", "чат-бот"],
        "домены": ["api.telegram.org", "t.me", "web.telegram.org"],
        "cmd": ["curl", "python", "pip"],
    },
    "browser-rpa": {
        "кв": ["playwright", "selenium", "cdp", "chrome", "браузер",
               "автоматизац", "rpa", "puppeteer", "websocket"],
        "домены": [],
        "cmd": ["python", "pip", "playwright", "node"],
    },
}

CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
CMD_RE = re.compile(r"^\s*(\$|>)\s*(\S.*)$", re.M)
URL_RE = re.compile(r"https?://[^\s\)\]\"\"]+")


def msg_text(msg):
    if not msg or not msg.get("content"):
        return ""
    return "".join(p for p in msg["content"].get("parts", [])
                   if isinstance(p, str))


def collect_facts():
    """По каждому скилу: команды, ссылки, сниппеты из чатов."""
    facts = {k: {"cmd": Counter(), "urls": Counter(), "code": []}
             for k in SKILLS}
    snippets = defaultdict(list)  # norm -> (code, chats)
    for f in sorted(os.listdir(DATA)):
        if not f.endswith(".json"):
            continue
        try:
            d = json.load(open(f"{DATA}/{f}"))
        except Exception:
            continue
        title = d.get("title") or "?"
        texts = []
        for node in d.get("mapping", {}).values():
            t = msg_text(node.get("message"))
            if t:
                texts.append(t)
        all_text = "\n".join(texts)
        blob = all_text.lower()
        # команды
        for _, cmd in CMD_RE.findall(all_text):
            w = cmd.split()[0] if cmd.split() else "?"
            for slug, spec in SKILLS.items():
                if w in spec["cmd"]:
                    facts[slug]["cmd"][cmd[:120]] += 1
        # ссылки
        for u in URL_RE.findall(all_text):
            host = u.split("/")[2] if u.count("/") >= 2 else ""
            for slug, spec in SKILLS.items():
                if any(dm in host for dm in spec["домены"]):
                    facts[slug]["urls"][u[:140]] += 1
        # код-блоки
        for code in CODE_RE.findall(all_text):
            code = code.strip()
            if not code or len(code) < 20:
                continue
            norm = re.sub(r"\s+", " ", code)[:80]
            snippets[norm].append(code)
            # привязка к скилу по ключевым словам
            for slug, spec in SKILLS.items():
                if any(k in blob and k in code.lower()[:200]
                       for k in spec["кв"][:4]):
                    facts[slug]["code"].append((code[:800], title))
    return facts, snippets


def ask_llm(prompt, max_tokens=2500):
    body = json.dumps({"model": "gemini-rpa",
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(LLM_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_KEY}"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def build_skills(facts):
    os.makedirs(f"{OUT_DIR}/skills", exist_ok=True)
    for slug, spec in SKILLS.items():
        f = facts[slug]
        parts = []
        if f["cmd"]:
            parts.append("Частые команды из чатов:\n" + "\n".join(
                f"  {c} (x{n})" for c, n in f["cmd"].most_common(12)))
        if f["urls"]:
            parts.append("Ссылки:\n" + "\n".join(
                f"  {u} (x{n})" for u, n in f["urls"].most_common(8)))
        if f["code"]:
            seen = set()
            code_lines = []
            for c, t in f["code"]:
                key = c[:60]
                if key in seen:
                    continue
                seen.add(key)
                code_lines.append(f"```\n{c}\n```")
                if len(code_lines) >= 4:
                    break
            parts.append("Примеры кода из чатов:\n" + "\n".join(code_lines))
        data = "\n".join(parts)
        if len(data) < 100:
            print(f"  {slug}: фактов мало — пропуск")
            continue
        prompt = (f"Ты — составитель skill-карточек для автономного агента. "
                  f"Тема: {slug}. Факты из реальных чатов:\n{data[:3500]}\n\n"
                  f"Составь skill-карточку на русском: 1) Когда применять; "
                  f"2) Шаги/порядок действий; 3) Ключевые команды и паттерны "
                  f"(из фактов); 4) Частые ошибки. Маркдаун, до 1500 символов.")
        try:
            card = ask_llm(prompt)
        except Exception as e:
            print(f"  {slug}: LLM ошибка {e} — пропуск")
            continue
        with open(f"{OUT_DIR}/skills/skill-{slug}.md", "w") as out:
            out.write(f"# Skill: {slug}\n\n{card}\n")
        print(f"  {slug}: готово ({len(card)} симв.)")
        time.sleep(8)


def build_snippets(snippets):
    os.makedirs(f"{OUT_DIR}/snippets", exist_ok=True)
    # рейтинг: повторяемость + наличие маркеров полезности
    markers = re.compile(
        r"(def |import |docker |ssh |scp |git |curl |sudo |systemctl |"
        r"npm |pip |python |node |#!/|yaml|json|nginx|python3|kaggle|"
        r"playwright|async def|class )")
    ranked = []
    for norm, codes in snippets.items():
        sample = codes[0]
        if not markers.search(sample):
            continue
        score = len(codes) + min(len(sample) // 500, 3)
        ranked.append((score, len(codes), norm, sample))
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    index = []
    for i, (score, cnt, norm, code) in enumerate(ranked[:120]):
        fname = f"snippet-{i:03d}.txt"
        with open(f"{OUT_DIR}/snippets/{fname}", "w") as f:
            f.write(code)
        index.append({"file": fname, "repeat": cnt,
                      "head": norm[:70]})
    with open(f"{OUT_DIR}/snippets/index.json", "w") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"SNIPPETS: {len(index)} файлов")


def build_fts():
    """SQLite FTS5 RAG-индекс по всем сообщениям."""
    db = sqlite3.connect("/opt/orchestrator/data/chat_index.db")
    db.execute("DROP TABLE IF EXISTS messages")
    db.execute("CREATE TABLE messages("
               "id INTEGER PRIMARY KEY, chat_title TEXT, role TEXT,"
               "ts REAL, text TEXT)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts "
               "USING fts5(text, content='messages', content_rowid='id')")
    rows = []
    mid = 0
    for f in sorted(os.listdir(DATA)):
        if not f.endswith(".json"):
            continue
        try:
            d = json.load(open(f"{DATA}/{f}"))
        except Exception:
            continue
        title = d.get("title") or "?"
        for node in d.get("mapping", {}).values():
            msg = node.get("message") or {}
            role = (msg.get("author") or {}).get("role")
            text = msg_text(msg)
            if not text or role not in ("user", "assistant"):
                continue
            mid += 1
            rows.append((mid, title, role, msg.get("create_time"), text))
    db.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", rows)
    db.execute("INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild')")
    db.commit()
    n = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    print(f"RAG: {n} сообщений в FTS-индексе")


def main():
    t0 = time.time()
    facts, snippets = collect_facts()
    print(f"факты собраны за {time.time()-t0:.0f}с")
    print("== СКИЛЫ ==")
    build_skills(facts)
    print("== СНИППЕТЫ ==")
    build_snippets(snippets)
    print("== RAG ==")
    build_fts()


if __name__ == "__main__":
    main()
