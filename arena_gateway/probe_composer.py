#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба: где настоящий композер (все textarea/контент-редактаблы + кнопки)."""
import asyncio, json, sys
sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, "/opt/orchestrator/arena_gateway")
import config as C
from engine import ArenaEngine

EXPR = """(() => {
  const pick = el => {
    const r = el.getBoundingClientRect();
    return {tag: el.tagName, ph: el.getAttribute('placeholder') || '',
            ce: el.getAttribute('contenteditable'),
            cls: (el.className || '').toString().slice(0, 60),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height)};
  };
  const tas = [...document.querySelectorAll('textarea')].map(pick);
  const ces = [...document.querySelectorAll('[contenteditable="true"],[contenteditable=""]')].map(pick);
  const btns = [...document.querySelectorAll('button')].map(b => {
    const r = b.getBoundingClientRect();
    const lab = b.getAttribute('aria-label') || b.title || '';
    return {lab, x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            dis: b.disabled};
  }).filter(b => b.w > 4 && /send|submit|отправ|→|arrow/i.test(b.lab) || (b.w>4&&b.w<70&&b.h<70&&b.x>1400&&b.y>500&&b.y<700));
  return JSON.stringify({vw: window.innerWidth, vh: window.innerHeight,
                         tas, ces, btns: btns.slice(0, 8)}, null, 1);
})()"""

async def main():
    eng = ArenaEngine(C)
    await eng.start()
    print(await eng.js(EXPR, timeout=30))

asyncio.run(main())
