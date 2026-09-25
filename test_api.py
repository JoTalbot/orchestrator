from curl_cffi import requests as cf
import json

token = open("/opt/orchestrator/.secrets/chatgpt_token.txt").read().strip()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
s = cf.Session(impersonate="chrome")
s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": UA})

# пробуем разные эндпоинты для списка папок
endpoints = [
    "https://chatgpt.com/backend-api/gizmos",
    "https://chatgpt.com/backend-api/projects",
    "https://chatgpt.com/backend-api/conversations?is_archived=false&order=updated&cursor=0&limit=50",
]

for url in endpoints:
    endpoint = url.split("backend-api/")[1] if "backend-api/" in url else url
    print(f"\n=== {endpoint} ===")
    try:
        r = s.get(url, timeout=30)
        print(f"status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                print(f"items: {len(data)}")
                if data:
                    print(f"первый: {json.dumps(data[0], ensure_ascii=False)[:200]}")
            elif isinstance(data, dict):
                print(f"ключи: {list(data.keys())[:10]}")
                if "items" in data:
                    print(f"items: {len(data['items'])}")
        else:
            print(f"body: {r.text[:200]}")
    except Exception as e:
        print(f"ERR: {type(e).__name__}: {e}")
