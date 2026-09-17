"""
OpenAI-совместимый шлюз к arena.ai (direct-режим).

  GET  /v1/models               список моделей
  POST /v1/chat/completions     чат (stream=true → SSE)
  POST /v1/completions          легаси-промпт
  POST /v1/images/generations   генерация картинок (modality=image)
  GET  /v1/arena/status         диагностика шлюза
  POST /v1/arena/probe          проверка доступности моделей (фон)
  GET  /health                  здоровье

Каждый запрос = новый приватный чат арены в режиме direct-battle, который
после получения ответа закрывается (DELETE /api/chat/{id}).
"""
import asyncio, base64, json, logging, os, re, sys, time

sys.path.insert(0, "/opt/orchestrator/arena_agent")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, HTTPException           # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

import config as C                                             # noqa: E402
from engine import ArenaEngine, ArenaError, uuid7              # noqa: E402
from models import Registry                                    # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("arena-gw")

registry = Registry(C.CATALOG, C.VERIFIED,
                    os.path.join(C.DATA_DIR, "model_aliases.json"))
engine = ArenaEngine(C)
app = FastAPI(title="Arena OpenAI Gateway", version="1.0.0")
PROBE_STATE = {"running": False, "done": 0, "total": 0, "results": [], "error": None}


# ------------------------------------------------------------------ сервисы
@app.on_event("startup")
async def _startup():
    os.makedirs(C.LOG_DIR, exist_ok=True)
    log.info("реестр моделей: %s", json.dumps(registry.stats(), ensure_ascii=False))
    try:
        await engine.start()
    except Exception as e:
        log.error("не удалось поднять вкладку: %s", e)


@app.on_event("shutdown")
async def _shutdown():
    try:
        await engine.stop()
    except Exception:
        pass


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("необработанная ошибка на %s", request.url.path)
    return _err(500, "internal", "%s: %s" % (type(exc).__name__, exc)[:400])


def _auth(request: Request):
    if not C.AUTH_ENABLED:
        return
    tok = C.load_token()
    if not tok:
        return
    host = request.client.host if request.client else ""
    if C.ALLOW_LOCAL_NOAUTH and host in ("127.0.0.1", "::1", "localhost"):
        return
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("bearer ") and hdr[7:].strip() == tok:
        return
    if request.headers.get("x-arena-token", "") == tok:
        return
    raise HTTPException(status_code=401, detail="неверный токен (Authorization: Bearer …)")


def _err(status, code, message, retry_after=None):
    body = {"error": {"message": message, "type": code, "code": status}}
    headers = {"Retry-After": str(int(retry_after))} if retry_after else None
    return JSONResponse(status_code=status, content=body, headers=headers)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" or "text" in p:
                    out.append(str(p.get("text", "")))
                elif p.get("type") in ("image_url", "input_image"):
                    out.append("[изображение во входе не поддерживается шлюзом]")
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(x for x in out if x)
    return str(content or "")


def build_prompt(messages):
    sys_parts, convo, has_images = [], [], False
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        raw = m.get("content")
        if isinstance(raw, list) and any(
                isinstance(p, dict) and p.get("type") in ("image_url", "input_image")
                for p in raw):
            has_images = True
        txt = _text_of(raw).strip()
        if not txt:
            continue
        if role in ("system", "developer"):
            sys_parts.append(txt)
        elif role == "tool":
            convo.append(("инструмент", txt))
        else:
            label = {"user": "пользователь", "assistant": "ассистент"}.get(role, role)
            convo.append((label, txt))
    if not convo:
        raise ArenaError(400, "bad_request", "в messages нет текстовых сообщений")
    if not sys_parts and len(convo) == 1 and convo[0][0] == "user" and not has_images:
        return convo[0][1]
    blocks = []
    if sys_parts:
        blocks.append("Системные инструкции:\n" + "\n\n".join(sys_parts))
    if len(convo) > 1:
        blocks.append("История диалога:\n" + "\n".join("%s: %s" % (r, t) for r, t in convo))
    else:
        blocks.append("Сообщение пользователя: %s" % convo[0][1])
    blocks.append("Дай ответ на последнее сообщение пользователя.")
    return "\n\n".join(blocks)


def _usage(prompt, text):
    pt = max(1, len(prompt) // 4)
    ct = max(1, len(text) // 4)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


IMG_URL_RE = re.compile(r"https?://[^\s\"'\\)]+\.(?:png|jpe?g|webp|gif)[^\s\"'\\)]*", re.I)
IMG_B64_RE = re.compile(r"data:image/([a-z+]+);base64,([A-Za-z0-9+/=\s]+)")


def extract_images(parts, text):
    out = []
    blob = json.dumps(parts, ensure_ascii=False) + "\n" + (text or "")
    for m in IMG_B64_RE.finditer(blob):
        raw = re.sub(r"\s+", "", m.group(2))
        out.append({"b64_json": raw, "media_type": "image/" + m.group(1)})
    for m in IMG_URL_RE.finditer(blob):
        out.append({"url": m.group(0)})
    for p in parts or []:
        d = p.get("data")
        if isinstance(d, dict):
            for k in ("url", "imageUrl", "src"):
                v = d.get(k)
                if isinstance(v, str) and v.startswith("http") and v not in [o.get("url") for o in out]:
                    out.append({"url": v})
    return out


# ------------------------------------------------------------------ эндпоинты
@app.get("/health")
@app.get("/v1/health")
async def health(deep: bool = False):
    return await engine.health(deep=deep)


@app.get("/v1/arena/status")
async def status(request: Request):
    _auth(request)
    h = await engine.health(deep=False)
    h["registry"] = registry.stats()
    h["probe"] = {k: v for k, v in PROBE_STATE.items() if k != "results"}
    h["probe"]["results"] = PROBE_STATE["results"][-12:]
    h["config"] = {"min_interval_s": C.MIN_INTERVAL, "burst": "%d/%dс" % (C.BURST_LIMIT, C.BURST_WINDOW),
                   "total_timeout_s": C.TOTAL_TIMEOUT, "idle_timeout_s": C.STREAM_IDLE_TIMEOUT,
                   "first_byte_timeout_s": C.FIRST_BYTE_TIMEOUT, "cleanup": C.CLEANUP_MODE,
                   "v2_enabled": C.V2_ENABLED, "default_model": C.DEFAULT_MODEL}
    return h


@app.get("/v1/models")
async def list_models(request: Request, all: bool = False, verified: bool = False,
                      modality: str = None, limit: int = 60):
    _auth(request)
    if all:
        items = registry.openai_models(only_selectable=True, only_verified=verified)
    else:
        items = registry.openai_models(only_selectable=True, only_verified=True)
        if not items:
            items = registry.openai_models(only_selectable=True)[:limit]
    if modality:
        items = [m for m in items if modality in m["arena_modalities"]]

    def key(d):
        m = registry.by_id.get(d["arena_id"])
        rank = m.best_rank if m else 9e18
        return (not d["arena_verified"], "chat" not in d["arena_modalities"], rank, d["id"])

    items.sort(key=key)
    return {"object": "list", "data": items if all else items[:limit]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _auth(request)
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_request", "тело запроса не JSON")
    return await _complete(request, body, legacy=False)


@app.post("/v1/completions")
async def completions(request: Request):
    _auth(request)
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_request", "тело запроса не JSON")
    if "prompt" in body and "messages" not in body:
        p = body["prompt"]
        if isinstance(p, list):
            p = "\n".join(str(x) for x in p)
        body["messages"] = [{"role": "user", "content": str(p)}]
    return await _complete(request, body, legacy=True)


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    _auth(request)
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_request", "тело запроса не JSON")
    prompt = body.get("prompt") or ""
    if not prompt:
        return _err(400, "bad_request", "нужен prompt")
    n = max(1, min(int(body.get("n") or 1), 4))
    want = body.get("model") or ""
    results = []
    for i in range(n):
        name = want
        if not name:
            pool = [m for m in registry.models if m.selectable and "image" in m.ranks]
            pool.sort(key=lambda m: m.best_rank)
            name = pool[0].public_name if pool else "flux-2-pro"
        m, modality, err = registry.resolve(name, "image")
        if not m:
            return _err(400, "model_not_found", err)
        try:
            res = await engine.evaluate(model_id=m.id, prompt=prompt, modality="image")
        except ArenaError as e:
            return _err(e.status, e.code, e.message, e.retry_after)
        imgs = extract_images(res.get("parts"), res.get("text"))
        results.append(imgs[0] if imgs else {"b64_json": "", "note": "картинка не получена",
                                            "debug": json.dumps(res.get("parts"))[:1500]})
        asyncio.create_task(engine.cleanup(res.get("eval_id")))
    fmt = body.get("response_format", "url")
    data = []
    for r in results:
        if r.get("b64_json") and fmt != "url":
            data.append({"b64_json": r["b64_json"]})
        elif r.get("url"):
            data.append({"url": r["url"]})
        elif r.get("b64_json"):
            data.append({"b64_json": r["b64_json"]})
        else:
            data.append(r)
    return {"created": int(time.time()), "data": data}


async def _complete(request: Request, body, legacy=False):
    model_name = body.get("model") or C.DEFAULT_MODEL
    m, modality, err = registry.resolve(model_name, body.get("arena_modality"))
    if not m:
        return _err(404, "model_not_found", err)
    mode = body.get("arena_mode") or "direct-battle"
    session_id = body.get("arena_session") or request.headers.get("x-arena-session")
    wait_budget = body.get("arena_wait_budget", C.COOLDOWN_MAX_WAIT)
    if legacy:
        prompt = build_prompt(body.get("messages") or
                              [{"role": "user", "content": body.get("prompt", "")}])
    else:
        prompt = build_prompt(body.get("messages"))
    stream = bool(body.get("stream"))
    cid = "chatcmpl-" + uuid7()
    created = int(time.time())
    display = m.public_name or m.id

    if stream:
        async def gen():
            queue = asyncio.Queue(maxsize=500)
            loop = asyncio.get_event_loop()

            def on_delta(s):
                loop.call_soon_threadsafe(queue.put_nowait, ("delta", s))

            async def worker():
                try:
                    res = await engine.evaluate(model_id=m.id, prompt=prompt,
                                                modality=modality, mode=mode,
                                                session_id=session_id,
                                                extra={"on_delta": on_delta},
                                                wait_budget=wait_budget)
                    queue.put_nowait(("done", res))
                except ArenaError as e:
                    queue.put_nowait(("error", e))
                except Exception as e:
                    queue.put_nowait(("error", ArenaError(500, "internal", str(e))))

            first = {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": display,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                  "finish_reason": None}]}
            yield "data: " + json.dumps(first, ensure_ascii=False) + "\n\n"
            task = asyncio.create_task(worker())
            text_len = 0
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "delta":
                        text_len += len(payload)
                        chunk = {"id": cid, "object": "chat.completion.chunk",
                                 "created": created, "model": display,
                                 "choices": [{"index": 0, "delta": {"content": payload},
                                              "finish_reason": None}]}
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    elif kind == "done":
                        fin = {"id": cid, "object": "chat.completion.chunk",
                               "created": created, "model": display,
                               "choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}],
                               "usage": _usage(prompt, "x" * text_len)}
                        yield "data: " + json.dumps(fin, ensure_ascii=False) + "\n\n"
                        yield "data: [DONE]\n\n"
                        asyncio.create_task(engine.cleanup(payload.get("eval_id")))
                        break
                    else:
                        e = payload
                        err_chunk = {"error": {"message": e.message, "type": e.code,
                                               "code": e.status}}
                        yield "data: " + json.dumps(err_chunk, ensure_ascii=False) + "\n\n"
                        yield "data: [DONE]\n\n"
                        break
            finally:
                if not task.done():
                    task.cancel()
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "X-Arena-Model-Id": m.id,
                                          "X-Arena-Modality": modality})

    try:
        res = await engine.evaluate(model_id=m.id, prompt=prompt, modality=modality,
                                    mode=mode, session_id=session_id,
                                    wait_budget=wait_budget)
    except ArenaError as e:
        return _err(e.status, e.code, e.message, e.retry_after)
    except Exception as e:
        log.exception("внутренняя ошибка")
        return _err(500, "internal", str(e)[:300])
    text = res.get("text") or ""
    asyncio.create_task(engine.cleanup(res.get("eval_id")))
    out = {
        "id": cid, "object": "chat.completion" if not legacy else "text_completion",
        "created": created, "model": display,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": _usage(prompt, text),
        "arena": {"evaluation_id": res.get("eval_id"), "model_id": m.id,
                  "provider": m.provider, "modality": modality, "mode": mode,
                  "upstream_ms": res.get("ms"), "bytes": res.get("bytes"),
                  "recaptcha_ms": res.get("token_ms"),
                  "extra_parts": len(res.get("parts") or [])},
    }
    if legacy:
        out["choices"][0] = {"index": 0, "text": text, "finish_reason": "stop", "logprobs": None}
    return JSONResponse(out)


# ------------------------------------------------------------------ пауза
@app.post("/v1/arena/pause")
async def arena_pause(request: Request):
    """Ручной «тихий режим»: {"seconds": 3600} — не ходить в арену час.

    {"seconds": 0} — снять кулдаун; {"reset_interval": true} — вернуть темп к
    ARENA_GW_MIN_INTERVAL. Нужно, чтобы дать флагу reCAPTCHA спасть: каждая
    попытка во время флага продлевает его.
    """
    _auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return engine.pause(float(body.get("seconds") or 0),
                        reset_interval=bool(body.get("reset_interval")))


# ------------------------------------------------------------------ пробник
@app.post("/v1/arena/probe")
async def probe(request: Request):
    _auth(request)
    if PROBE_STATE["running"]:
        return _err(409, "busy", "пробник уже запущен")
    try:
        body = await request.json()
    except Exception:
        body = {}
    limit = int(body.get("limit") or 6)
    per_provider = int(body.get("per_provider") or 1)
    modality = body.get("modality") or "chat"
    ids = body.get("models")
    asyncio.create_task(_run_probe(limit, per_provider, modality, ids))
    return {"started": True, "limit": limit, "modality": modality}


async def _run_probe(limit, per_provider, modality, ids):
    PROBE_STATE.update(running=True, done=0, total=0, results=[], error=None)
    try:
        if ids:
            cases = []
            for i in ids:
                m, _mod, err = registry.resolve(i, modality)
                if m:
                    cases.append(m)
                else:
                    log.warning("probe: %s не разрешено (%s)", i, err)
        else:
            import collections as _c
            by_prov = _c.defaultdict(list)
            for m in registry.models:
                if not m.selectable:
                    continue
                if modality != "chat" and modality not in m.ranks:
                    continue
                if modality == "chat" and not m.ranks:
                    continue
                by_prov[m.provider or m.organization or "?"].append(m)
            cases = []
            for prov, lst in by_prov.items():
                lst.sort(key=lambda x: x.best_rank)
                cases.extend(lst[:per_provider])
            cases.sort(key=lambda x: x.best_rank)
            cases = cases[:limit]
        PROBE_STATE["total"] = len(cases)
        verified = {}
        if os.path.exists(C.VERIFIED):
            try:
                verified = json.load(open(C.VERIFIED)).get("models", {})
            except Exception:
                verified = {}
        prompt = "Ответь одним словом: ОК"
        for cs in cases:
            mid_ = cs.id if hasattr(cs, "id") else cs["id"]
            name = (getattr(cs, "public_name", None) or
                    (cs.get("name") if isinstance(cs, dict) else None))
            prov = (getattr(cs, "provider", None) or
                    (cs.get("provider") if isinstance(cs, dict) else None)) or ""
            try:
                res = await engine.evaluate(model_id=mid_, prompt=prompt, modality=modality)
                rec = {"ok": True, "status": 200, "text": (res.get("text") or "")[:80],
                       "ms": res.get("ms"), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                await engine.cleanup(res.get("eval_id"))
            except ArenaError as e:
                rec = {"ok": False, "status": e.status, "code": e.code,
                       "error": e.message[:200], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            except Exception as e:
                rec = {"ok": False, "status": 500, "code": "internal",
                       "error": str(e)[:200], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            rec.update({"name": name, "provider": prov, "modality": modality, "id": mid_})
            verified[mid_] = rec
            registry.mark_verified(mid_, rec["ok"])
            PROBE_STATE["results"].append(rec)
            PROBE_STATE["done"] += 1
            log.info("probe %s/%s → %s %s", PROBE_STATE["done"], PROBE_STATE["total"],
                     rec.get("status"), name)
        json.dump({"updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "modality": modality,
                   "models": verified}, open(C.VERIFIED, "w"), ensure_ascii=False, indent=1)
    except Exception as e:
        PROBE_STATE["error"] = str(e)[:300]
        log.exception("пробник упал")
    finally:
        PROBE_STATE["running"] = False


@app.delete("/v1/arena/chats/{chat_id}")
async def drop_chat(chat_id: str, request: Request):
    _auth(request)
    return await engine.cleanup(chat_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="info")
