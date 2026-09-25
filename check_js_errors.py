import json, websockets.sync.client, urllib.request, time

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
cg = [t for t in tabs if "chatgpt.com" in t.get("url", "")]
if not cg:
    print("нет вкладок chatgpt"); exit(1)
tab = cg[0]
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    # включаем Runtime для捕获 исключений
    ws.send(json.dumps({"id": 1, "method": "Runtime.enable", "params": {}}))
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
        "params": {"expression": "1+1"}}))  # dummy для активации
    
    time.sleep(3)
    
    # вызываем ошибку намеренно чтобы проверить что консоль работает
    ws.send(json.dumps({"id": 3, "method": "Runtime.evaluate",
        "params": {"expression": "console.error('TEST ERROR'); console.log('TEST LOG'); throw new Error('TEST THROW')"}}))
    
    time.sleep(2)
    
    # проверяем что есть в DOM
    expr = """JSON.stringify({
        scripts: document.querySelectorAll('script').length,
        stylesheets: document.querySelectorAll('link[rel=stylesheet]').length,
        divs: document.querySelectorAll('div').length,
        reactRoot: !!document.getElementById('__next'),
        bodyChilds: document.body ? document.body.children.length : 0
    })"""
    ws.send(json.dumps({"id": 4, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(f"DOM: {val}")
    
    # пробуем вызвать React render вручную
    print("\n→ пробуем window.location.reload()")
    ws.send(json.dumps({"id": 5, "method": "Runtime.evaluate",
        "params": {"expression": "window.location.reload()"}}))
    time.sleep(10)
    
    # проверяем снова
    expr2 = "JSON.stringify({bodyLen: document.body ? document.body.innerText.length : 0, divs: document.querySelectorAll('div').length})"
    ws.send(json.dumps({"id": 6, "method": "Runtime.evaluate",
        "params": {"expression": expr2, "returnByValue": True}}))
    resp2 = json.loads(ws.recv())
    val2 = resp2.get("result", {}).get("result", {}).get("value", "?")
    print(f"после reload: {val2}")
