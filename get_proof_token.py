"""Получение proof_token из работающей вкладки ChatGPT"""
import json, websockets.sync.client, urllib.request, time

tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
cg = [t for t in tabs if "chatgpt.com" in t.get("url", "")]
if not cg:
    print("нет вкладок chatgpt"); exit(1)
tab = cg[0]
print(f"вкладка: {tab.get('url')}")

with websockets.sync.client.connect(tab["webSocketDebuggerUrl"],
    max_size=5*1024*1024, close_timeout=10, open_timeout=30) as ws:
    time.sleep(3)
    
    # ищем proof_token в глобальных переменных
    print("\n→ ищем proof_token:")
    expr = """JSON.stringify({
        turnstile: typeof window.turnstile,
        cfTurnstileResponse: window.cfTurnstileResponse || null,
        proofToken: window.__proofToken || null,
        sentry: typeof window.Sentry,
        oai: window.__oai ? Object.keys(window.__oai) : null
    })"""
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True}}))
    resp = json.loads(ws.recv())
    val = resp.get("result", {}).get("result", {}).get("value", "?")
    print(val)
    
    # пробуем найти в localStorage
    print("\n→ localStorage (ключи с token/proof):")
    expr2 = "JSON.stringify(Object.keys(localStorage).filter(k => k.includes('token') || k.includes('proof') || k.includes('turnstile')))"
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
        "params": {"expression": expr2, "returnByValue": True}}))
    resp2 = json.loads(ws.recv())
    val2 = resp2.get("result", {}).get("result", {}).get("value", "?")
    print(val2)
