#!/usr/bin/env python3
"""
ctl.py — консольная отладка шлюза арены (без HTTP).

  health [--deep]                 состояние движка/вкладки/reCAPTCHA
  models [--all] [--verified] [--modality chat] [--filter ПОДСТРОКА] [--limit N]
  resolve ИМЯ                     во что разрешится имя модели
  ask "ТЕКСТ" [--model ИМЯ] [--modality chat] [--stream] [--keep]
  raw "ТЕКСТ" [--model ИМЯ] [--modality chat]   сырой поток (разведка протокола)
  probe [--limit N] [--models id1,id2] [--modality chat]
  v2                              проверка эскалации reCAPTCHA v2
  cleanup EVAL_ID                 удалить/архивировать чат
"""
import argparse, asyncio, json, os, sys, time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C
from engine import ArenaEngine, ArenaError
from models import Registry


def jdump(x):
    print(json.dumps(x, ensure_ascii=False, indent=1, default=str))


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("health"); p.add_argument("--deep", action="store_true")
    p = sub.add_parser("models"); p.add_argument("--all", action="store_true")
    p.add_argument("--verified", action="store_true"); p.add_argument("--modality")
    p.add_argument("--filter", dest="flt"); p.add_argument("--limit", type=int, default=40)
    p = sub.add_parser("resolve"); p.add_argument("name")
    p = sub.add_parser("ask"); p.add_argument("text"); p.add_argument("--model")
    p.add_argument("--modality"); p.add_argument("--stream", action="store_true")
    p.add_argument("--keep", action="store_true"); p.add_argument("--session")
    p = sub.add_parser("raw"); p.add_argument("text"); p.add_argument("--model")
    p.add_argument("--modality", default="chat")
    p = sub.add_parser("probe"); p.add_argument("--limit", type=int, default=6)
    p.add_argument("--models"); p.add_argument("--modality", default="chat")
    p = sub.add_parser("v2")
    p = sub.add_parser("cleanup"); p.add_argument("eval_id")
    a = ap.parse_args()

    reg = Registry(C.CATALOG, C.VERIFIED, os.path.join(C.DATA_DIR, "model_aliases.json"))
    eng = ArenaEngine(C)

    if a.cmd == "health":
        await eng.start()
        jdump(await eng.health(deep=a.deep))
        await eng.stop()
        return

    if a.cmd == "models":
        items = reg.openai_models(only_selectable=True,
                                  only_verified=a.verified and not a.all)
        if a.modality:
            items = [m for m in items if a.modality in m["arena_modalities"]]
        if a.flt:
            f = a.flt.lower()
            items = [m for m in items if f in (m["id"] or "").lower()
                     or f in (m["owned_by"] or "").lower()]
        items.sort(key=lambda m: (not m["arena_verified"], m["owned_by"], m["id"]))
        print("найдено: %d (всего в каталоге %d, verified %d)"
              % (len(items), len(reg.models),
                 sum(1 for m in reg.models if m.verified)))
        for m in items[:a.limit]:
            print("  %s%-38s %-26s %-18s %s" % (
                "✓ " if m["arena_verified"] else "  ",
                m["id"][:38], (m["owned_by"] or "")[:26],
                ",".join(m["arena_modalities"])[:18], m["arena_id"]))
        return

    if a.cmd == "resolve":
        m, mod, err = reg.resolve(a.name)
        if not m:
            print("НЕ РАЗРЕШЕНО:", err); return
        print("модель: %s\n  id: %s\n  провайдер: %s\n  модальности: %s\n  запрошенная: %s"
              % (m.public_name, m.id, m.provider, ",".join(m.modalities), mod))
        return

    if a.cmd in ("ask", "raw"):
        want = a.model or C.DEFAULT_MODEL
        m, mod, err = reg.resolve(want, getattr(a, "modality", None))
        if not m:
            print("модель не найдена:", err)
            print("подсказка: ctl.py models --verified")
            return
        print("модель: %s (%s) | модальность: %s" % (m.public_name, m.id, mod))
        await eng.start()
        t0 = time.time()
        if a.cmd == "ask" and a.stream:
            buf = []
            def on_delta(s):
                buf.append(s)
                print(s, end="", flush=True)
            try:
                res = await eng.evaluate(model_id=m.id, prompt=a.text, modality=mod,
                                         session_id=getattr(a, "session", None),
                                         extra={"on_delta": on_delta})
                print("\n---\n%.1f с | символов: %d | eval: %s"
                      % (time.time() - t0, len("".join(buf)), res.get("eval_id")))
                if not getattr(a, "keep", False):
                    print("cleanup:", await eng.cleanup(res.get("eval_id")))
            except ArenaError as e:
                print("\nОШИБКА %s/%s: %s (retry_after=%s)"
                      % (e.status, e.code, e.message, e.retry_after))
        else:
            try:
                res = await eng.evaluate(model_id=m.id, prompt=a.text, modality=mod)
                print("\n=== ответ (%.1f с, %s байт потока) ===" % (time.time() - t0, res.get("bytes")))
                print(res.get("text") or "<пусто>")
                if res.get("parts"):
                    print("\n=== прочие части потока (%d) ===" % len(res["parts"]))
                    print(json.dumps(res["parts"], ensure_ascii=False)[:4000])
                print("\neval_id:", res.get("eval_id"), "| finish:", res.get("finish"))
                if a.cmd == "raw":
                    print("неизвестные коды:", dict(eng.unknown_codes))
                if not getattr(a, "keep", False):
                    print("cleanup:", await eng.cleanup(res.get("eval_id")))
            except ArenaError as e:
                print("ОШИБКА %s/%s: %s (retry_after=%s)"
                      % (e.status, e.code, e.message, e.retry_after))
        jdump(await eng.health())
        await eng.stop()
        return

    if a.cmd == "probe":
        await eng.start()
        ids = [x.strip() for x in (a.models or "").split(",") if x.strip()]
        if ids:
            cases = []
            for i in ids:
                m, mod, err = reg.resolve(i, a.modality)
                if not m:
                    print("пропуск %s: %s" % (i, err)); continue
                cases.append(m)
        else:
            import collections
            by_prov = collections.defaultdict(list)
            for m in reg.models:
                if not m.selectable: continue
                if a.modality not in m.ranks: continue
                by_prov[m.provider or m.organization or "?"].append(m)
            cases = []
            for prov, lst in by_prov.items():
                lst.sort(key=lambda x: x.best_rank)
                cases.append(lst[0])
            cases.sort(key=lambda x: x.best_rank)
            cases = cases[:a.limit]
        print("проверяю %d моделей (интервал %s с)" % (len(cases), C.MIN_INTERVAL))
        verified = {}
        if os.path.exists(C.VERIFIED):
            try: verified = json.load(open(C.VERIFIED)).get("models", {})
            except Exception: verified = {}
        ok = 0
        for i, m in enumerate(cases, 1):
            try:
                res = await eng.evaluate(model_id=m.id, prompt="Ответь одним словом: ОК",
                                         modality=a.modality)
                rec = {"ok": True, "status": 200, "text": (res.get("text") or "")[:60],
                       "ms": res.get("ms"), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                await eng.cleanup(res.get("eval_id"))
                ok += 1
            except ArenaError as e:
                rec = {"ok": False, "status": e.status, "code": e.code,
                       "error": e.message[:160], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            rec.update({"name": m.public_name, "provider": m.provider, "modality": a.modality})
            verified[m.id] = rec
            reg.mark_verified(m.id, rec["ok"])
            print("%2d/%2d %-8s %-36s %-24s %s" % (
                i, len(cases), rec.get("status"), (m.public_name or "")[:36],
                (m.provider or "")[:24],
                rec.get("text") or rec.get("error", "")))
            json.dump({"updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "modality": a.modality, "models": verified},
                      open(C.VERIFIED, "w"), ensure_ascii=False, indent=1)
        print("\nработают %d из %d → %s" % (ok, len(cases), C.VERIFIED))
        await eng.stop()
        return

    if a.cmd == "v2":
        await eng.start()
        t0 = time.time()
        tok = await eng.recaptcha_v2()
        print("v2-токен: %s (%.1f с)" % ((tok[:40] + "…") if tok else "НЕТ", time.time() - t0))
        await eng.stop()
        return

    if a.cmd == "cleanup":
        await eng.start()
        print(await eng.cleanup(a.eval_id))
        await eng.stop()


asyncio.run(main())
