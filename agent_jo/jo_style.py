#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стиль Jo: шаблоны сообщений и эвристики решений (по PROFILE.md).

Протокол работы (15.09.2026, по просьбе Jo):
  * по ходу работы — в чат писать только «+»;
  * «КОНЕЦ» в конце сообщения — проект завершён или продолжить работу
    невозможно: работа над проектом останавливается;
  * первое сообщение нового чата в проекте —
    «@GitHub JoTalbot/<проект>» + «Продолжаем работать над проектом.»
    + правила работы.
"""

import re

# ---------------------------------------------------------------- правила

PROTOCOL = (
    "Правила работы:\n"
    "1. По ходу работы всегда в чат писать только: «+» — "
    "сигнал, что работа идёт.\n"
    "2. Как только проект будет завершён, или ты не можешь продолжить, "
    "или не знаешь, как продолжать — напиши в конце сообщения: «КОНЕЦ». "
    "Тогда работа над этим проектом останавливается.\n"
    "3. «+» от меня = продолжай работу, не останавливайся и не переспрашивай."
)

# Первое сообщение нового чата в проекте (старт или handoff)
NEW_CHAT_HEADER = (
    "@GitHub {repo_name}\n"
    "Продолжаем работать над проектом.\n"
)

INIT_TEMPLATE = (
    NEW_CHAT_HEADER
    + "\n"
    + PROTOCOL
    + "\n\n"
    + "Репозиторий: {repo_url}\n"
    "GitHub: {github_pat}\n"
    "Проанализируй весь доступный контекст: читай README.md, docs/ROADMAP.md, "
    "статус, историю коммитов в репозитории.\n"
    "Состояние:\n{repo_summary}\n"
    "Работай над проектом в этом чате до завершения. Каждый шаг пиши на русском."
)

HANDOFF_TEMPLATE = (
    NEW_CHAT_HEADER
    + "\n"
    + PROTOCOL
    + "\n\n"
    + "(Контекст прошлого чата разросся — новый чат в папке проекта, "
    "продолжение работы.)\n"
    "Репозиторий: {repo_url}\n"
    "GitHub: {github_pat}\n"
    "Состояние:\n{repo_summary}\n"
    "Продолжи с текущего состояния: выбери следующую задачу, выполни, "
    "протестируй, исправь регрессии и подготовь изменения к push. "
    "Каждый шаг пиши на русском."
)

# По ходу работы — только «+», как Jo делает в чатах
CONTINUE_TEMPLATES = [
    "+",
]

# ---------------------------------------------------------------- эвристики

CTX_FULL_RE = re.compile(
    r"(context (?:length|window|limit)|too (?:long|large)|maximum context"
    r"|максимальн\w* (?:длин\w*|контекст\w*)|лимит контекста|контекст закончил)", re.I)

# «КОНЕЦ» в конце сообщения (работа над проектом останавливается)
KONEC_TAIL_RE = re.compile(r"конец[\s»\"'’)\].!]*$", re.I)


def is_konec(text: str | None) -> bool:
    """«КОНЕЦ» в конце сообщения → остановить работу над проектом."""
    if not text:
        return False
    return bool(KONEC_TAIL_RE.search(text.strip()))


def decide(last_reply: str | None) -> str:
    """Что писать дальше: по ходу работы — только «+»;
    OVERFLOW — контекст разросся, пересоздать чат."""
    if last_reply and CTX_FULL_RE.search(last_reply):
        return "OVERFLOW"
    return "CONTINUE"


def compose(kind: str, ctx: dict, n: int) -> str:
    """Собрать сообщение в стиле Jo. ctx: repo_name, repo_url, github_pat,
    repo_summary, tasks."""
    if kind == "INIT":
        return INIT_TEMPLATE.format(
            repo_name=ctx["repo_name"], repo_url=ctx["repo_url"],
            github_pat=ctx.get("github_pat", ""),
            repo_summary=ctx.get("repo_summary", "см. репозиторий"))
    if kind == "HANDOFF":
        return HANDOFF_TEMPLATE.format(
            repo_name=ctx["repo_name"], repo_url=ctx["repo_url"],
            github_pat=ctx.get("github_pat", ""),
            repo_summary=ctx.get("repo_summary", "см. репозиторий"))
    # CONTINUE — только «+» (протокол работы)
    return CONTINUE_TEMPLATES[n % len(CONTINUE_TEMPLATES)]
