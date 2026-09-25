"""Тест API с cookies из браузера"""
from curl_cffi import requests as cf
import json
import websockets.sync.client
import urllib.request

# получаем cookies из браузера
tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
cg = [t for t in tabs if "chatgpt.com" in t.get("url", "")]
tab = cg[0]

cookies_dict = {}
with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    ws.send(json.dumps({"id": 1, "method": "Network.getCookies", "params": {}}))
    resp = json.loads(ws.recv())
    cookies = resp.get("result", {}).get("cookies", [])
    for c in cookies:
        cookies_dict[c["name"]] = c["value"]

print(f"получено cookies: {len(cookies_dict)}")

# пробуем API с cookies
s = cf.Session(impersonate="chrome")
s.cookies.update(cookies_dict)
s.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
})

# пробуем GET conversations
print("\n=== GET /backend-api/conversations (с cookies) ===")
r = s.get("https://chatgpt.com/backend-api/conversations?offset=0&limit=5", timeout=30)
print(f"status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"items: {len(data.get('items', []))}")
else:
    print(f"body: {r.text[:200]}")

# пробуем POST (без proof_token, но с cookies)
print("\n=== POST /backend-api/conversation (с cookies) ===")
import uuid
chat_id = "6aa7ba93-40b8-83ed-b1b0-62a7f85d43f3"
payload = {
    "action": "next",
    "messages": [{
        "id": str(uuid.uuid4()),
        "author": {"role": "user"},
        "content": {"content_type": "text", "parts": ["."]}
    }],
    "parent_message_id": "client-created-root",
    "conversation_id": chat_id,
    "model": "gpt-4o",
}
r = s.post("https://chatgpt.com/backend-api/conversation",
           data=json.dumps(payload), timeout=30)
print(f"status: {r.status_code}")
print(f"body: {r.text[:300]}")
