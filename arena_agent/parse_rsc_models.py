#!/usr/bin/env python3
"""Разбор RSC-потока /text/direct: извлечь initialModels и сравнить с каталогом.

Вход: /tmp/rsc_0.txt (то, что сохранил scan_rsc_models.py).
Выход: /tmp/models_new.json (только список моделей: id, name, org, capabilities)
       + отчёт по отличиям от data/arena/models_catalog.json.
"""
import json
import re
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rsc_0.txt"
raw = open(src, encoding="utf-8").read()

i = raw.find('"initialModels":[')
if i < 0:
    print("initialModels не найден")
    sys.exit(1)
start = raw.index("[", i)
# RSC-поток — это JS-строка внутри push(...), скобки сбалансированы
depth, j, instr, esc = 0, start, False, False
while j < len(raw):
    c = raw[j]
    if instr:
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            instr = False
    else:
        if c == '"':
            instr = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
    j += 1
chunk = raw[start:j + 1]
try:
    models = json.loads(chunk)
except json.JSONDecodeError as e:
    # в RSC внутри строк бывают \" и \\ — пробуем «распаковать» ещё раз
    models = json.loads(json.loads('"%s"' % chunk.replace('"', '\\"')))
    del e
print("моделей в потоке:", len(models))
print("поля первой:", sorted(models[0].keys())[:14])

compact = []
for m in models:
    caps = m.get("capabilities") or {}
    inp = (caps.get("inputCapabilities") or {})
    out = (caps.get("outputCapabilities") or {})
    compact.append({
        "id": m.get("id"),
        "name": m.get("name"),
        "publicName": m.get("publicName"),
        "displayName": m.get("displayName"),
        "organization": m.get("organization"),
        "provider": m.get("provider"),
        "input": sorted(k for k, v in inp.items() if v),
        "output": sorted(k for k, v in out.items() if v),
    })
with open("/tmp/models_new.json", "w", encoding="utf-8") as f:
    json.dump(compact, f, ensure_ascii=False, indent=1)

old_path = "/opt/orchestrator/data/arena/models_catalog.json"
try:
    old = json.load(open(old_path, encoding="utf-8"))
except Exception as e:
    old = []
    print("старый каталог не прочитан:", e)
old_ids = set()
for m in (old if isinstance(old, list) else old.get("models", [])):
    mid = m.get("id") or m.get("model_id") or m.get("uuid")
    if mid:
        old_ids.add(str(mid))
new_ids = {str(m["id"]) for m in compact}
print("в старом каталоге id: %d, в новом: %d" % (len(old_ids), len(new_ids)))
print("новых id:", len(new_ids - old_ids), "| исчезли:", len(old_ids - new_ids))

# что важно шлюзу: claude / gpt / gemini / grok и «битые» из пробника
bad = {"019fd40c-e6c8-7d66-b5ab-72e6fa021030": "lhotse (400 в пробнике)",
       "019c6f55-70d6-7a9c-b89b-9a0db36a3582": "claude-sonnet-4-6-search (400)",
       "019a2d13-28a5-7205-908c-0a58de904617": "claude-sonnet-4-5-20250929 (наш рабочий)"}
print("\n=== судьба спорных id ===")
for mid, label in bad.items():
    hit = next((m for m in compact if str(m["id"]) == mid), None)
    print("  %-42s %s" % (label, ("ЕСТЬ: " + str(hit["displayName"])) if hit else "НЕТ в каталоге"))

print("\n=== актуальные chat-модели claude / gpt-5 / gemini / grok ===")
for m in compact:
    nm = " ".join(str(m.get(k) or "") for k in ("displayName", "publicName", "name")).lower()
    if any(k in nm for k in ("claude", "gpt-5", "gemini", "grok")) and \
            any(k in m["output"] for k in ("text", "search", "thinking")):
        print("  %-34s %s | %s | out=%s" % (str(m["displayName"])[:34], m["id"],
                                            m["organization"], ",".join(m["output"])[:40]))
