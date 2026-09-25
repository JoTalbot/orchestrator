import json, websockets.sync.client, urllib.request, time

# создаём вкладку на главную
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:9222/json/new?https://chatgpt.com/", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка: {tab.get('id')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    # включаем Network для захвата запросов
    ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
    
    api_requests = []
    start = time.time()
    while time.time() - start < 25:  # 25 секунд сбора
        try:
            msg = json.loads(ws.recv())
            if msg.get("method") == "Network.requestWillBeSent":
                url = msg.get("params", {}).get("request", {}).get("url", "")
                if "backend-api" in url or "api" in url:
                    api_requests.append(url)
                    print(f"[API] {url[:120]}")
            elif msg.get("method") == "Network.responseReceived":
                url = msg.get("params", {}).get("response", {}).get("url", "")
                status = msg.get("params", {}).get("response", {}).get("status", 0)
                if ("backend-api" in url or "api" in url) and status != 200:
                    print(f"[ERR {status}] {url[:120]}")
        except Exception as e:
            print(f"ERR: {type(e).__name__}: {e}")
            break
    
    print(f"\nсобрано API-запросов: {len(api_requests)}")
    
    # проверяем localStorage
    print("\n→ localStorage:")
    expr = "JSON.stringify(Object.keys(localStorage).slice(0, 20))"
    ws.send(json.dumps({"id": 99, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(val)
