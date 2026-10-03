#!/usr/bin/env python3
"""Выгружаем каталог моделей arena.ai из initialModels (RSC потока /text/direct).

ИСТОЧНИК (найден 03.10.2026): `GET https://arena.ai/text/direct` с заголовком
`RSC: 1` — поток React Server Components отдаёт полный массив `initialModels`:
id (UUID), organization, provider, publicName, name, displayName, capabilities
(вход: text/image/file, выход: text/web/image/search/video), userSelectable,
rankByModality (chat/webdev/image/search/video). На 03.10.2026 — 292 записи,
все userSelectable.

Почему так: прежний источник `/leaderboard/agent` перестал работать —
в его RSC теперь `"initialModels":"$undefined"`, список грузится клиентом
(см. docs/ARENA_GATEWAY.md, раздел «каталог моделей»). Как источник найден:
`arena_agent/scan_api_surface.py` (читает JS-бандлы) + `scan_net_surface.py`
(CDP Network.* на /text/direct — видно, что JSON-API моделей не вызывается,
значит список приезжает в RSC) + `scan_rsc_models.py` (сравнение RSC-потоков
маршрутов: /text/direct содержит initialModels, /leaderboard и /agent — нет).

ВАЖНО: каталог быстро устаревает — арена и удаляет модели, и переименовывает
их (в разборе 03.10.2026 видно `displayName: "dreamina-seedance-2.5-720p"` при
`publicName: lhotse`). Из старого каталога от 17.09.2026 исчезли 784 id, включая
рабочую `claude-sonnet-4-5-20250929`, а часть оставшихся записей меняла смысл.
Отдельная ловушка: 400 «Chosen Model(s) are no longer available» приходит и при
ВЕРНОМ id, если модель не поддерживает запрошенный режим (например, видео-модель
lhotse в режиме chat) — см. `arena_gateway/models.py::Model.supports`.
Поэтому каталог надо обновлять регулярно, а режим проверять до запроса.

Запуск (на сервере, где живёт CDP :9222):
    .venv/bin/python arena_agent/dump_models.py            # выгрузить и записать
    .venv/bin/python arena_agent/dump_models.py --check    # только посмотреть
Результат: data/arena/models_catalog.json (+ REST: GET /models/catalog).
Скрипт не перезаписывает каталог, если разобрал 0 моделей; старый файл
сохраняется рядом как models_catalog.json.bak.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_api import ArenaAPI, connect  # noqa: E402

OUT = os.environ.get("ARENA_MODELS_OUT",
                     "/opt/orchestrator/data/arena/models_catalog.json")
ROUTE = os.environ.get("ARENA_MODELS_ROUTE", "/text/direct")


def extract_array(text, key):
    i = text.find('"%s":[' % key)
    if i < 0:
        return None
    j = text.find("[", i)
    depth, in_str, esc = 0, False, False
    for k in range(j, min(len(text), j + 4_000_000)):
        c = text[k]
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
                return text[j:k + 1]
    return None


def stats(models):
    mods = {}
    for m in models:
        for k, v in (m.get("rankByModality") or {}).items():
            if isinstance(v, (int, float)) and v < 9e15:
                mods[k] = mods.get(k, 0) + 1
    return {"всего": len(models),
            "selectable": sum(1 for m in models if m.get("userSelectable")),
            "по модальностям": mods}


async def main():
    check_only = "--check" in sys.argv
    tab = await connect(own=True, verbose=False)
    api = ArenaAPI(tab, verbose=False, own_tab=True)
    try:
        r = await api.fetch("GET", ROUTE, headers={"RSC": "1"})
        body = (r["body"] or "").replace('\\"', '"')
        print("страница: %s | тело %d символов | initialModels: %d символов"
              % (r["status"], len(body), len(extract_array(body, "initialModels") or "")))
        blob = extract_array(body, "initialModels")
        models = []
        if blob:
            try:
                models = json.loads(blob)
            except Exception as e:
                print("  json не целиком (%s) — разбираю по объектам" % str(e)[:80])
        if not models:
            import re
            for m in re.finditer(r'\{"id":"[0-9a-f-]{36}",.*?\}(?=,\{"id"|\]$)',
                                 blob or "", re.S):
                try:
                    models.append(json.loads(m.group(0)))
                except Exception:
                    pass
        st = stats(models)
        print("моделей разобрано:", st["всего"], "| selectable:", st["selectable"])
        print("модальности:", st["по модальностям"])
        old_count = None
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            old = old["models"] if isinstance(old, dict) else old
            old_count = len(old)
        except Exception:
            pass
        if not models:
            print("ОТКАЗ: 0 моделей — каталог не перезаписываю (источник изменился?)")
            return 1
        if check_only:
            print("--check: ничего не записываю (в файле было %s)" % old_count)
            return 0
        if old_count is not None and os.path.exists(OUT):
            os.replace(OUT, OUT + ".bak")
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "source": "%s → initialModels (RSC: 1)" % ROUTE,
                       "count": len(models), "models": models},
                      f, ensure_ascii=False, indent=1)
        print("сохранено: %s (было %s моделей, стало %d; бэкап %s.bak)"
              % (OUT, old_count, len(models), OUT))
        return 0
    finally:
        try:
            tab_ws = tab.ws_url
            del tab_ws
        except Exception:
            pass
        await tab.close()
        if tab.target_id:
            from arena_api import close_tab
            close_tab(tab.target_id)   # не закроет последнюю вкладку браузера


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
