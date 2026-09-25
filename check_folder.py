import json, websockets.sync.client, urllib.request, time

# открываем папку ukraine
url = "https://chatgpt.com/g/g-p-6a94053b811c8191a46daaaa059f9dd5-ukraine"
r = urllib.request.urlopen(urllib.request.Request(
    f"http://127.0.0.1:9222/json/new?{url}", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(15)  # даём SPA загрузиться
    
    # что на странице?
    expr = "JSON.stringify({url: location.href, title: document.title, chatCards: document.querySelectorAll('[data-testid=\"chat-item\"]').length, allLinks: Array.from(document.querySelectorAll('a[href*=\"/c/\"]')).slice(0, 5).map(a => a.href).join(' | '), bodyText: document.body ? document.body.innerText.slice(0, 1000) : null})"
    
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(val)
