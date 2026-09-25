import json, websockets.sync.client, urllib.request, time

# открываем папку ukraine
url = "https://chatgpt.com/g/g-p-6a94053b811c8191a46daaaa059f9dd5-ukraine"
r = urllib.request.urlopen(urllib.request.Request(
    f"http://127.0.0.1:9222/json/new?{url}", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка создана: {tab.get('url')}")

try:
    with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
        max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
        # включаем Network и Page
        ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))
        ws.send(json.dumps({"id": 2, "method": "Page.enable", "params": {}}))
        
        events = []
        start = time.time()
        while time.time() - start < 20:  # 20 секунд сбора
            try:
                msg = json.loads(ws.recv())
                if msg.get("method") in ("Network.requestWillBeSent", 
                                          "Network.responseReceived",
                                          "Network.loadingFailed",
                                          "Page.frameNavigated",
                                          "Page.frameStoppedLoading",
                                          "Inspector.targetCrashed"):
                    events.append(msg)
                    print(f"[{msg.get('method')}] {json.dumps(msg.get('params', {}), ensure_ascii=False)[:200]}")
                if len(events) > 30:
                    break
            except Exception as e:
                print(f"ERR: {type(e).__name__}: {e}")
                break
        
        print(f"\nсобрано событий: {len(events)}")

except Exception as e:
    print(f"WebSocket ERR: {type(e).__name__}: {e}")
