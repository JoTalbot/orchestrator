import json, websockets.sync.client, urllib.request, time

r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:9222/json/new?https://chatgpt.com/", method="PUT"))
tab = json.loads(r.read())
print("вкладка:", tab.get("url"))

expr = "JSON.stringify({url: location.href, title: document.title, body: document.body ? document.body.innerText.slice(0, 800) : null})"

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(10)
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(val)
