import json, websockets.sync.client, urllib.request, time

# создаём пустую вкладку
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:9222/json/new?about:blank", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка создана: {tab.get('id')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    # navigate на главную chatgpt
    print("→ navigate на chatgpt.com/")
    ws.send(json.dumps({"id": 1, "method": "Page.navigate",
        "params": {"url": "https://chatgpt.com/"}}))
    time.sleep(15)
    
    # проверяем
    expr = "JSON.stringify({url: location.href, title: document.title, bodyLen: document.body ? document.body.innerText.length : 0})"
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(f"после chatgpt.com/: {val}")
    
    # теперь navigate на папку
    print("\n→ navigate на папку ukraine")
    ws.send(json.dumps({"id": 3, "method": "Page.navigate",
        "params": {"url": "https://chatgpt.com/g/g-p-6a94053b811c8191a46daaaa059f9dd5-ukraine"}}))
    time.sleep(20)
    
    # проверяем
    expr2 = "JSON.stringify({url: location.href, title: document.title, bodyLen: document.body ? document.body.innerText.length : 0, chatCards: document.querySelectorAll('[data-testid=\"chat-item\"]').length, bodyText: document.body ? document.body.innerText.slice(0, 800) : null})"
    ws.send(json.dumps({"id": 4, "method": "Runtime.evaluate",
        "params": {"expression": expr2, "returnByValue": True}}))
    resp2 = json.loads(ws.recv())
    val2 = resp2.get("result", {}).get("result", {}).get("value", "?")
    print(f"после папки: {val2}")
