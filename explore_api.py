"""Изучение API для отправки сообщений в ChatGPT"""
from curl_cffi import requests as cf
import json

token = open("/opt/orchestrator/.secrets/chatgpt_token.txt").read().strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
s = cf.Session(impersonate="chrome")
s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": UA})

# 1. Смотрим структуру существующего чата
chat_id = "6aa7ba93-40b8-83ed-b1b0-62a7f85d43f3"
print(f"=== GET /backend-api/conversation/{chat_id} ===")
r = s.get(f"https://chatgpt.com/backend-api/conversation/{chat_id}", timeout=30)
print(f"status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"ключи: {list(data.keys())}")
    print(f"current_node: {data.get('current_node')}")
    print(f"mapping keys: {len(data.get('mapping', {}))}")
    # смотрим структуру сообщения
    for node_id, node in list(data.get("mapping", {}).items())[:2]:
        print(f"\nnode {node_id}:")
        msg = node.get("message", {})
        print(f"  author: {msg.get('author')}")
        print(f"  content.keys: {list(msg.get('content', {}).keys())}")
        print(f"  id: {msg.get('id')}")

# 2. Пробуем найти эндпоинт для отправки сообщений
print("\n=== пробуем OPTIONS на /backend-api/conversation ===")
r = s.options("https://chatgpt.com/backend-api/conversation", timeout=10)
print(f"status: {r.status_code}")
print(f"headers: {dict(r.headers)}")

# 3. Смотрим что есть в /backend-api
print("\n=== пробуем GET /backend-api ===")
r = s.get("https://chatgpt.com/backend-api", timeout=10)
print(f"status: {r.status_code}")
print(f"body: {r.text[:300]}")
