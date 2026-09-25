"""Тест отправки сообщения через API"""
from curl_cffi import requests as cf
import json
import uuid

token = open("/opt/orchestrator/.secrets/chatgpt_token.txt").read().strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
s = cf.Session(impersonate="chrome")
s.headers.update({
    "Authorization": f"Bearer {token}",
    "User-Agent": UA,
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
})

chat_id = "6aa7ba93-40b8-83ed-b1b0-62a7f85d43f3"

# получаем current_node
r = s.get(f"https://chatgpt.com/backend-api/conversation/{chat_id}", timeout=30)
data = r.json()
parent_id = data.get("current_node")
print(f"parent_id (current_node): {parent_id}")

# пробуем отправить сообщение
message_id = str(uuid.uuid4())
payload = {
    "action": "next",
    "messages": [{
        "id": message_id,
        "author": {"role": "user"},
        "content": {"content_type": "text", "parts": ["."]}
    }],
    "parent_message_id": parent_id,
    "conversation_id": chat_id,
    "model": "gpt-4o",
    "timezone_offset_min": 180,
    "suggestions": [],
    "history_and_training_disabled": False,
    "conversation_mode": {"kind": "primary_assistant"},
    "force_paragen": False,
    "force_nulligen": False,
    "force_paragen_model_slug": "",
    "force_paragen_safety_checker": False,
}

print(f"\n=== POST /backend-api/conversation ===")
print(f"payload keys: {list(payload.keys())}")
r = s.post("https://chatgpt.com/backend-api/conversation",
           data=json.dumps(payload), timeout=30)
print(f"status: {r.status_code}")
print(f"headers: {dict(r.headers)}")
print(f"body (first 500): {r.text[:500]}")
