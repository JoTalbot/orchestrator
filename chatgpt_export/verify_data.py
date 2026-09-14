#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка целостности собранных данных ChatGPT."""
import json
from datetime import datetime
from pathlib import Path

root = Path("data")
chats = list((root / "chats").glob("*.json"))
light = list((root / "light").glob("*.json"))
index = json.loads((root / "index.json").read_text())
errs_path = root / "errors.json"
errs = json.loads(errs_path.read_text()) if errs_path.exists() else []

total_bytes = sum(p.stat().st_size for p in chats)
total_msgs = sum(v.get("messages", 0) for v in index.values())
empty = [p.name for p in chats if p.stat().st_size == 0]
broken = []
for p in chats:
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        broken.append((p.name, str(e)[:60]))

def dt(ts):
    try:
        return datetime.fromtimestamp(float(ts))
    except Exception:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", ""))
        except Exception:
            return None

times = [dt(v.get("create_time")) for v in index.values() if v.get("create_time")]
times = [t for t in times if t]
top = sorted(index.values(), key=lambda v: -(v.get("messages") or 0))[:10]

print("=== ИТОГ ПРОВЕРКИ ===")
print("Чатов в индексе:          %d" % len(index))
print("Файлов chats/*.json:      %d" % len(chats))
print("Файлов light/*.json:      %d" % len(light))
print("Всего сообщений:          %d" % total_msgs)
print("Объём сырых данных:       %.1f МБ" % (total_bytes / 1024 / 1024))
print("Пустых файлов:            %d" % len(empty))
print("Битых JSON:               %d" % len(broken))
print("Недоступных чатов (404):  %d" % len(errs))
for e in errs:
    print("  - %s" % str(e.get("title", ""))[:70])
if times:
    print("Диапазон дат чатов:       %s .. %s"
          % (min(times).strftime("%Y-%m-%d"), max(times).strftime("%Y-%m-%d")))
print("=== ТОП-10 крупнейших чатов ===")
for v in top:
    print("  %5d сообщ. | %s" % (v.get("messages") or 0, (v.get("title") or "")[:70]))
