#!/usr/bin/env python3
"""Разведка формата вложений direct-режима: что фронтенд шлёт с картинками.

Ключи поиска в JS-чанках страницы /text/direct: experimental_attachments,
generateUploadUrl, uploadUrl, attachments, mediaType, uploads.
Печатает контекст (±250 символов) — по нему видно структуру объекта вложения.

Только чтение: чанки статики, никаких промптов и запросов к API.
"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/orchestrator")
from arena_agent import arena_api  # noqa: E402

KEYS = ["experimental_attachments", "generateUploadUrl", "uploadUrl",
        "uploads", "attachments", "mediaType", "generate-agent-upload-url"]

JS = r"""
(async () => {
  const keys = %s;
  const srcs = new Set();
  const addSrcs = (html) => {
    const re = /(?:src|href)="([^"]+\.js[^"]*)"/g;
    let m; while ((m = re.exec(html))) srcs.add(m[1]);
  };
  for (const p of ["/text/direct", "/", "/leaderboard"]) {
    try { const r = await fetch(p, {credentials: "include"});
          addSrcs(await r.text()); } catch (e) {}
  }
  addSrcs(document.documentElement.outerHTML);
  const out = {}; const files = [];
  const arr = [...srcs].slice(0, 120);
  for (const s of arr) {
    let u; try { u = new URL(s, location.origin).href; } catch (e) { continue; }
    if (!u.includes(location.host)) continue;
    let t; try {
      const r = await fetch(u, {credentials: "include"});
      if (!r.ok) continue;
      t = await r.text();
    } catch (e) { continue; }
    files.push({url: u, bytes: t.length});
    for (const k of keys) {
      let i = -1, n = 0;
      while ((i = t.indexOf(k, i + 1)) >= 0 && n < 2) {
        out[k] = out[k] || [];
        out[k].push({file: u.split("/").pop(), ctx:
                     t.slice(Math.max(0, i - 250), i + 250).replace(/\s+/g, " ")});
        n++;
      }
    }
  }
  return {files: files.length, out: out};
})()
""" % json.dumps(KEYS)


async def main():
    tab = await arena_api.connect(verbose=True)
    try:
        res = await tab.js(JS, timeout=280)
    finally:
        await tab.close()
    print("чанков прочитано:", res["files"])
    with open("/tmp/attach_scan.json", "w", encoding="utf-8") as f:
        json.dump(res["out"], f, ensure_ascii=False, indent=1)
    for k in KEYS:
        hits = res["out"].get(k) or []
        print("\n=== %s (%d) ===" % (k, len(hits)))
        for h in hits[:3]:
            print("  [%s] …%s…" % (h["file"], h["ctx"][:420]))
    print("\nполные данные: /tmp/attach_scan.json")


if __name__ == "__main__":
    asyncio.run(main())
