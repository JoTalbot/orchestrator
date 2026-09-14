#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стиль Jo: шаблоны сообщений и эвристики решений (по PROFILE.md)."""

import re

# ---------------------------------------------------------------- шаблоны

INIT_TEMPLATE = (
    "Проект: {repo_name}\n"
    "{repo_url}\n"
    "GitHub: {github_pat}\n"
    "Читай README.md, docs/ROADMAP.md, статус в репозитории.\n"
    "Продолжи работу над проектом. Каждый шаг пиши на русском.\n"
    "Статус сохраняй в репозиторий.\n"
    "В конце — один файл-отчёт: кратко, но всё.\n"
    "Делай всё сам, одним батчем.\n"
    "Правило: «+» от меня = продолжай работу, не останавливайся и не переспрашивай."
)

HANDOFF_TEMPLATE = (
    "Продолжи проект {repo_name} с текущего состояния. Контекст нового чата пустой.\n"
    "{repo_url}\n"
    "GitHub: {github_pat}\n"
    "Проанализируй весь доступный контекст: читай README.md, docs/ROADMAP.md, "
    "статус, историю коммитов в репозитории.\n"
    "Состояние:\n{repo_summary}\n"
    "Выбери следующие независимые задачи, выполни их, протестируй, "
    "исправь регрессии и подготовь изменения к push.\n"
    "Каждый шаг пиши на русском. В конце — один файл-отчёт: кратко, но всё.\n"
    "Делай всё сам, одним батчем.\n"
    "Правило: «+» от меня = продолжай работу, не останавливайся и не переспрашивай."
)

# Продолжение работы — просто «+», как Jo делает в чатах
CONTINUE_TEMPLATES = [
    "+",
    "+",
    "+",
    "+",
]

APPROVE_TEMPLATES = [
    "Да. Делай всё сам.",
    "Даю добро. Делай всё.",
    "Да. Продолжай.",
    "Да. Делай всё сам и продолжай.",
]

STATUS_TEMPLATES = [
    "Статус. Что сделано, что дальше. Кратко.",
    "Статус. Что сделал, что осталось. Кратко.",
]

RETRY_TEMPLATES = [
    "Продолжи. Если была ошибка — разберись и исправь сам.",
    "Продолжи. Ошибки чини сам, не останавливайся.",
]

# ---------------------------------------------------------------- эвристики

ASK_RE = re.compile(
    r"(нужно (?:тво[её] )?(?:решение|подтверждение|добро|согласие)"
    r"|какой вариант|выбери|подтверди|продолжать\?"
    r"|жду (?:тво[её]го )?(?:ответа|решения|добра)"
    r"|нужен доступ|нужен токен|нужен ключ|нужны доступы"
    r"|могу ли|дать добро|одобри|жд[уё] от тебя)", re.I)
DONE_RE = re.compile(
    r"\b(готово|выполнено|завершен|сделано|выполнил|завершил|done|completed)\b", re.I)
CTX_FULL_RE = re.compile(
    r"(context (?:length|window|limit)|too (?:long|large)|maximum context"
    r"|максимальн\w* (?:длин\w*|контекст\w*)|лимит контекста|контекст закончил)", re.I)
ERR_RE = re.compile(
    r"\b(error|ошибка|failed|failure|traceback|exception)\b", re.I)


def decide(last_reply: str | None) -> str:
    """Что написать дальше: APPROVE / STATUS / RETRY / OVERFLOW / CONTINUE."""
    if not last_reply:
        return "CONTINUE"
    t = last_reply
    if CTX_FULL_RE.search(t):
        return "OVERFLOW"
    if ASK_RE.search(t):
        return "APPROVE"
    if DONE_RE.search(t) and len(t) < 1500:
        return "STATUS"
    if ERR_RE.search(t) and len(t) < 1500:
        return "RETRY"
    return "CONTINUE"


def compose(kind: str, ctx: dict, n: int) -> str:
    """Собрать сообщение в стиле Jo. ctx: repo_name, repo_url, github_pat,
    repo_summary, tasks."""
    if kind == "INIT":
        return INIT_TEMPLATE.format(
            repo_name=ctx["repo_name"], repo_url=ctx["repo_url"],
            github_pat=ctx.get("github_pat", ""))
    if kind == "HANDOFF":
        return HANDOFF_TEMPLATE.format(
            repo_name=ctx["repo_name"], repo_url=ctx["repo_url"],
            github_pat=ctx.get("github_pat", ""),
            repo_summary=ctx.get("repo_summary", "см. репозиторий"))
    if kind == "APPROVE":
        return APPROVE_TEMPLATES[n % len(APPROVE_TEMPLATES)]
    if kind == "STATUS":
        return STATUS_TEMPLATES[n % len(STATUS_TEMPLATES)]
    if kind == "RETRY":
        return RETRY_TEMPLATES[n % len(RETRY_TEMPLATES)]
    # CONTINUE — просто «+»
    return CONTINUE_TEMPLATES[n % len(CONTINUE_TEMPLATES)]
