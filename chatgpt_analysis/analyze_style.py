#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Анализ стиля работы пользователя по всем собранным чатам ChatGPT.

Читает data/light/*.json, берёт только сообщения роли 'user' (т.е. реальные
сообщения Jo в чатах) и считает:
  * объём/длину сообщений, распределение;
  * язык (рус/укр/англ), форматирование (код, списки, эмодзи, ссылки);
  * директивные глаголы (сделай/проверь/запусти/...), слова-маркеры
    (итог, статус, файл, коммит, деплой...), вежливость/приветствия;
  * первые/последние слова сообщений, часовая активность;
  * топ английских терминов;
  * выборку типичных сообщений (для PROFILE).

Результаты:
  agent_profile/metrics.json               — все метрики;
  agent_profile/samples_user_messages.md   — 60 типичных сообщений.
"""

import json
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("data")
OUT = Path("agent_profile")
LIGHT = DATA / "light"
INDEX = json.loads((DATA / "index.json").read_text(encoding="utf-8"))

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE0F\u200d\U00002764]"
)
UA_LETTERS = set("іїєґІЇЄҐ")
CYR_RE = re.compile("[А-Яа-яЁёІіЇїЄєҐґ]")
LATIN_RE = re.compile("[A-Za-z]")
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9_']+")

# ---------------------------------------------------------------- утилиты

def lang_of(t: str) -> str:
    ua = sum(1 for ch in t if ch in UA_LETTERS)
    cyr = len(CYR_RE.findall(t))
    lat = len(LATIN_RE.findall(t))
    if ua >= 2 and ua / max(1, cyr) >= 0.03:
        return "ua"
    if cyr > lat:
        return "ru"
    if lat > 0:
        return "en"
    return "other"


def tokens(t: str) -> list[str]:
    return TOKEN_RE.findall(t.lower())


# директивные глаголы (стемы) -> нормальная форма
VERB_STEMS = {
    "сдела": "сделать", "провер": "проверить", "посмотр": "посмотреть",
    "запуст": "запустить", "исправ": "исправить", "почин": "починить",
    "добав": "добавить", "удал": "удалить", "убр": "убрать",
    "покаж": "показать", "объясн": "объяснить", "расскаж": "рассказать",
    "найд": "найти", "проанализ": "проанализировать", "анализ": "анализировать",
    "разбер": "разобраться", "созда": "создать", "напиш": "написать",
    "сгенер": "сгенерировать", "клон": "клонировать", "депло": "деплой",
    "коммит": "коммит", "запуш": "запушить", "push": "push",
    "обнов": "обновить", "перезапуст": "перезапустить", "останов": "остановить",
    "продолж": "продолжить", "сохран": "сохранить", "запиш": "записать",
    "пришл": "прислать", "отправ": "отправить", "скача": "скачать",
    "установ": "установить", "настро": "настроить", "подключ": "подключить",
    "убед": "убедиться", "помн": "помнить", "подожд": "подождать",
    "сравн": "сравнить", "оцен": "оценить", "предлож": "предложить",
    "придума": "придумать", "давай": "давай", "нужно": "нужно",
    "надо": "надо", "хочу": "хочу", "попроб": "попробовать",
    "пофикс": "пофиксить", "вылож": "выложить", "разверн": "развернуть",
    "смерж": "смержить", "мердж": "мердж", "изуч": "изучить",
    "прочита": "прочитать", "загляд": "заглянуть", "зайд": "зайти",
    "постав": "поставить", "помен": "поменять", "измен": "изменить",
    "перепиш": "переписать", "передел": "переделать", "додел": "доделать",
    "тестир": "тестировать", "протест": "протестировать", "отлад": "отладить",
    "дебаж": "дебажить", "собери": "собрать", "подними": "поднять",
    "очист": "очистить", "перемест": "переместить", "замени": "заменить",
    "забуд": "забыть", "займись": "заняться", "разреш": "разрешить",
    "реши": "решить", "узнай": "узнать", "уточни": "уточнить",
    "спроси": "спросить", "подтверд": "подтвердить", "согласу": "согласовать",
}

# слова-маркеры стиля работы
MARKERS = {
    "итог/резюме/кратко": ["итог", "резюме", "саммари", "кратк", "вывод", "подытож"],
    "статус/прогресс/отчёт": ["статус", "отчет", "отчёт", "прогресс", "результат", "что по", "как дела"],
    "файлы/код/репо": ["файл", "workspace", "репо", "репозитор", "скрипт", "код", "проект"],
    "проверка перед действием": ["убед", "уточни", "спроси", "подтверд", "согласу", "аккуратн", "осторожн"],
    "бэкап/безопасность": ["бэкап", "backup", "не слома", "безопасн", "секрет", "токен", "парол"],
    "пошаговость": ["сначала", "потом", "затем", "после", "далее", "шаг", "поэтап"],
    "спасибо/вежливость": ["спасибо", "благодар", "пожалуйста", "плиз", "please", "thx", "thanks"],
}

GREETINGS = ["привет", "здравств", "добрый", "доброе", "хай", "hi", "hello", "ку", "доброго"]

# ---------------------------------------------------------------- загрузка

print("Загрузка сообщений...", flush=True)
user_msgs = []          # {chat_id, chat_title, text, create_time}
role_counter = Counter()
empty_user = 0
for p in sorted(LIGHT.glob("*.json")):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    for m in data:
        role = m.get("role", "unknown")
        role_counter[role] += 1
        if role != "user":
            continue
        text = (m.get("text") or "").strip()
        if not text:
            empty_user += 1
            continue
        user_msgs.append({
            "chat_id": p.stem,
            "chat_title": INDEX.get(p.stem, {}).get("title", "?"),
            "text": text,
            "create_time": m.get("create_time"),
        })

print(f"Сообщений user: {len(user_msgs)}, пустых пропущено: {empty_user}")

# ---------------------------------------------------------------- метрики

n = len(user_msgs)
lens = [len(m["text"]) for m in user_msgs]
words = [len(tokens(m["text"])) for m in user_msgs]
no_code = [m["text"] for m in user_msgs if "```" not in m["text"]]
no_code_lens = [len(t) for t in no_code]

has_code = sum(1 for m in user_msgs if "```" in m["text"])
has_inline = sum(1 for m in user_msgs if "`" in m["text"])
has_bullet = sum(1 for m in user_msgs if re.search(r"(?m)^\s*[-*+•]\s+\S", m["text"]))
has_number = sum(1 for m in user_msgs if re.search(r"(?m)^\s*\d+[.)]\s+\S", m["text"]))
has_header = sum(1 for m in user_msgs if re.search(r"(?m)^#{1,3}\s", m["text"]))
has_emoji = sum(1 for m in user_msgs if EMOJI_RE.search(m["text"]))
has_url = sum(1 for m in user_msgs if re.search(r"https?://|www\.", m["text"]))
has_q = sum(1 for m in user_msgs if "?" in m["text"])
ends_q = sum(1 for m in user_msgs if m["text"].rstrip().endswith("?"))
ends_bang = sum(1 for m in user_msgs if m["text"].rstrip().endswith("!"))
multiline = sum(1 for m in user_msgs if m["text"].count("\n") >= 3)
caps = sum(1 for m in user_msgs if re.search(r"\b[А-ЯA-Z]{4,}\b", m["text"]))
dots = sum(1 for m in user_msgs if "..." in m["text"])

langs = Counter(lang_of(m["text"]) for m in user_msgs)

# глаголы и маркеры: и доля сообщений, и число вхождений
verb_hits = Counter()
marker_hits = Counter()
for m in user_msgs:
    toks = tokens(m["text"])
    tokset = set(toks)
    for stem, label in VERB_STEMS.items():
        if any(t.startswith(stem) for t in toks):
            verb_hits[label] += 1
    for label, subs in MARKERS.items():
        if any(any(s in t for t in toks) for s in subs):
            marker_hits[label] += 1

# приветствия в начале
greet_start = 0
for m in user_msgs:
    t = m["text"].lstrip().lower()
    if any(t.startswith(g) for g in GREETINGS):
        greet_start += 1

# первые/последние два слова
first2 = Counter()
last2 = Counter()
for m in user_msgs:
    t = tokens(m["text"])
    if len(t) >= 2:
        first2[" ".join(t[:2])] += 1
        last2[" ".join(t[-2:])] += 1

# часовая активность (UTC)
hours = Counter()
for m in user_msgs:
    ts = m.get("create_time")
    if isinstance(ts, (int, float)):
        hours[datetime.fromtimestamp(ts, tz=timezone.utc).hour] += 1

# английские термины (топ)
en_terms = Counter()
for m in user_msgs:
    toks = tokens(m["text"])
    seen = set()
    for t in toks:
        if t.isascii() and t.isalpha() and len(t) >= 4 and t not in seen:
            en_terms[t] += 1
            seen.add(t)

# сообщений на чат
per_chat = Counter(m["chat_id"] for m in user_msgs)
chat_vals = list(per_chat.values())

# типичная длина (без кода и без гигантских вставок логов)
typical = [x for x in no_code_lens if 10 <= x <= 2500]

metrics = {
    "сообщений_user": n,
    "роли": dict(role_counter),
    "пустых_user": empty_user,
    "длина_символов": {
        "средняя": round(statistics.mean(lens), 1),
        "медиана": round(statistics.median(lens), 1),
        "p90": round(sorted(lens)[int(n * 0.9)], 1),
        "p99": round(sorted(lens)[int(n * 0.99)], 1),
        "макс": max(lens),
    },
    "длина_типичных_без_кода": {
        "кол-во": len(typical),
        "средняя": round(statistics.mean(typical), 1),
        "медиана": round(statistics.median(typical), 1),
        "p90": round(sorted(typical)[int(len(typical) * 0.9)], 1),
    },
    "слов_в_сообщении_среднее": round(statistics.mean(words), 1),
    "форматирование_доля_%": {
        "с_код_блоком": round(100 * has_code / n, 1),
        "с_инлайн_кодом": round(100 * has_inline / n, 1),
        "со_списком": round(100 * has_bullet / n, 1),
        "с_нумерацией": round(100 * has_number / n, 1),
        "с_заголовком": round(100 * has_header / n, 1),
        "с_эмодзи": round(100 * has_emoji / n, 1),
        "с_ссылкой": round(100 * has_url / n, 1),
        "с_вопросом": round(100 * has_q / n, 1),
        "заканч_вопросом": round(100 * ends_q / n, 1),
        "заканч_восклицанием": round(100 * ends_bang / n, 1),
        "многострочные": round(100 * multiline / n, 1),
        "с_многоточием": round(100 * dots / n, 1),
        "с_КАПСОМ": round(100 * caps / n, 1),
    },
    "язык_доля_%": {k: round(100 * v / n, 1) for k, v in langs.most_common()},
    "с_приветствием_в_начале_%": round(100 * greet_start / n, 1),
    "глаголы_топ30": verb_hits.most_common(30),
    "маркеры_стиля": dict(marker_hits.most_common()),
    "первые_слова_топ15": first2.most_common(15),
    "последние_слова_топ15": last2.most_common(15),
    "английские_термины_топ25": en_terms.most_common(25),
    "часовая_активность_utc": {str(h): hours[h] for h in sorted(hours)},
    "сообщений_на_чат": {
        "медиана": statistics.median(chat_vals),
        "среднее": round(statistics.mean(chat_vals), 1),
        "макс": max(chat_vals),
    },
    "чатов_в_анализе": len(per_chat),
}

OUT.mkdir(exist_ok=True)
(OUT / "metrics.json").write_text(
    json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(metrics, ensure_ascii=False, indent=1))

# ---------------------------------------------------------------- примеры

# типичные сообщения: 120..1200 симв., без кода, с кириллицей
cand = [m for m in user_msgs
        if 120 <= len(m["text"]) <= 1200
        and "```" not in m["text"]
        and CYR_RE.search(m["text"])]
cand.sort(key=lambda m: -(m.get("create_time") or 0))
seen_chats = set()
picked = []
for m in cand:
    if len(picked) >= 60:
        break
    if m["chat_id"] in seen_chats:
        continue
    seen_chats.add(m["chat_id"])
    picked.append(m)

def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"

lines = ["# Типичные сообщения Jo (автовыборка из чатов)\n",
         "Отобраны автоматически: 120–1200 символов, без кода, с кириллицей, "
         "не больше одного на чат.\n"]
for i, m in enumerate(picked[:40], 1):
    lines.append(f"## {i}. [{fmt_ts(m.get('create_time'))}] "
                 f"{m['chat_title'][:60]}\n\n{m['text'][:1200]}\n")
(OUT / "samples_user_messages.md").write_text("\n".join(lines), encoding="utf-8")
print(f"\nПримеров сохранено: {len(picked[:40])} -> {OUT / 'samples_user_messages.md'}")
