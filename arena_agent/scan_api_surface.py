#!/usr/bin/env python3
"""Разведка API-поверхности arena.ai прямо из вкладки браузера.

Зачем: каталог моделей обновлялся через `initialModels` в RSC-потоке
`/leaderboard/agent`, но 03.10.2026 этот ключ превратился в `$undefined`,
и `dump_models.py` перестал находить список. Вместо угадывания эндпоинтов
скрипт читает САМИ JS-чанки arena.ai (same-origin fetch из вкладки, куки на
месте), вытаскивает оттуда все строки вида `/api/...` и контекст вокруг
`initialModels` — то есть реальный список эндпоинтов, которые фронтенд зовёт.

Только чтение: никаких POST-ов, никаких промптов, бюджет шлюза не тратится.

Запуск (на сервере, где живёт CDP :9222):
    .venv/bin/python arena_agent/scan_api_surface.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/orchestrator")
from arena_agent import arena_api  # noqa: E402

SCAN_JS = r"""
(async () => {
  const srcs = new Set();
  const addSrcs = (html) => {
    const re = /(?:src|href)="([^"]+\.js[^"]*)"/g;
    let m; while ((m = re.exec(html))) srcs.add(m[1]);
  };
  const pages = ["/", "/leaderboard", "/leaderboard/agent", "/agent", "/models"];
  const texts = {};
  for (const p of pages) {
    try {
      const r = await fetch(p, {credentials: "include"});
      const t = await r.text();
      texts[p] = t; addSrcs(t);
    } catch (e) { texts[p] = ""; }
  }
  addSrcs(document.documentElement.outerHTML);
  const arr = [...srcs].slice(0, 90);
  const apis = new Set();
  let okChunks = 0;
  for (const s of arr) {
    try {
      const u = new URL(s, location.origin).href;
      if (!u.includes(location.host)) continue;
      const r = await fetch(u, {credentials: "include"});
      if (!r.ok) continue;
      const t = await r.text(); okChunks++;
      let m;
      const re = /["'`](\/api\/[A-Za-z0-9\/_.\-]{2,60})["'`]/g;
      while ((m = re.exec(t))) apis.add(m[1]);
      const re2 = /["'`](\/v1\/[A-Za-z0-9\/_.\-]{2,60})["'`]/g;
      while ((m = re2.exec(t))) apis.add(m[1]);
      const re3 = /(trpc\.[A-Za-z0-9_.]{2,50})/g;
      while ((m = re3.exec(t))) apis.add(m[1]);
    } catch (e) {}
  }
  const ctx = [];
  const all = Object.values(texts).join("\n") + document.documentElement.outerHTML;
  for (const key of ["initialModels", "availableModels", "modelsCatalog",
                     "agent-models", "modelIds", "leaderboard"]) {
    let i = -1, n = 0;
    while ((i = all.indexOf(key, i + 1)) >= 0 && n < 2) {
      ctx.push(key + " :: " + all.slice(Math.max(0, i - 120), i + 200)
                 .replace(/\s+/g, " "));
      n++;
    }
  }
  return {scriptSrcs: arr.length, okChunks: okChunks,
          apis: [...apis].sort(), ctx: ctx};
})()
"""


async def main():
    tab = await arena_api.connect(verbose=True)
    try:
        href = await tab.js("location.href")
        print("вкладка:", href)
        out = await tab.js(SCAN_JS, timeout=240)
    finally:
        await tab.close()
    print("скриптов к обработке: %d, реально выкачано чанков: %d"
          % (out["scriptSrcs"], out["okChunks"]))
    print("\n=== эндпоинты, найденные в JS фронтенда ===")
    for a in out["apis"]:
        print(" ", a)
    print("\n=== контекст вокруг ключей каталога ===")
    for c in out["ctx"]:
        print(" -", c[:360])
    with open("/tmp/api_surface.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nполные данные: /tmp/api_surface.json")


if __name__ == "__main__":
    asyncio.run(main())
