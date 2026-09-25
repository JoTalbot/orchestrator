import json, websockets.sync.client, urllib.request, time

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
ukr = [t for t in tabs if "ukraine" in t.get("url", "")]
if not ukr:
    print("нет вкладки ukraine"); exit(1)
tab = ukr[0]
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(20)  # ещё 20 сек на загрузку
    
    expr = "JSON.stringify({url: location.href, title: document.title, chatCards: document.querySelectorAll('[data-testid=\"chat-item\"]').length, allLinks: Array.from(document.querySelectorAll('a[href*=\"/c/\"]')).slice(0, 5).map(a => a.href).join(' | '), bodyText: document.body ? document.body.innerText.slice(0, 1200) : null})"
    
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(val)
