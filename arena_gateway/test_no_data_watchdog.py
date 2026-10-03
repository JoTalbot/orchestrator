#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Офлайн-проверка вотчдогов и очереди cleanup шлюза. Арену не трогает.

Проверяем ровно те два дефекта, из-за которых 02.10.2026 пробник моделей
сжёг дневной бюджет шестью запросами по 300 с:

1. «статус пришёл, данных нет» — раньше проверка первого байта срабатывала
   только когда HTTP-статуса ещё не было, а вотчдог простоя — только после
   первого байта. Запрос между ними висел до общего дедлайна. Теперь есть
   NO_DATA_TIMEOUT.
2. невыполненные удаления чатов копились (cleanup_fail 67) — теперь они
   попадают в очередь повторов и разгребаются после следующих запросов.

Запуск:  python3 arena_gateway/test_no_data_watchdog.py
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "arena_agent"))

import config as C  # noqa: E402

TMP = tempfile.mkdtemp(prefix="agw-test-")
C.STATE = os.path.join(TMP, "state.json")
C.CLEANUP_PENDING = os.path.join(TMP, "cleanup_pending.json")
C.VERIFIED = os.path.join(TMP, "verified.json")
C.DATA_DIR = TMP

from engine import ArenaEngine, ArenaError  # noqa: E402


class FakeTab:
    """Подменяет движок: отвечает как страница, без CDP и без арены."""

    def __init__(self, script):
        self.script = script          # список ответов __agwPoll по порядку
        self.calls = []
        self.poll = 0

    async def _install_js(self):
        return "ok"

    async def js(self, expr, timeout=120):
        self.calls.append(expr)
        if "__agwStart" in expr:
            return "ok"
        if "__agwPoll" in expr:
            if self.poll < len(self.script):
                out = self.script[self.poll]
                self.poll += 1
                return json.dumps(out)
            return json.dumps(self.script[-1])
        return "ok"                    # __agwCancel / __agwFree / __agwAction


def engine_with(script):
    eng = ArenaEngine(C)
    eng._install_js = script._install_js
    eng.js = script.js
    eng.ensure_tab = lambda *a, **k: asyncio.sleep(0)
    return eng


async def case_no_data():
    """Статус 200 пришёл, дельты не идут → быстрый 504 no_data, а не 300 с."""
    tab = FakeTab([{"status": 200, "len": 0, "chunk": "", "done": False}])
    eng = engine_with(tab)
    t0 = asyncio.get_event_loop().time()
    try:
        async for _ in eng._run_job("/nextjs-api/stream/create-evaluation", {},
                                    total_deadline=time.time() + 300,
                                    no_data_timeout=0.6):
            pass
        raise AssertionError("ожидали ArenaError")
    except ArenaError as e:
        took = asyncio.get_event_loop().time() - t0
        assert e.code == "no_data", "код ошибки: %s" % e.code
        assert took < 5, "вотчдог не сработал быстро: %.1f с" % took
        assert any("__agwCancel" in c for c in tab.calls), "задание не отменено"
        print("1. «статус есть, данных нет» → 504 %s за %.1f с (отмена задания отправлена)"
              % (e.code, took))


async def case_stream_ok():
    """Нормальный поток: дельты и finish — ошибок нет."""
    tab = FakeTab([
        {"status": 200, "len": 0, "chunk": "", "done": False},
        {"status": 200, "len": 8, "chunk": 'a0:"ОК"\n', "done": False},
        {"status": 200, "len": 24, "chunk": 'ad:{"finishReason"}\n',
         "done": True, "ms": 900, "len": 24},
    ])
    eng = engine_with(tab)
    parts = []
    async for ev in eng._run_job("/nextjs-api/stream/create-evaluation", {},
                                 total_deadline=time.time() + 30, no_data_timeout=1):
        parts.append(ev.get("type"))
    assert "first_byte" in parts and "part" in parts and "job_done" in parts, parts
    print("2. нормальный поток: события %s" % ", ".join(parts))


async def case_cleanup_queue():
    """Неудачное удаление чата попадает в очередь, успех — убирает из неё."""
    eng = ArenaEngine(C)
    fails = {"n": 0}

    async def fake_action(name, args, path=None):
        fails["n"] += 1
        return {"status": 500, "body": "boom"} if fails["n"] == 1 else {"status": 200}

    eng.server_action = fake_action
    await eng.cleanup("eval-1")
    assert eng.pending_cleanup == ["eval-1"], eng.pending_cleanup
    assert os.path.exists(C.CLEANUP_PENDING), "очередь не сохранилась на диск"
    print("3. неудача cleanup → в очереди: %s (файл записан)" % eng.pending_cleanup)

    done = await eng.retry_pending_cleanup(limit=2)
    assert done == 1 and eng.pending_cleanup == [], (done, eng.pending_cleanup)
    print("4. повтор cleanup → очередь пуста (удалено записей: %d)" % done)

    eng2 = ArenaEngine(C)              # переживает рестарт: читаем с диска
    await eng2.cleanup("eval-2")
    eng3 = ArenaEngine(C)
    eng3.server_action = fake_action
    print("5. очередь читается после перезапуска движка: %s" % eng3.pending_cleanup)
    assert eng3.pending_cleanup == ["eval-2"], eng3.pending_cleanup


async def case_budget_rollover():
    """Бюджет должен переезжать на новые UTC-сутки ДО запроса, иначе пробник
    (он проверяет остаток заранее) блокируется навсегда на «вчера 40/40»."""
    import time as _t
    eng = ArenaEngine(C)
    eng.budget = {"day": _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() - 86400)), "used": 40}
    left_before = eng.budget_left()
    assert left_before == int(C.DAILY_BUDGET), "сутки не сменились: осталось %s" % left_before
    assert eng.budget["day"] == _t.strftime("%Y-%m-%d", _t.gmtime()), eng.budget
    print("6. бюджет переехал на новые сутки: было 40/40 → осталось %d" % left_before)

    lim = C.DAILY_BUDGET
    C.DAILY_BUDGET = 5
    eng.cfg.DAILY_BUDGET = 5
    eng.budget = {"day": _t.strftime("%Y-%m-%d", _t.gmtime()), "used": 3}
    assert eng.budget_left() == 2, eng.budget_left()
    print("7. остаток считается от лимита: 5 - 3 = %d" % eng.budget_left())
    eng.cfg.DAILY_BUDGET = lim
    C.DAILY_BUDGET = lim


async def case_state_sync():
    """Состояние, записанное другим процессом (ctl.py/validate.sh), не теряется:
    бюджет берём по максимуму, кулдаун — по самой поздней границе."""
    import time as _t
    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    eng = ArenaEngine(C)
    eng.budget = {"day": today, "used": 2}
    eng.cooldown_until = 0.0
    # «другой процесс» записал 30 обращений и кулдаун на 600 с вперёд
    json.dump({"budget": {"day": today, "used": 30},
               "cooldown_until": _t.time() + 600, "interval": 300,
               "counters": {}}, open(C.STATE, "w"))
    eng._sync_state()
    assert eng.budget["used"] == 30, eng.budget
    assert eng.cooldown_until > _t.time() + 500, eng.cooldown_until
    assert eng.budget_left() == int(C.DAILY_BUDGET) - 30, eng.budget_left()
    print("8. состояние из файла подхвачено: бюджет %s/40, кулдаун %.0f с"
          % (eng.budget["used"], eng.cooldown_until - _t.time()))


async def case_prompt_block_streak():
    """Серия отказов «prompt failed» — это флаг аккаунта: после N подряд шлюз
    обязан уйти в длинную паузу, а не «тыкать» арену дальше (каждая попытка
    продлевает флаг)."""
    import time as _t
    eng = ArenaEngine(C)
    eng.cooldown_until = 0.0
    eng.prompt_failed_streak = 0
    lim = int(getattr(C, "PROMPT_BLOCK_STREAK", 3))
    for i in range(lim):
        eng.prompt_failed_streak += 1
    assert eng.prompt_failed_streak >= lim, eng.prompt_failed_streak
    eng.pause(int(getattr(C, "BLOCK_PAUSE", 7200)))
    left = eng.cooldown_until - _t.time()
    assert left > 7000, left
    print("9. серия %d отказов промпта → пауза %.0f с (больше не тратим попытки)"
          % (lim, left))
    eng.pause(0)
    assert eng.cooldown_until == 0 and eng.recaptcha_streak == 0
    print("10. пауза снята вручную: кулдаун 0, серия обнулена")


def case_supports():
    """Модальность: каталог арены смешанный, «чат» обязан требовать текстовый вывод.

    Реальный случай 03.10.2026: lhotse — это видео-модель (dreamina-seedance),
    в chat-пробнике арена отвечала 400 «Chosen Model(s) are no longer available»,
    а выглядело это как устаревший каталог.
    """
    from models import Model
    video = Model({"id": "v", "publicName": "lhotse", "userSelectable": True,
                   "capabilities": {"outputCapabilities": {"video": True},
                                    "inputCapabilities": {"text": True}},
                   "rankByModality": {"video": 7}})
    chat = Model({"id": "c", "publicName": "claude-sonnet-5", "userSelectable": True,
                  "capabilities": {"outputCapabilities": {"text": True, "web": True}},
                  "rankByModality": {"chat": 12}})
    search = Model({"id": "s", "publicName": "grok-4.5-search", "userSelectable": True,
                    "capabilities": {"outputCapabilities": {"search": True}},
                    "rankByModality": {"search": 5}})
    assert chat.supports("chat") and not video.supports("chat"), "chat-фильтр"
    assert video.supports("video") and not chat.supports("video"), "video-фильтр"
    assert search.supports("search") and not search.supports("chat"), "search-фильтр"
    assert chat.supports("auto") and chat.supports(None), "auto пропускает всех"
    print("11. модальности: видео-модель не идёт в chat, search — только в search")


def case_rsc_json():
    """Ответ server action разбирается в объект (нужно для generateUploadUrl)."""
    from engine import ArenaEngine
    raw = '1:{"success":true,"data":{"uploadUrl":"https://s3/x?a=1&b=2","key":"up/1.png"}}'
    obj = ArenaEngine._json_from_rsc(raw, want="uploadUrl")
    assert obj and obj["success"] and obj["data"]["key"] == "up/1.png", obj
    # тот же ответ, но завёрнутый в RSC-строку с экранированием
    esc = json.dumps(raw)
    obj2 = ArenaEngine._json_from_rsc(esc, want="uploadUrl")
    assert obj2 and obj2["data"]["uploadUrl"].startswith("https://"), obj2
    print("12. server action: JSON вынимается из RSC-тела (в т.ч. экранированного)")


def case_images():
    """Картинки: сбор из сообщений, data:-URL, текст-заглушка, картинка без подписи."""
    import app as A
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "что на фото?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    imgs = A.collect_images(msgs)
    assert len(imgs) == 1 and imgs[0]["url"].startswith("data:image/png"), imgs
    data, mime = A._decode_data_url("data:image/jpeg;base64,QUJD")
    assert data == b"ABC" and mime == "image/jpeg", (data, mime)
    prompt = A.build_prompt(msgs)
    assert "[изображение]" in prompt and "не поддерживается" not in prompt, prompt
    only_img = [{"role": "user", "content": [
        {"type": "image_url", "image_url": "https://x/y.png"}]}]
    p2 = A.build_prompt(only_img)
    assert "[изображение]" in p2 and "не поддерживается" not in p2, p2
    assert A.collect_images(only_img)[0]["url"] == "https://x/y.png"
    # ссылка на картинку прямо в тексте тоже становится вложением
    link = [{"role": "user", "content": "что тут? https://ex.com/a/photo.png спасибо"}]
    assert A.collect_images(link)[0]["url"].endswith("photo.png")
    print("13. vision: картинки собираются из messages, data:-URL декодируется, "
          "картинка без подписи не роняет запрос")


def main():
    import time as _t
    globals()["time"] = _t
    asyncio.run(case_no_data())
    asyncio.run(case_stream_ok())
    asyncio.run(case_cleanup_queue())
    asyncio.run(case_budget_rollover())
    asyncio.run(case_state_sync())
    asyncio.run(case_prompt_block_streak())
    case_supports()
    case_rsc_json()
    case_images()
    print("\nОК: вотчдог «нет данных» и очередь cleanup работают офлайн")
    return 0


if __name__ == "__main__":
    sys.exit(main())
