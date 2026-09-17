#!/usr/bin/env python3
"""Офлайн-проверка логики автоблокировки (не трогает арену и боевое состояние)."""
import sys, time
sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, "/opt/orchestrator/arena_gateway")
import config as C
C.STATE = "/tmp/test_gateway_state.json"
from engine import ArenaEngine

eng = ArenaEngine(C)
eng.recaptcha_streak = 0
eng.cooldown_until = 0.0
eng.security_blocked = False

pen1 = eng._on_recaptcha_fail()
print("1-й отказ: штраф %d с, blocked=%s, streak=%d, интервал=%d с"
      % (pen1, eng.security_blocked, eng.recaptcha_streak, eng.interval))
assert not eng.security_blocked and pen1 == int(C.RECAPTCHA_PENALTY), "первый отказ не должен блокировать"

pen2 = eng._on_recaptcha_fail()
print("2-й отказ: штраф %d с, blocked=%s, streak=%d" % (pen2, eng.security_blocked, eng.recaptcha_streak))
assert eng.security_blocked, "серия из %d отказов должна включать блокировку" % C.BLOCK_STREAK
assert pen2 >= int(C.BLOCK_PAUSE) - 5, "штраф должен быть не короче BLOCK_PAUSE"

rest = int(eng.cooldown_until - time.time())
print("кулдаун после блокировки: %d с" % rest)
assert rest > 3000

eng._speed_up()
print("после успеха: blocked=%s, streak=%d, интервал=%d с"
      % (eng.security_blocked, eng.recaptcha_streak, eng.interval))
assert not eng.security_blocked and eng.recaptcha_streak == 0

print("\nОК: серия 403 → автоблокировка (запросы не тратятся), успех → разблокировка")
