#!/usr/bin/env python3
"""Где сейчас живёт каталог моделей arena.ai (RSC-потоки Next.js).

Факт 03.10.2026: `/leaderboard/agent` в HTML отдаёт `"initialModels":"$undefined"`,
`dump_models.py` из-за этого находит 0 моделей. Фронтенд не зовёт JSON-API моделей
(`scan_net_surface.py`: только /api/me/pulse, RSC-фетчи и аналитика), значит список
приезжает либо в RSC-потоке какой-то страницы, либо серверным действием.

Скрипт тянет RSC-потоки нескольких маршрутов прямо из вкладки (заголовок RSC: 1,
куки браузера на месте), сохраняет их в /tmp/rsc_*.txt и печатает:
  * размер и статус каждого потока,
  * контекст вокруг initialModels / models,
  * есть ли в потоке UUID моделей.

Только чтение, промптов не отправляет.
"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "/opt/orchestrator")
from arena_agent import arena_api  # noqa: E402

ROUTES = ["/text/direct", "/text/direct?model_a=max", "/leaderboard",
          "/leaderboard/agent", "/agent", "/"]

JS = r"""
(async () => {
  const routes = %s;
  const out = [];
  for (let i = 0; i < routes.length; i++) {
    const r = {route: routes[i]};
    try {
      const resp = await fetch(routes[i], {credentials: "include",
                                           headers: {"RSC": "1"}});
      r.status = resp.status;
      const t = await resp.text();
      r.bytes = t.length;
      r.initialModels = t.indexOf("initialModels") >= 0;
      const idx = t.indexOf("initialModels");
      if (idx >= 0) r.ctx = t.slice(Math.max(0, idx - 80), idx + 400).replace(/\s+/g, " ");
      const uu = t.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g) || [];
      r.uuids = uu.length;
      r.uuidSample = [...new Set(uu)].slice(0, 8);
      for (const k of ["claude", "gpt", "gemini", "grok", "lhotse", "sora", "veo"]) {
        r[k] = (t.match(new RegExp(k, "gi")) || []).length;
      }
      r.text = t;
    } catch (e) { r.error = String(e); }
    out.push(r);
  }
  return out;
})()
""" % json.dumps(ROUTES)


async def main():
    tab = await arena_api.connect(verbose=True)
    try:
        out = await tab.js(JS, timeout=240)
    finally:
        await tab.close()
    for i, r in enumerate(out):
        name = "/tmp/rsc_%d.txt" % i
        if r.get("text"):
            with open(name, "w", encoding="utf-8") as f:
                f.write(r["text"])
        r.pop("text", None)
        print("--- %s -> %s" % (r["route"], name))
        print("    статус %s, байт %s, initialModels=%s, UUID-моделей %s"
              % (r.get("status"), r.get("bytes"), r.get("initialModels"),
                 r.get("uuids")))
        print("    упоминания: claude=%s gpt=%s gemini=%s grok=%s lhotse=%s sora=%s veo=%s"
              % tuple(r.get(k) for k in ("claude", "gpt", "gemini", "grok",
                                         "lhotse", "sora", "veo")))
        if r.get("uuidSample"):
            print("    uuid-примеры:", ", ".join(r["uuidSample"]))
        if r.get("ctx"):
            print("    ctx:", r["ctx"][:300])
    with open("/tmp/rsc_scan.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
