import json, websockets.sync.client, urllib.request, time

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
# берём любую вкладку chatgpt
cg = [t for t in tabs if "chatgpt.com" in t.get("url", "")]
if not cg:
    print("нет вкладок chatgpt"); exit(1)
tab = cg[0]
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(5)
    
    # проверяем куки
    print("→ куки:")
    ws.send(json.dumps({"id": 1, "method": "Network.getCookies", "params": {}}))
    resp = json.loads(ws.recv())
    cookies = resp.get("result", {}).get("cookies", [])
    for c in cookies[:15]:
        print(f"  {c.get('name')}: {c.get('value')[:50]}...")
    
    print(f"\nвсего куки: {len(cookies)}")
    
    # проверяем что в сайдбаре
    print("\n→ сайдбар (первые 800 символов):")
    expr = "document.querySelector('[class*=\"sidebar\"]') ? document.querySelector('[class*=\"sidebar\"]').innerText.slice(0, 800) : 'sidebar not found'"
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp2 = json.loads(ws.recv())
    val = resp2.get("result", {}).get("result", {}).get("value", "?")
    print(val)
