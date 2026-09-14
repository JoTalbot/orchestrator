#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1: дайджесты истории проектов через LLM (liza-mock → Gemini).
Вход: agent_profile/knowledge/raw.json
Выход: agent_profile/knowledge/<project>.md"""
import json
import time
import urllib.request

RAW = "/opt/orchestrator/agent_profile/knowledge/raw.json"
OUT_DIR = "/opt/orchestrator/agent_profile/knowledge"
LLM_URL = "http://127.0.0.1:8000/v1/chat/completions"
LLM_KEY = "mock-rpa-key"

# проекты для дайджестов: активные драйверы + бэклог-кандидаты
PROJECTS = ["ukraine", "game", "logistics", "fs", "transcribe",
            "madworld", "aios"]

PROMPT_TMPL = """Ты — аналитик. Ниже — выжимка из чатов ChatGPT по проекту {project}:
{data}

Составь компактный дайджест истории проекта на русском. Разделы:
## Цель проекта
## Архитектура и ключевые решения
## Известные проблемы и их фиксы
## Текущий статус
## Следующие шаги
## Правила и договорённости
Только факты из данных, без домыслов. Маркдаун, до 2000 символов."""


def ask_llm(prompt: str, max_tokens: int = 3000) -> str:
    body = json.dumps({
        "model": "gemini-rpa",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_KEY}",
    })
    with urllib.request.urlopen(req, timeout=900) as r:
        resp = json.loads(r.read())
    return resp["choices"][0]["message"]["content"]


def main():
    raw = json.load(open(RAW))
    projects = raw["projects"]
    for proj in PROJECTS:
        chats = projects.get(proj, [])
        if not chats:
            print(f"{proj}: чатов нет — пропуск")
            continue
        # сырьё: последние 12 чатов, компактно
        lines = []
        for c in chats[-12:]:
            lines.append(f"- [{c['date']}] {c['title']} "
                         f"(user:{c['n_user']}, asst:{c['n_asst']})")
            if c.get("last_task"):
                lines.append(f"  задание: {c['last_task'][:220]}")
            if c.get("last_asst_tail"):
                lines.append(f"  итог: {c['last_asst_tail'][-220:]}")
        data = "\n".join(lines)[:9000]
        prompt = PROMPT_TMPL.format(project=proj, data=data)
        print(f"→ {proj}: {len(chats)} чатов, промпт {len(prompt)} симв.")
        for attempt in range(2):
            try:
                text = ask_llm(prompt)
                break
            except Exception as e:
                print(f"  попытка {attempt+1} не удалась: {e}")
                time.sleep(30)
                text = ""
        if not text:
            print(f"  {proj}: LLM не ответил — дайджест не создан")
            continue
        with open(f"{OUT_DIR}/{proj}.md", "w") as f:
            f.write(f"# {proj} — история из чатов (автодайджест)\n\n")
            f.write(text)
            f.write(f"\n\n---\nИсточник: {len(chats)} чатов, "
                    f"автогенерация {time.strftime('%Y-%m-%d %H:%M')}, "
                    f"сырьё: agent_profile/knowledge/raw.json\n")
        print(f"  {proj}: сохранено ({len(text)} симв.)")
        time.sleep(10)


if __name__ == "__main__":
    main()
