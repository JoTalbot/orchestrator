#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стиль Jo: шаблоны сообщений и эвристики решений (по PROFILE.md).

Протокол работы:
  * по ходу работы — в чат писать только «Продолжить»;
  * «КОНЕЦ» в ответе ассистента — работа над проектом завершена;
  * первое сообщение нового чата в проекте —
    «@GitHub JoTalbot/<проект>» + инструкции.
"""

import re

# ---------------------------------------------------------------- правила

# Первое сообщение нового чата — ТОЛЬКО эта строка, без лишнего текста
INIT_TEMPLATE = (
    "@GitHub {repo_name} При завершении работ над проектом писать "
    "в конце сообщения: КОНЕЦ. Во время работы слово КОНЕЦ не использовать."
)

HANDOFF_TEMPLATE = (
    "@GitHub {repo_name} При завершении работ над проектом писать "
    "в конце сообщения: КОНЕЦ. Во время работы слово КОНЕЦ не использовать."
    "\n\n"
    "(Контекст прошлого чата разросся — новый чат, продолжение работы.)"
)

# По ходу работы — «Продолжить»
CONTINUE_TEMPLATES = [
    "Продолжить",
]

# ---------------------------------------------------------------- эвристики

CTX_FULL_RE = re.compile(
    r"(context (?:length|window|limit)|too (?:long|large)|maximum context"
    r"|максимальн\w* (?:длин\w*|контекст\w*)|лимит контекста|контекст закончил)", re.I)

# «КОНЕЦ» — ищем во всём тексте ответа
KONEC_RE = re.compile(r"\bКОНЕЦ\b", re.I)


def is_konec(text: str | None) -> bool:
    """«КОНЕЦ» в ответе ассистента → остановить работу над проектом."""
    if not text:
        return False
    return bool(KONEC_RE.search(text))


def decide(last_reply: str | None) -> str:
    """Что писать дальше: по ходу работы — «Продолжить»;
    OVERFLOW — контекст разросся, пересоздать чат."""
    if last_reply and CTX_FULL_RE.search(last_reply):
        return "OVERFLOW"
    return "CONTINUE"


def compose(kind: str, ctx: dict, n: int) -> str:
    """Собрать сообщение в стиле Jo. ctx: repo_name, repo_url, github_pat,
    repo_summary, tasks."""
    if kind == "INIT":
        return INIT_TEMPLATE.format(repo_name=ctx["repo_name"])
    if kind == "HANDOFF":
        return HANDOFF_TEMPLATE.format(repo_name=ctx["repo_name"])
    # CONTINUE — «Продолжить» (протокол работы)
    return CONTINUE_TEMPLATES[n % len(CONTINUE_TEMPLATES)]
