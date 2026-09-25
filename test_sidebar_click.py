import json, websockets.sync.client, urllib.request, time

# создаём вкладку на главную
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:9222/json/new?https://chatgpt.com/", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(20)  # ждём полную загрузку
    
    # проверяем что загрузилось
    expr = "JSON.stringify({url: location.href, title: document.title, bodyLen: document.body ? document.body.innerText.length : 0, sidebar: !!document.querySelector('nav'), links: document.querySelectorAll('a').length})"
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(f"после загрузки: {val}")
    
    # ищем папки в сайдбаре
    print("\n→ поиск папок проектов:")
    expr2 = """Array.from(document.querySelectorAll('a[href*=\"/g/\"]')).slice(0, 10).map(a => ({href: a.href, text: a.innerText.slice(0, 50)}))"""
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
        "params": {"expression": expr2, "returnByValue": True}}))
    resp2 = json.loads(ws.recv())
    val2 = resp2.get("result", {}).get("result", {}).get("value", "?")
    print(val2)
