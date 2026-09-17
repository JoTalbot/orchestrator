#!/usr/bin/env python3
"""
install_integrations.py — подключает шлюз арены к балансировщику AIOS и Hermes.

  1. /opt/aios/llm/llm_balancer.py  — провайдеры arena-* (маркеры ARENA-GATEWAY-*)
  2. /etc/octopus/secrets.env       — ARENA_GATEWAY_KEY=<токен шлюза>
  3. /opt/hermes/config/models/hermes-models.yaml — тир arena
Запуск:  python3 install_integrations.py [--dry] [--max N]
"""
import json, os, re, shutil, sys, time

DRY = "--dry" in sys.argv
MAXP = 4
if "--max" in sys.argv:
    MAXP = int(sys.argv[sys.argv.index("--max") + 1])

BALANCER = "/opt/aios/llm/llm_balancer.py"
SECRETS = "/etc/octopus/secrets.env"
HERMES = "/opt/hermes/config/models/hermes-models.yaml"
VERIFIED = "/opt/orchestrator/data/arena/direct_models_verified.json"
TOKEN_FILE = "/opt/orchestrator/.secrets/arena_gateway_token.txt"
BEGIN, END = "# ARENA-GATEWAY-BEGIN", "# ARENA-GATEWAY-END"


def backup(path):
    if os.path.exists(path):
        dst = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, dst)
        return dst
    return None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]


def verified_models():
    if not os.path.exists(VERIFIED):
        return []
    d = json.load(open(VERIFIED)).get("models", {})
    out = [v for v in d.values() if v.get("ok")]
    out.sort(key=lambda v: v.get("ms") or 99999)
    return out


def main():
    ok = verified_models()
    print("проверенных моделей:", len(ok))
    picks = ok[:MAXP]
    if not picks:
        print("НЕТ проверенных моделей — сначала запустите пробник:")
        print('  curl -X POST http://127.0.0.1:8791/v1/arena/probe -d \'{"limit":8}\'')
        return 1
    providers = []
    for i, v in enumerate(picks):
        name = v.get("name") or v["id"]
        providers.append(("arena-%s" % slug(name), name, "arena", 1 + i))
        print("  %-40s → %s" % (name, providers[-1][0]))

    # ---------------------------------------------------------- 1. балансировщик
    src = open(BALANCER).read()
    table = "ARENA_PROVIDERS = [\n" + "".join(
        '    ("%s", "%s", "%s", %d),\n' % p for p in providers) + "]\n\n\n"
    block = """        %s — Arena AI (direct mode) через локальный шлюз 127.0.0.1:8791
        arena_keys = self._extract_keys("ARENA_GATEWAY_KEY")
        if arena_keys:
            arena_url = os.environ.get("ARENA_GATEWAY_URL", "http://127.0.0.1:8791/v1")
            for aname, amodel, atier, aweight in ARENA_PROVIDERS:
                self.providers.append(OpenAICompatibleCloudProvider(
                    aname, arena_url, amodel, arena_keys, tier=atier,
                    weight=aweight, timeout=150.0))
        %s
""" % (BEGIN, END)

    if BEGIN in src:
        src = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n",
                     block.lstrip(), src, flags=re.S)
        src = re.sub(r"ARENA_PROVIDERS = \[.*?\]\n\n\n", table, src, flags=re.S)
        print("балансировщик: блок обновлён")
    else:
        anchor = "class OpenAICompatibleCloudProvider(MultiKeyRotatingProvider):"
        if anchor not in src:
            print("не найден якорь класса в балансировщике"); return 1
        src = src.replace(anchor, table + anchor, 1)
        # вставляем блок провайдеров перед "# 9. Liza RPA"
        m = re.search(r"\n( *)# 9\. Liza RPA", src)
        if not m:
            m = re.search(r"\n( *)# 1[01]\. ", src)
        if not m:
            print("не найдено место вставки в _init_providers"); return 1
        pos = m.start() + 1
        src = src[:pos] + block + src[pos:]
        print("балансировщик: блок добавлен")
    if not DRY:
        backup(BALANCER)
        open(BALANCER, "w").write(src)
        compile(src, BALANCER, "exec")
        print("  записано и скомпилировано:", BALANCER)

    # ---------------------------------------------------------- 2. секрет
    token = open(TOKEN_FILE).read().strip()
    line = "ARENA_GATEWAY_KEY=%s\nARENA_GATEWAY_URL=http://127.0.0.1:8791/v1\n" % token
    cur = open(SECRETS).read() if os.path.exists(SECRETS) else ""
    if "ARENA_GATEWAY_KEY=" in cur:
        cur = re.sub(r"ARENA_GATEWAY_KEY=.*\n", "ARENA_GATEWAY_KEY=%s\n" % token, cur)
        print("секрет: ключ обновлён")
    else:
        cur = cur.rstrip("\n") + "\n" + line if cur else line
        print("секрет: ключ добавлен")
    if not DRY:
        backup(SECRETS)
        open(SECRETS, "w").write(cur)
        os.chmod(SECRETS, 0o600)

    # ---------------------------------------------------------- 3. hermes yaml
    y = open(HERMES).read() if os.path.exists(HERMES) else ""
    if "hermes-arena:" in y:
        print("hermes: тир arena уже есть")
    else:
        entry = ("  hermes-arena: # arena.ai direct mode: %s\n"
                 "    tier: arena\n"
                 "    use_for: [deep-research, second-opinion, frontier-chat]\n"
                 % ", ".join(p[1] for p in providers))
        m = re.search(r"\nfallback_chain:", y)
        if m:
            y = y[:m.start() + 1] + entry + y[m.start() + 1:]
        else:
            y = y.rstrip("\n") + "\n" + entry
        if not DRY:
            backup(HERMES)
            open(HERMES, "w").write(y)
        print("hermes: тир arena добавлен")

    print("\nПерезапуск:")
    print("  sudo systemctl restart octopus-aios.service hermes-shim.service")
    return 0


sys.exit(main())
