#!/usr/bin/env python3
"""Выгружаем каталог моделей arena.ai из initialModels (RSC страницы /leaderboard/agent).

Внутри пейлоада страницы лежит полный реестр моделей площадки: id (UUID),
organization, provider, publicName, name, displayName, capabilities
(вход: text/image/file, выход: text/web/image/search/video), userSelectable,
rank и rankByModality (chat/webdev/image/search/video). На 17.09.2026 — 1074 записи.

Это каталог батл-режимов; список моделей агент-режима отдаёт
GET /api/chat/agent-models, который закрыт флагом agent-model-selector (403).
Передача modelId в create-chat без флага тоже даёт 403 «Not allowed».

Запуск:  .venv/bin/python arena_agent/dump_models.py
Результат: data/arena/models_catalog.json (+ REST: GET /models/catalog)

НЕ РАБОТАЕТ с 03.10.2026: страница /leaderboard/agent больше не отдаёт каталог
инлайном — в RSC лежит `"initialModels":"$undefined"`, то есть список грузится
клиентом отдельным запросом. Что известно на 03.10.2026:

* на /leaderboard в пейлоаде есть публичный список моделей, но в новом виде:
  `{"key":"claude-sonnet-5-5-max-agent","displayName":"Claude Sonnet 5.5",
    "organization":"Anthropic","arenas":[{"slug":"code","rank":3,...}]}` —
  ключи-слаги, без UUID, которые нужны `modelAId`;
* `GET /api/chat/agent-models` существует, но закрыт флагом `agent-model-selector` (403);
* `GET /api/chat/models` существует и требует валидный UUID в параметре `id`
  (без него 400 ZodError), то есть это список моделей уровня чата, а не каталог.

Следующий шаг: найти актуальный источник каталога (сканирование бандлов —
`arena_agent/scan_api.py scan --download`, поиск вызова, отдающего `initialModels`)
и обновить этот скрипт. Прогон при неработающем источнике безопасен: файл не
перезаписывается, если моделей разобрано 0.
"""
import asyncio
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_api import ArenaAPI, connect  # noqa: E402

OUT = os.environ.get("ARENA_MODELS_OUT",
                     "/opt/orchestrator/data/arena/models_catalog.json")


def extract_array(html, key):
    i = html.find('"%s":[' % key)
    if i < 0:
        return None
    j = html.find("[", i)
    depth, in_str, esc = 0, False, False
    for k in range(j, min(len(html), j + 4_000_000)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return html[j:k + 1]
    return None


async def main():
    tab = await connect(own=True, verbose=False)
    api = ArenaAPI(tab, verbose=False, own_tab=True)
    try:
        r = await api.fetch("GET", "/leaderboard/agent",
                            headers={"accept": "text/html,application/xhtml+xml"})
        html = (r["body"] or "").replace('\\"', '"')
        blob = extract_array(html, "initialModels")
        print("страница:", r["status"], "| initialModels:", len(blob or ""), "символов")
        models = []
        if blob:
            try:
                models = json.loads(blob)
            except Exception as e:
                print("  json не целиком (%s) — разбираю по объектам" % str(e)[:80])
        if not models:
            for m in re.finditer(r'\{"id":"[0-9a-f-]{36}","organization".*?\}(?=,\{"id"|\]$)',
                                 blob or "", re.S):
                try:
                    models.append(json.loads(m.group(0)))
                except Exception:
                    pass
        print("моделей разобрано:", len(models))
        if models:
            keys = {}
            for m in models:
                for k in m:
                    keys[k] = keys.get(k, 0) + 1
            print("поля:", keys)
            mods = set()
            for m in models:
                mods.update((m.get("rankByModality") or {}).keys())
            print("модальности:", sorted(mods))
            sel = [m for m in models if m.get("userSelectable")]
            print("userSelectable=true:", len(sel))
            with open(OUT, "w") as f:
                json.dump({"savedAt": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                           "source": "/leaderboard/agent → initialModels",
                           "count": len(models), "models": models},
                          f, ensure_ascii=False, indent=1)
            print("сохранено:", OUT)
    finally:
        urllib.request.urlopen("http://127.0.0.1:9222/json/close/" + tab.target_id,
                              timeout=10)

asyncio.run(main())
