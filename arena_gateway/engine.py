"""
Движок шлюза: одна выделенная вкладка Chrome (CDP) → reCAPTCHA Enterprise →
POST /nextjs-api/stream/create-evaluation → разбор SSE-потока арены.

Внутри: очередь с темпом (анти-кулдаун), вотчдоги (токен, первый байт, простой,
общее время), эскалация до reCAPTCHA v2, ретраи, очистка чата после ответа.
"""
import asyncio, collections, json, logging, os, re, secrets, time, urllib.request, uuid

import websockets

log = logging.getLogger("arena-gw")

ORIGIN = "https://arena.ai"
LINE_RE = re.compile(r"^([0-9a-z]{1,3}):(.*)$")

# коды протокола потока (AI SDK data stream) — префикс слота модели a/b
CODE_TEXT, CODE_DATA, CODE_ERROR, CODE_TOOL_CALL = "0", "1", "3", "9"
CODE_TOOL_RESULT, CODE_TOOL_DELTA, CODE_FINISH = "a", "c", "d"


class ArenaError(Exception):
    """Ошибка с HTTP-статусом для клиента и, при необходимости, retry_after."""

    def __init__(self, status, code, message, retry_after=None, upstream=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.upstream = upstream

    def to_dict(self):
        d = {"error": {"message": self.message, "type": self.code, "code": self.status}}
        if self.retry_after:
            d["error"]["retry_after"] = self.retry_after
        if self.upstream:
            d["error"]["upstream"] = self.upstream
        return d


def uuid7():
    ms = int(time.time() * 1000)
    val = (ms & 0xFFFFFFFFFFFF) << 80
    val |= 0x7 << 76
    val |= secrets.randbits(12) << 64
    val |= 0b10 << 62
    val |= secrets.randbits(62)
    return str(uuid.UUID(int=val))


# ------------------------------------------------------------------ JS-ядро
JS_INSTALL = r"""
(() => {
  const V = 4;
  if (window.__agw && window.__agw.version === V) return 'ok';
  window.__agw = {version: V, jobs: {}, v2: {token: null, err: null, state: 'idle'}};

  window.__agwStart = function (cfg) {
    const id = cfg.jobId;
    const controller = new AbortController();
    const j = {status: null, ctype: null, headers: {}, raw: '', done: false, err: null,
               tokenErr: null, tokenMs: null, startedAt: Date.now(), ms: null,
               controller: controller, timeout: false, overflow: false};
    window.__agw.jobs[id] = j;
    (async () => {
      try {
        const payload = Object.assign({}, cfg.payload);
        if (cfg.v2token) {
          payload.recaptchaV2Token = cfg.v2token;
        } else {
          try {
            await new Promise((res, rej) => {
              if (!window.grecaptcha || !grecaptcha.enterprise || !grecaptcha.enterprise.ready)
                return rej(new Error('нет grecaptcha.enterprise'));
              grecaptcha.enterprise.ready(res);
              setTimeout(() => rej(new Error('recaptcha ready timeout')),
                         cfg.tokenTimeoutMs || 15000);
            });
            const t0 = Date.now();
            payload.recaptchaV3Token = await grecaptcha.enterprise.execute(
              cfg.v3key, {action: cfg.action || 'chat_submit'});
            j.tokenMs = Date.now() - t0;
          } catch (e) { j.tokenErr = String(e && e.message || e); }
        }
        const resp = await fetch(cfg.endpoint, {
          method: 'POST', headers: {'content-type': 'application/json'},
          credentials: 'include', body: JSON.stringify(payload), signal: controller.signal});
        j.status = resp.status;
        j.ctype = resp.headers.get('content-type');
        ['ratelimit','ratelimit-policy','retry-after','cf-ray'].forEach(k => {
          const v = resp.headers.get(k); if (v) j.headers[k] = v; });
        const rd = resp.body.getReader(), dec = new TextDecoder();
        while (true) {
          const r = await rd.read();
          if (r.done) break;
          j.raw += dec.decode(r.value, {stream: true});
          if (j.raw.length > (cfg.maxBytes || 8000000)) {
            j.overflow = true; try { rd.cancel(); } catch (e) {} break; }
          if (Date.now() - j.startedAt > (cfg.maxMs || 300000)) {
            j.timeout = true; try { controller.abort(); } catch (e) {} break; }
        }
      } catch (e) { j.err = String(e && e.message || e); }
      j.done = true; j.ms = Date.now() - j.startedAt;
    })();
    return id;
  };

  window.__agwPoll = function (id, from) {
    const j = window.__agw.jobs[id];
    if (!j) return JSON.stringify({missing: true});
    return JSON.stringify({
      status: j.status, ctype: j.ctype, headers: j.headers, done: j.done, err: j.err,
      tokenErr: j.tokenErr, tokenMs: j.tokenMs, timeout: j.timeout, overflow: j.overflow,
      ms: j.ms, len: j.raw.length,
      chunk: from < j.raw.length ? j.raw.slice(from) : ''});
  };

  window.__agwCancel = function (id) {
    const j = window.__agw.jobs[id];
    if (!j) return 'нет задания';
    try { j.controller.abort(); } catch (e) {}
    j.done = true; return 'отменено';
  };

  window.__agwFree = function (id) { delete window.__agw.jobs[id]; return Object.keys(window.__agw.jobs).length; };

  window.__agwFetch = async function (method, path, body) {
    try {
      const r = await fetch(path, {method: method, credentials: 'include',
        headers: body ? {'content-type': 'application/json'} : {},
        body: body ? JSON.stringify(body) : undefined});
      const t = await r.text();
      return JSON.stringify({status: r.status, body: t.slice(0, 4000)});
    } catch (e) { return JSON.stringify({status: 0, body: String(e)}); }
  };

  window.__agwAction = async function (actionId, args, path) {
    try {
      const r = await fetch(path || location.pathname, {
        method: 'POST',
        headers: {'content-type': 'text/plain;charset=UTF-8', 'next-action': actionId,
                  'accept': 'text/x-component'},
        credentials: 'include',
        body: JSON.stringify(args || [])});
      const t = await r.text();
      return JSON.stringify({status: r.status, body: t.slice(0, 2000)});
    } catch (e) { return JSON.stringify({status: 0, body: String(e)}); }
  };

  window.__agwV2Start = function (cfg) {
    const S = window.__agw.v2;
    S.token = null; S.err = null; S.state = 'rendering';
    (async () => {
      try {
        await new Promise((res, rej) => {
          if (!window.grecaptcha || !grecaptcha.enterprise)
            return rej(new Error('нет grecaptcha.enterprise'));
          grecaptcha.enterprise.ready(res);
          setTimeout(() => rej(new Error('ready timeout')), 10000);
        });
        let el = document.getElementById('arena-gw-v2');
        if (!el) {
          el = document.createElement('div');
          el.id = 'arena-gw-v2';
          el.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;';
          document.body.appendChild(el);
        }
        el.innerHTML = '';
        S.widget = grecaptcha.enterprise.render(el, {
          sitekey: cfg.v2key,
          callback: t => { S.token = t; S.state = 'solved'; },
          'error-callback': e => { S.err = String(e); S.state = 'error'; },
          'expired-callback': () => { S.token = null; S.state = 'expired'; }});
        S.state = 'rendered';
      } catch (e) { S.err = String(e && e.message || e); S.state = 'error'; }
    })();
    return 'started';
  };

  window.__agwV2Poll = function () {
    const S = window.__agw.v2;
    const f = document.querySelector('#arena-gw-v2 iframe');
    let box = null;
    if (f) { const r = f.getBoundingClientRect();
             box = {x: r.x, y: r.y, w: r.width, h: r.height}; }
    const bf = [...document.querySelectorAll('iframe')]
      .filter(x => (x.src || '').includes('bframe'))
      .map(x => { const r = x.getBoundingClientRect();
                  return {w: Math.round(r.width), h: Math.round(r.height), vis: x.offsetWidth > 0}; });
    return JSON.stringify({state: S.state, token: S.token, tokenLen: (S.token || '').length,
                           err: S.err, box: box, bframes: bf});
  };

  return 'installed';
})()
"""


# React Server Actions арены (удаление/архивация evaluation-сессий и пр.)
SERVER_ACTIONS = {
    "deleteEvaluationSession": "60918522605e167913bc622c7ffdd319e5265e9103",
    "archiveEvaluationSession": "600f1dc717d8b907d4bd4daf0200da652c01733fb8",
    "unarchiveEvaluationSession": "602ac143d568f6aaf7626e273929f57b427cfbc1dc",
    "generateUploadUrl": "70a65b3ae65ca24a5a01b1f3f8265e2e7b7f903b3f",
    "getSignedUrl": "6085a067c3f07dec61c8015283b03226d8e66b1925",
    "createPairwiseFeedback": "605c023e26b5d46808dc570b503b1b52c005d168f4",
    "getLeaderboardModels": "60e00474471d34cb120413e713c223854b338232ef",
}


def load_server_actions(path):
    """Дополнить реестр из data/arena/server_actions.json (если есть)."""
    out = dict(SERVER_ACTIONS)
    try:
        d = json.load(open(path))
        for k, v in d.items():
            if isinstance(v, list) and v:
                out[k] = v[0]
            elif isinstance(v, str):
                out[k] = v
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ движок
class ArenaEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tab = None                 # CDPTab
        self.target_id = None
        self._sem = asyncio.Semaphore(cfg.MAX_CONCURRENCY)
        self._lock = asyncio.Lock()
        self._last_ts = 0.0
        self._hits = collections.deque()
        self.cooldown_until = 0.0
        self.recaptcha_streak = 0   # подряд идущие отказы reCAPTCHA
        self.counters = collections.Counter()
        self.unknown_codes = collections.Counter()
        self.last_success = None
        self.last_error = None
        self.started_at = time.time()
        self.errors = collections.deque(maxlen=50)
        self._wd_task = None
        self.tab_created_at = None
        self.page_url = None
        self.interval = cfg.MIN_INTERVAL      # адаптивный интервал
        self._state_saved = 0.0
        self._load_state()
        self.actions = load_server_actions(
            os.path.join(cfg.DATA_DIR, "server_actions.json"))

    # ------------------------------------------------------------ состояние
    def _load_state(self):
        """Кулдаун и темп переживают перезапуск сервиса."""
        try:
            d = json.load(open(self.cfg.STATE))
        except Exception:
            return
        self.recaptcha_streak = int(d.get("recaptcha_streak") or 0)
        cu = float(d.get("cooldown_until") or 0)
        if cu > time.time():
            self.cooldown_until = cu
            log.warning("восстановлен кулдаун из состояния: ещё %d с",
                        int(cu - time.time()))
        iv = float(d.get("interval") or 0)
        if iv > self.interval:
            self.interval = min(iv, self.cfg.MAX_INTERVAL)
        for k, v in (d.get("counters") or {}).items():
            self.counters[k] = v

    def _save_state(self, force=False):
        now = time.time()
        if not force and now - self._state_saved < 20:
            return
        self._state_saved = now
        try:
            os.makedirs(os.path.dirname(self.cfg.STATE), exist_ok=True)
            json.dump({"cooldown_until": self.cooldown_until, "interval": self.interval,
                       "recaptcha_streak": self.recaptcha_streak,
                       "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "counters": dict(self.counters)},
                      open(self.cfg.STATE, "w"), ensure_ascii=False, indent=1)
        except Exception as e:
            log.warning("не сохранил состояние: %s", e)

    # ------------------------------------------------------------ CDP / вкладка
    def _cdp_targets(self):
        with urllib.request.urlopen(self.cfg.CDP + "/json/list", timeout=10) as r:
            return json.loads(r.read().decode())

    def _open_tab(self, url):
        import urllib.parse
        req = urllib.request.Request(
            self.cfg.CDP + "/json/new?" + urllib.parse.quote(url, safe=":/?=&"),
            method="PUT")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    def _close_tab(self, target_id):
        try:
            urllib.request.urlopen(self.cfg.CDP + "/json/close/" + target_id, timeout=10).read()
            return True
        except Exception:
            return False

    async def ensure_tab(self, force=False):
        """Своя вкладка на /text/direct. Чужие не трогаем."""
        alive = False
        if self.target_id and not force:
            try:
                alive = any(t.get("id") == self.target_id for t in self._cdp_targets())
            except Exception:
                alive = False
        if alive and self.tab and not self.tab.closed:
            return self.tab
        if force and self.target_id:
            self._close_tab(self.target_id)
            self.target_id, self.tab = None, None
        # ищем нашу прежнюю вкладку (помечена URL)
        try:
            for t in self._cdp_targets():
                if t.get("type") == "page" and (t.get("url") or "").startswith(self.cfg.DIRECT_URL):
                    self.target_id = t["id"]
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            else:
                raise IndexError
        except Exception:
            info = self._open_tab(self.cfg.DIRECT_URL)
            self.target_id = info["id"] if isinstance(info, dict) else info
            ws_url = None
            for _ in range(40):
                for t in self._cdp_targets():
                    if t.get("id") == self.target_id:
                        ws_url = t.get("webSocketDebuggerUrl")
                if ws_url:
                    break
                await asyncio.sleep(0.5)
            if not ws_url:
                raise ArenaError(502, "cdp", "не получил websocket вкладки")
        from arena_api import CDPTab
        self.tab = await CDPTab(ws_url, self.target_id).connect()
        self.tab_created_at = time.time()
        await asyncio.sleep(self.cfg.PAGE_WAIT)
        self.page_url = await self.js("location.href", timeout=20)
        if not self.page_url or "arena.ai" not in str(self.page_url):
            await self.tab.cmd("Page.enable")
            await self.tab.cmd("Page.navigate", {"url": self.cfg.DIRECT_URL})
            await asyncio.sleep(self.cfg.PAGE_WAIT)
            self.page_url = await self.js("location.href", timeout=20)
        await self._install_js()
        self.counters["tab_opened"] += 1
        log.info("вкладка готова: %s (%s)", self.target_id, self.page_url)
        return self.tab

    async def reload_page(self, wait=None):
        """Перезагрузить страницу вкладки — сбрасывает сессию reCAPTCHA."""
        if not self.tab or self.tab.closed:
            await self.ensure_tab(force=True)
            return self.page_url
        await self.tab.cmd("Page.enable")
        await self.tab.cmd("Page.reload", {"ignoreCache": False})
        await asyncio.sleep(wait or self.cfg.PAGE_WAIT)
        self.page_url = await self.js("location.href", timeout=20)
        await self._install_js()
        self.counters["page_reloaded"] += 1
        log.info("страница перезагружена: %s", self.page_url)
        return self.page_url

    async def _install_js(self):
        out = await self.js(JS_INSTALL, timeout=30)
        if out not in ("ok", "installed"):
            raise ArenaError(502, "cdp", "не удалось установить JS-ядро: %s" % out)
        return out

    async def js(self, expr, timeout=120):
        if not self.tab or self.tab.closed:
            await self.ensure_tab()
        try:
            return await self.tab.js(expr, timeout=timeout)
        except Exception as e:
            log.warning("js-ошибка (%s) — переподключаю вкладку", e)
            await self.ensure_tab(force=True)
            return await self.tab.js(expr, timeout=timeout)

    async def start(self):
        await self.ensure_tab()
        if not self._wd_task:
            self._wd_task = asyncio.create_task(self.watchdog())

    async def stop(self):
        if self._wd_task:
            self._wd_task.cancel()
            self._wd_task = None
        if self.cfg.CLEANUP and self.target_id:
            self._close_tab(self.target_id)

    # ------------------------------------------------------------ темп/кулдаун
    def _slow_down(self, reason, penalty=None, escalate=False):
        """Отказ арены → увеличиваем интервал и (опционально) уходим в штрафной кулдаун.

        escalate=True: штраф удваивается за каждой неудачей подряд (до PENALTY_MAX) —
        флаг reCAPTCHA на аккаунте живёт дольше, чем одиночный штрафной кулдаун,
        и регулярные ретраи лишь продлевают его.
        """
        self.interval = min(self.cfg.MAX_INTERVAL,
                            max(self.interval * self.cfg.BACKOFF_FACTOR, 60))
        penalty = float(penalty or 0)
        if penalty and escalate and self.cfg.PENALTY_ESCALATE and self.recaptcha_streak > 1:
            penalty = min(self.cfg.PENALTY_MAX,
                          penalty * (2 ** (self.recaptcha_streak - 1)))
        if penalty:
            until = time.time() + penalty
            if until > self.cooldown_until:
                self.cooldown_until = until
        log.warning("темп снижен до %d с (%s), кулдаун %d с",
                    int(self.interval), reason,
                    max(0, int(self.cooldown_until - time.time())))
        self._save_state(force=True)
        return int(penalty)

    def _speed_up(self):
        self.recaptcha_streak = 0
        self.interval = max(self.cfg.MIN_INTERVAL, self.interval * self.cfg.RECOVER_FACTOR)
        self._save_state()

    def pause(self, seconds, reset_interval=False):
        """Ручное управление кулдауном.

        seconds > 0 — «тихий режим»: шлюз не ходит в арену и отвечает 503 + Retry-After.
        seconds == 0 — снять кулдаун (и обнулить счётчик неудач подряд).
        reset_interval — вернуть адаптивный интервал к MIN_INTERVAL.
        """
        seconds = float(seconds or 0)
        if seconds > 0:
            self.cooldown_until = max(self.cooldown_until, time.time() + seconds)
            log.warning("ручная пауза %d с (до %s)", int(seconds),
                        time.strftime("%H:%M:%S UTC", time.gmtime(self.cooldown_until)))
        else:
            self.cooldown_until = 0.0
            self.recaptcha_streak = 0
            log.warning("пауза снята вручную")
        if reset_interval:
            self.interval = self.cfg.MIN_INTERVAL
        self._save_state(force=True)
        return {"cooldown_remaining_s": max(0, int(self.cooldown_until - time.time())),
                "paused_until_utc": time.strftime("%H:%M:%S", time.gmtime(self.cooldown_until))
                if self.cooldown_until > time.time() else None,
                "adaptive_interval_s": int(self.interval),
            "recaptcha_streak": self.recaptcha_streak,
                "recaptcha_streak": self.recaptcha_streak}

    def _note_cooldown(self, retry_after):
        try:
            ra = float(retry_after)
        except (TypeError, ValueError):
            return
        if ra > 0:
            until = time.time() + min(ra, 7200)
            if until > self.cooldown_until:
                self.cooldown_until = until
                log.warning("кулдаун арены: %d с (retry-after=%s)", int(ra), retry_after)
                self._save_state(force=True)

    async def _gate(self, wait_budget):
        async with self._sem:
            deadline = time.time() + (wait_budget if wait_budget else 0)
            while True:
                now = time.time()
                wait = self.cooldown_until - now
                if wait > 0:
                    if wait_budget is not None and now + wait > deadline + 1:
                        raise ArenaError(503, "cooldown",
                                         "арена в кулдауне ещё %d с" % int(wait),
                                         retry_after=int(wait))
                    await asyncio.sleep(min(wait, 5))
                    continue
                while self._hits and now - self._hits[0] > self.cfg.BURST_WINDOW:
                    self._hits.popleft()
                if len(self._hits) >= self.cfg.BURST_LIMIT:
                    wait = self.cfg.BURST_WINDOW - (now - self._hits[0])
                    if wait_budget is not None and now + wait > deadline + 1:
                        raise ArenaError(429, "rate_limited",
                                         "локальный лимит шлюза: %d запросов за %d с"
                                         % (self.cfg.BURST_LIMIT, int(self.cfg.BURST_WINDOW)),
                                         retry_after=int(wait) + 1)
                    await asyncio.sleep(min(max(wait, 0.5), 10))
                    continue
                wait = self.interval - (now - self._last_ts)
                if wait > 0:
                    if wait_budget is not None and now + wait > deadline + 1:
                        raise ArenaError(429, "rate_limited",
                                         "соблюдаю интервал %d с между запросами"
                                         % int(self.interval), retry_after=int(wait) + 1)
                    await asyncio.sleep(wait)
                break
            async with self._lock:
                self._last_ts = time.time()
                self._hits.append(self._last_ts)

    # ------------------------------------------------------------ reCAPTCHA v2
    async def recaptcha_v2(self):
        """Клик по чекбоксу reCAPTCHA v2 (эскалация). → токен или None."""
        if not self.cfg.V2_ENABLED:
            return None
        await self._install_js()
        await self.js("window.__agwV2Start(%s)" % json.dumps(
            {"v2key": self.cfg.RECAPTCHA_V2_SITEKEY}), timeout=30)
        t0 = time.time()
        clicked = 0
        while time.time() - t0 < self.cfg.V2_TIMEOUT:
            await asyncio.sleep(1.2)
            raw = await self.js("window.__agwV2Poll()", timeout=20)
            try:
                d = json.loads(raw or "{}")
            except Exception:
                continue
            if d.get("token"):
                log.info("reCAPTCHA v2 решена за %.1f с", time.time() - t0)
                self.counters["v2_solved"] += 1
                return d["token"]
            if d.get("err"):
                log.warning("reCAPTCHA v2 ошибка: %s", d["err"])
                return None
            box = d.get("box")
            if box and box.get("w", 0) > 50 and clicked < 3:
                challenge = [b for b in (d.get("bframes") or [])
                             if b.get("vis") and b.get("h", 0) > 250]
                if challenge:
                    log.warning("reCAPTCHA v2 показала картинный челлендж — нужен человек")
                    self.counters["v2_challenge"] += 1
                    return None
                await self._click(box["x"] + 32, box["y"] + box["h"] / 2)
                clicked += 1
                self.counters["v2_clicks"] += 1
        self.counters["v2_timeout"] += 1
        return None

    async def _click(self, x, y):
        for t, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            await self.tab.cmd("Input.dispatchMouseEvent", {
                "type": t, "x": int(x), "y": int(y), "button": "left",
                "clickCount": 1, "buttons": buttons})
        await asyncio.sleep(0.3)

    # ------------------------------------------------------------ поток
    def _parse_line(self, line):
        m = LINE_RE.match(line)
        if not m:
            return None
        prefix, rest = m.group(1), m.group(2)
        slot = "a"
        code = prefix
        if len(prefix) > 1 and prefix[0] in "ab":
            slot, code = prefix[0], prefix[1:]
        try:
            data = json.loads(rest)
        except Exception:
            data = rest
        return {"slot": slot, "code": code, "data": data}

    async def _run_job(self, endpoint, payload, v2token=None, total_deadline=None):
        """Запуск запроса в странице + опрос. → async generator событий."""
        job_id = uuid7()
        cfgjs = {
            "jobId": job_id,
            "endpoint": endpoint,
            "payload": payload,
            "v3key": self.cfg.RECAPTCHA_V3_SITEKEY,
            "v2key": self.cfg.RECAPTCHA_V2_SITEKEY,
            "action": self.cfg.RECAPTCHA_ACTION,
            "tokenTimeoutMs": int(self.cfg.TOKEN_TIMEOUT * 1000),
            "maxBytes": self.cfg.MAX_STREAM_BYTES,
            "maxMs": int(self.cfg.TOTAL_TIMEOUT * 1000),
        }
        if v2token:
            cfgjs["v2token"] = v2token
        await self._install_js()
        await self.js("window.__agwStart(%s)" % json.dumps(cfgjs), timeout=30)

        pos, buf, full = 0, "", ""
        t0 = time.time()
        first_byte_at = None
        last_data_at = t0
        status_seen = None
        try:
            while True:
                if total_deadline and time.time() > total_deadline:
                    await self.js("window.__agwCancel(%s)" % json.dumps(job_id), timeout=15)
                    raise ArenaError(504, "timeout",
                                     "превышено общее время ответа (%d с)"
                                     % int(self.cfg.TOTAL_TIMEOUT))
                await asyncio.sleep(self.cfg.POLL_INTERVAL)
                raw = await self.js("window.__agwPoll(%s, %d)" % (json.dumps(job_id), pos),
                                    timeout=30)
                try:
                    d = json.loads(raw or "{}")
                except Exception:
                    continue
                if d.get("missing"):
                    # страница перезагрузилась — ядро потеряло задание
                    raise ArenaError(502, "tab_reset",
                                     "вкладка перезагрузилась во время запроса")
                if d.get("status") is not None and status_seen != d["status"]:
                    status_seen = d["status"]
                    yield {"type": "http", "status": d["status"],
                           "ctype": d.get("ctype"), "headers": d.get("headers") or {}}
                    if d.get("headers", {}).get("retry-after"):
                        self._note_cooldown(d["headers"]["retry-after"])
                chunk = d.get("chunk") or ""
                if chunk:
                    if first_byte_at is None:
                        first_byte_at = time.time()
                        yield {"type": "first_byte", "ms": int((first_byte_at - t0) * 1000)}
                    last_data_at = time.time()
                    pos = d.get("len", pos + len(chunk))
                    buf += chunk
                    if len(full) < 20000:
                        full += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        p = self._parse_line(line)
                        if not p:
                            continue
                        yield {"type": "part", **p}
                elif pos < (d.get("len") or 0):
                    pos = d["len"]
                # вотчдоги
                now = time.time()
                if status_seen is None and now - t0 > self.cfg.FIRST_BYTE_TIMEOUT:
                    await self.js("window.__agwCancel(%s)" % json.dumps(job_id), timeout=15)
                    raise ArenaError(504, "no_response",
                                     "арена не ответила за %d с"
                                     % int(self.cfg.FIRST_BYTE_TIMEOUT))
                if first_byte_at and now - last_data_at > self.cfg.STREAM_IDLE_TIMEOUT:
                    await self.js("window.__agwCancel(%s)" % json.dumps(job_id), timeout=15)
                    raise ArenaError(504, "stream_stalled",
                                     "поток завис: нет данных %d с"
                                     % int(self.cfg.STREAM_IDLE_TIMEOUT))
                if d.get("done"):
                    if buf.strip():
                        p = self._parse_line(buf.strip())
                        if p:
                            yield {"type": "part", **p}
                    yield {"type": "job_done", "ms": d.get("ms"), "bytes": d.get("len"),
                           "err": d.get("err"), "tokenErr": d.get("tokenErr"),
                           "tokenMs": d.get("tokenMs"), "timeout": d.get("timeout"),
                           "overflow": d.get("overflow"), "status": d.get("status"),
                           "raw_head": full[:1200]}
                    break
        finally:
            try:
                await self.js("window.__agwFree(%s)" % json.dumps(job_id), timeout=15)
            except Exception:
                pass

    # ------------------------------------------------------------ главный вызов
    async def evaluate(self, *, model_id, prompt, modality="chat",
                       mode="direct-battle", session_id=None, extra=None,
                       wait_budget=None, collect=True):
        """
        Один запрос к арене. Возвращает dict:
        {ok, text, reasoning, parts, finish_reason, eval_id, status, ms,
         model_id, modality, raw_codes, error}
        Также стримит дельты через колбэк on_delta (если передан).
        """
        on_delta = (extra or {}).pop("on_delta", None) if extra else None
        eval_id = session_id or uuid7()
        endpoint = ("/nextjs-api/stream/post-to-evaluation/%s" % eval_id
                    if session_id else "/nextjs-api/stream/create-evaluation")
        payload = {
            "id": eval_id,
            "modality": modality,
            "modelAId": model_id,
            "userMessageId": uuid7(),
            "modelAMessageId": uuid7(),
            "userMessage": {"content": prompt, "experimental_attachments": [],
                            "metadata": {}},
        }
        if not session_id:
            payload["mode"] = mode
        if extra:
            payload.update(extra)

        total_deadline = time.time() + self.cfg.TOTAL_TIMEOUT + 30
        attempt_v2 = False
        last = None
        for attempt in range(1 + self.cfg.RETRIES_PROMPT_FAILED + (1 if self.cfg.V2_ENABLED else 0)):
            await self._gate(wait_budget)
            text_parts, reason_parts, other, finish = [], [], [], None
            http_status, err_body = None, None
            v2token = None
            if attempt_v2:
                v2token = await self.recaptcha_v2()
                if not v2token:
                    raise ArenaError(403, "recaptcha_v2",
                                     "не удалось решить reCAPTCHA v2 (эскалация арены)")
            try:
                async for ev in self._run_job(endpoint, payload, v2token=v2token,
                                              total_deadline=total_deadline):
                    t = ev["type"]
                    if t == "http":
                        http_status = ev["status"]
                    elif t == "part":
                        code, data = ev["code"], ev["data"]
                        if code == CODE_TEXT:
                            s = data if isinstance(data, str) else json.dumps(data)
                            text_parts.append(s)
                            if on_delta:
                                try:
                                    r = on_delta(s)
                                    if asyncio.iscoroutine(r):
                                        await r
                                except Exception as e:
                                    log.warning("on_delta: %s", e)
                        elif code == CODE_FINISH:
                            finish = data
                        elif code == CODE_ERROR:
                            err_body = data
                        elif code in ("1", "2"):
                            other.append(ev)
                        else:
                            self.unknown_codes[ev["slot"] + code] += 1
                            other.append(ev)
                    elif t == "job_done":
                        last = ev
            except ArenaError as e:
                self._fail(e)
                raise
            # --- разбор ответа
            text = "".join(text_parts)
            if http_status == 200 and (text or other):
                self.counters["ok"] += 1
                self.last_success = time.time()
                self._speed_up()
                return {"ok": True, "text": text, "reasoning": "".join(reason_parts),
                        "parts": other, "finish": finish, "eval_id": eval_id,
                        "status": 200, "ms": (last or {}).get("ms"),
                        "bytes": (last or {}).get("bytes"),
                        "model_id": model_id, "modality": modality,
                        "session_id": eval_id,
                        "token_ms": (last or {}).get("tokenMs")}
            # --- ошибки
            body = err_body or (last or {}).get("raw_head") or (last or {}).get("err") or ""
            tok_err = (last or {}).get("tokenErr")
            if tok_err and not body:
                body = "reCAPTCHA: %s" % tok_err
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False)
            raw_body = body or ""
            if http_status == 403 and "recaptcha" in str(raw_body).lower():
                self.counters["recaptcha_failed"] += 1
                if self.cfg.V2_ENABLED and not attempt_v2:
                    log.warning("recaptcha v3 отклонена → эскалация к v2")
                    attempt_v2 = True
                    continue
                self.recaptcha_streak += 1
                pen = self._slow_down("recaptcha 403, неудач подряд: %d"
                                      % self.recaptcha_streak,
                                      self.cfg.RECAPTCHA_PENALTY, escalate=True)
                raise ArenaError(403, "recaptcha",
                                 "reCAPTCHA отклонила запрос (аккаунт временно помечен; "
                                 "шлюз ушёл в кулдаун на %d с): %s" % (pen, raw_body),
                                 retry_after=pen)
            if http_status == 429 and "prompt failed" in str(raw_body).lower():
                self.counters["prompt_failed"] += 1
                self._slow_down("429 prompt failed")
                if self.cfg.V2_ENABLED and not attempt_v2:
                    log.warning("429 prompt failed → эскалация к v2")
                    attempt_v2 = True
                    continue
                raise ArenaError(429, "prompt_failed",
                                 "арена отклонила промпт (prompt failed)", upstream=raw_body)
            if http_status == 429:
                self.counters["rate_limited"] += 1
                self._slow_down("429 от арены")
                raise ArenaError(429, "rate_limited",
                                 "лимит арены: %s" % (raw_body or "Too Many Requests"),
                                 retry_after=int(max(self.cooldown_until - time.time(), 60)))
            if http_status in (None, 0):
                self.counters["transport_error"] += 1
                raise ArenaError(502, "transport",
                                 "нет ответа от арены: %s" % (raw_body or "unknown"))
            self.counters["upstream_error"] += 1
            raise ArenaError(http_status if 400 <= http_status < 600 else 502,
                             "upstream", "арена ответила %s: %s" % (http_status, raw_body),
                             upstream=raw_body)
        raise ArenaError(502, "exhausted", "исчерпаны попытки")

    def _fail(self, e):
        self.counters["errors"] += 1
        self.last_error = {"at": time.strftime("%H:%M:%S"), "code": getattr(e, "code", "?"),
                           "message": str(getattr(e, "message", e))[:200]}
        self.errors.append(self.last_error)
        log.warning("ошибка: %s", self.last_error)

    # ------------------------------------------------------------ служебное
    async def fetch_json(self, method, path, body=None):
        raw = await self.js("window.__agwFetch(%s, %s, %s)" % (
            json.dumps(method), json.dumps(path), json.dumps(body)), timeout=60)
        try:
            return json.loads(raw)
        except Exception:
            return {"status": 0, "body": str(raw)}

    async def server_action(self, name, args, path=None):
        aid = self.actions.get(name)
        if not aid:
            return {"status": 0, "body": "неизвестное действие %s" % name}
        raw = await self.js("window.__agwAction(%s, %s, %s)" % (
            json.dumps(aid), json.dumps(args), json.dumps(path)), timeout=60)
        try:
            return json.loads(raw)
        except Exception:
            return {"status": 0, "body": str(raw)[:300]}

    async def cleanup(self, eval_id, kind="evaluation"):
        """Закрыть чат арены после ответа: удалить или архивировать."""
        if not self.cfg.CLEANUP or self.cfg.CLEANUP_MODE == "none" or not eval_id:
            return {"skipped": True}
        if self.cfg.CLEANUP_MODE == "archive":
            if kind == "evaluation":
                r = await self.server_action("archiveEvaluationSession", [eval_id])
            else:
                r = await self.fetch_json("POST", "/api/chat/%s/archive" % eval_id)
        else:
            if kind == "evaluation":
                r = await self.server_action("deleteEvaluationSession", [eval_id])
            else:
                r = await self.fetch_json("DELETE", "/api/chat/%s" % eval_id)
        ok = isinstance(r, dict) and r.get("status") in (200, 204)
        self.counters["cleanup_ok" if ok else "cleanup_fail"] += 1
        if not ok:
            log.warning("cleanup %s не удался: %s", eval_id, str(r)[:200])
        return r

    async def health(self, deep=False):
        h = {"ok": False, "tab": self.target_id, "page": self.page_url,
             "tab_age_s": int(time.time() - self.tab_created_at) if self.tab_created_at else None,
             "uptime_s": int(time.time() - self.started_at),
             "cooldown_remaining_s": max(0, int(self.cooldown_until - time.time())),
             "adaptive_interval_s": int(self.interval),
             "recaptcha_streak": self.recaptcha_streak,
             "paused": self.cooldown_until > time.time(),
             "queue": {"concurrency": self.cfg.MAX_CONCURRENCY,
                       "hits_in_window": len(self._hits),
                       "window_s": int(self.cfg.BURST_WINDOW),
                       "limit": self.cfg.BURST_LIMIT,
                       "min_interval_s": self.cfg.MIN_INTERVAL,
                       "current_interval_s": int(self.interval)},
             "counters": dict(self.counters),
             "unknown_stream_codes": dict(self.unknown_codes),
             "last_success": time.strftime("%H:%M:%S", time.localtime(self.last_success))
             if self.last_success else None,
             "last_error": self.last_error,
             "recent_errors": list(self.errors)[-5:]}
        if not deep:
            h["ok"] = bool(self.target_id) and self.cooldown_until <= time.time()
            return h
        try:
            await self.ensure_tab()
            ping = await self.js("1+1", timeout=15)
            rc = await self.js("""(async () => {
              try {
                await new Promise((res, rej) => { grecaptcha.enterprise.ready(res);
                  setTimeout(()=>rej(new Error('ready timeout')), 8000); });
                const t = await grecaptcha.enterprise.execute(%s, {action: %s});
                return JSON.stringify({ok: true, len: (t||'').length});
              } catch (e) { return JSON.stringify({ok: false, err: String(e)}); }
            })()""" % (json.dumps(self.cfg.RECAPTCHA_V3_SITEKEY),
                       json.dumps(self.cfg.RECAPTCHA_ACTION)), timeout=40)
            h["ping"] = ping
            h["recaptcha"] = json.loads(rc or "{}")
            h["ok"] = bool(ping == 2 and h["recaptcha"].get("ok"))
        except Exception as e:
            h["ok"] = False
            h["error"] = str(e)[:300]
        return h

    async def humanize(self):
        """Лёгкая имитация присутствия человека: движения мыши + небольшой скролл.

        Только mouseMoved/scroll — никаких кликов и клавиш, чтобы не задеть UI.
        """
        if not self.cfg.HUMANIZE:
            return
        try:
            import random
            pts = await self.js("""(() => {
              try { window.scrollBy({top: Math.round(Math.random()*80 - 40), behavior: 'smooth'}); } catch (e) {}
              const w = window.innerWidth || 1280, h = window.innerHeight || 800;
              const out = [];
              let x = w * (0.3 + Math.random() * 0.4), y = h * (0.3 + Math.random() * 0.4);
              for (let i = 0; i < 5; i++) {
                x += (Math.random() - 0.5) * 220; y += (Math.random() - 0.5) * 160;
                out.push([Math.max(20, Math.min(w - 20, x | 0)),
                          Math.max(20, Math.min(h - 20, y | 0))]);
              }
              return JSON.stringify(out);
            })()""", timeout=20)
            for x, y in json.loads(pts or "[]"):
                await self.tab.cmd("Input.dispatchMouseEvent",
                                   {"type": "mouseMoved", "x": int(x), "y": int(y)})
                await asyncio.sleep(0.12 + random.random() * 0.25)
            self.counters["humanized"] += 1
        except Exception as e:
            log.debug("humanize: %s", e)

    async def watchdog(self):
        """Фоновая проверка: вкладка жива, страница отвечает, recaptcha на месте."""
        last_human = 0.0
        while True:
            try:
                await asyncio.sleep(self.cfg.HEALTH_INTERVAL)
                if (self.cfg.HUMANIZE and self.tab and not self.tab.closed
                        and time.time() - last_human > self.cfg.HUMANIZE_EVERY):
                    last_human = time.time()
                    await self.humanize()
                try:
                    targets = {t.get("id"): t for t in self._cdp_targets()}
                except Exception as e:
                    log.error("CDP недоступен: %s", e)
                    self.counters["cdp_down"] += 1
                    continue
                if self.target_id not in targets:
                    log.warning("вкладка пропала — открываю новую")
                    self.counters["tab_lost"] += 1
                    self.tab = None
                    await self.ensure_tab(force=True)
                    continue
                if self.tab is None or self.tab.closed:
                    await self.ensure_tab()
                    continue
                url = str(targets[self.target_id].get("url") or "")
                if not url.startswith(ORIGIN):
                    log.warning("вкладка уехала на %s — возвращаю", url[:80])
                    self.counters["tab_navigated"] += 1
                    await self.ensure_tab(force=True)
                    continue
                try:
                    alive = await self.js("!!(window.__agw && window.grecaptcha && "
                                          "grecaptcha.enterprise)", timeout=15)
                except Exception:
                    alive = False
                if not alive:
                    log.warning("страница не отвечает/нет recaptcha — переустанавливаю")
                    self.counters["page_reset"] += 1
                    await self.ensure_tab(force=True)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("watchdog: %s", e)
                await asyncio.sleep(5)
