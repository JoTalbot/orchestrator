#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка: умеет ли страница выдать reCAPTCHA v3-токен для действия chat_submit."""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/orchestrator/arena_agent")
from arena_fetch import Tab, arena_tab  # noqa: E402

SITEKEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"


async def main():
    t = arena_tab()
    tab = Tab(t["webSocketDebuggerUrl"])
    await tab.connect()
    await tab.cmd("Runtime.enable")
    expr = """
(async () => {
  const out = {};
  out.hasGrecaptcha = typeof grecaptcha !== 'undefined';
  out.hasEnterprise = out.hasGrecaptcha && !!grecaptcha.enterprise;
  out.ready = out.hasGrecaptcha ? typeof grecaptcha.ready : null;
  if (out.hasEnterprise) {
    try {
      const tok = await grecaptcha.enterprise.execute(%s, {action: 'chat_submit'});
      out.tokenLen = tok.length;
      out.tokenHead = tok.slice(0, 24);
    } catch (e) { out.err = String(e); }
  }
  return JSON.stringify(out);
})()
""" % json.dumps(SITEKEY)
    print(await tab.js(expr, timeout=60))
    await tab.close()


asyncio.run(main())
