"""Конфигурация OpenAI-совместимого шлюза к arena.ai."""
import os

def _b(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)

# --- сеть
HOST = os.environ.get("ARENA_GW_HOST", "0.0.0.0")
PORT = _i("ARENA_GW_PORT", 8791)

# --- браузер (CDP)
CDP = os.environ.get("ARENA_GW_CDP", "http://127.0.0.1:9222")
ORIGIN = "https://arena.ai"
DIRECT_URL = ORIGIN + "/text/direct"
PAGE_WAIT = _f("ARENA_GW_PAGE_WAIT", 12)

# --- reCAPTCHA
RECAPTCHA_V3_SITEKEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
RECAPTCHA_V2_SITEKEY = "6Le3_cYsAAAAAGwWOK2RLDgNI15Bh8C0yLBOL1yL"
RECAPTCHA_ACTION = "chat_submit"
V2_ENABLED = _b("ARENA_GW_V2", False)   # v2 эскалирует в картинный челлендж — человеком решается
V2_TIMEOUT = _f("ARENA_GW_V2_TIMEOUT", 75)

# --- темп и лимиты (анти-кулдаун)
MIN_INTERVAL = _f("ARENA_GW_MIN_INTERVAL", 15)      # секунд между запросами к арене
BURST_LIMIT = _i("ARENA_GW_BURST_LIMIT", 10)        # не более N запросов…
BURST_WINDOW = _f("ARENA_GW_BURST_WINDOW", 300)     # …за M секунд
MAX_CONCURRENCY = _i("ARENA_GW_CONCURRENCY", 1)
COOLDOWN_MAX_WAIT = _f("ARENA_GW_COOLDOWN_WAIT", 90)  # сколько ждём кулдаун, прежде чем отдать 503

# --- вотчдоги
TOKEN_TIMEOUT = _f("ARENA_GW_TOKEN_TIMEOUT", 20)
FIRST_BYTE_TIMEOUT = _f("ARENA_GW_FIRST_BYTE", 60)
STREAM_IDLE_TIMEOUT = _f("ARENA_GW_IDLE", 60)
TOTAL_TIMEOUT = _f("ARENA_GW_TOTAL", 300)
POLL_INTERVAL = _f("ARENA_GW_POLL", 0.25)
MAX_STREAM_BYTES = _i("ARENA_GW_MAX_BYTES", 8_000_000)
HEALTH_INTERVAL = _f("ARENA_GW_HEALTH", 30)
# «очеловечивание» вкладки: лёгкие движения мыши/скролл повышают оценку reCAPTCHA
HUMANIZE = _b("ARENA_GW_HUMANIZE", True)
HUMANIZE_EVERY = _f("ARENA_GW_HUMANIZE_EVERY", 120)

# --- ретраи
RETRIES_RECAPTCHA = _i("ARENA_GW_RETRY_RECAPTCHA", 0)   # ретраи в flagged-состоянии продлевают штраф
RETRIES_PROMPT_FAILED = _i("ARENA_GW_RETRY_PROMPT", 1)

# адаптивный темп: после отказа reCAPTCHA/лимита интервал растёт, после успеха — падает
MAX_INTERVAL = _f("ARENA_GW_MAX_INTERVAL", 900)
RECAPTCHA_PENALTY = _f("ARENA_GW_PENALTY", 1200)   # штрафной кулдаун после 403 recaptcha
# каждая следующая неудача подряд удваивает штраф (флаг аккаунта держится дольше,
# чем 20 мин): 1200 → 2400 → 4800 → PENALTY_MAX
PENALTY_ESCALATE = _b("ARENA_GW_PENALTY_ESCALATE", True)
PENALTY_MAX = _f("ARENA_GW_PENALTY_MAX", 7200)
# модалка «Security Verification» (флаг аккаунта, нужен человек) — автопауза
BLOCK_PAUSE = _f("ARENA_GW_BLOCK_PAUSE", 7200)
# сколько отказов reCAPTCHA подряд считать флагом аккаунта (а не сбоем темпа)
BLOCK_STREAK = int(_f("ARENA_GW_BLOCK_STREAK", 2))
BACKOFF_FACTOR = _f("ARENA_GW_BACKOFF", 2.0)
RECOVER_FACTOR = _f("ARENA_GW_RECOVER", 0.9)

# --- жизненный цикл чата
CLEANUP = _b("ARENA_GW_CLEANUP", True)
CLEANUP_MODE = os.environ.get("ARENA_GW_CLEANUP_MODE", "delete")   # delete | archive | none

# --- данные
DATA_DIR = os.environ.get("ARENA_GW_DATA", "/opt/orchestrator/data/arena")
CATALOG = os.path.join(DATA_DIR, "models_catalog.json")
VERIFIED = os.path.join(DATA_DIR, "direct_models_verified.json")
STATE = os.path.join(DATA_DIR, "gateway_state.json")
LOG_DIR = os.environ.get("ARENA_GW_LOGDIR", "/opt/orchestrator/logs")

# --- авторизация
TOKEN_FILES = [
    os.environ.get("ARENA_GW_TOKEN_FILE", ""),
    "/opt/orchestrator/.secrets/arena_gateway_token.txt",
    "/opt/orchestrator/.secrets/arena_service_token.txt",
]
AUTH_ENABLED = _b("ARENA_GW_AUTH", True)
ALLOW_LOCAL_NOAUTH = _b("ARENA_GW_LOCAL_NOAUTH", True)  # 127.0.0.1 без токена

DEFAULT_MODEL = os.environ.get("ARENA_GW_DEFAULT_MODEL", "")
DEFAULT_MODALITY = os.environ.get("ARENA_GW_DEFAULT_MODALITY", "chat")
TIMEZONE = os.environ.get("ARENA_GW_TZ", "Europe/Kiev")


def load_token():
    for p in TOKEN_FILES:
        if p and os.path.exists(p):
            try:
                t = open(p).read().strip()
                if t:
                    return t
            except OSError:
                pass
    return None
