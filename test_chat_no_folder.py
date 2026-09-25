import json, websockets.sync.client, urllib.request, time

# создаём вкладку на чат без папки
chat_url = "https://chatgpt.com/c/6aa7ba93-40b8-83ed-b1b0-62a7f85d43f3"
r = urllib.request.urlopen(urllib.request.Request(
    f"http://127.0.0.1:9222/json/new?{chat_url}", method="PUT"))
tab = json.loads(r.read())
print(f"вкладка создана: {tab.get('url')}")

try:
    with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
        max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
        time.sleep(20)
        
        # проверяем что загрузилось
        expr = """JSON.stringify({
            url: location.href,
            title: document.title,
            bodyLen: document.body ? document.body.innerText.length : 0,
            textarea: !!document.querySelector('textarea'),
            sendButton: !!document.querySelector('[data-testid="send-button"]'),
            messages: document.querySelectorAll('[data-message-author-role]').length,
            bodyText: document.body ? document.body.innerText.slice(0, 1000) : null
        })"""
        
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True}}))
        resp = json.loads(ws.recv())
        val = resp.get("result", {}).get("result", {}).get("value", "?")
        print(val)
except Exception as e:
    print(f"ERR: {type(e).__name__}: {e}")
