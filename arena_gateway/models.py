"""Реестр моделей арены: разрешение имени клиента в modelAId + модальность."""
import json, os, re, time, unicodedata

MODALITIES = ("chat", "webdev", "search", "image", "video", "p2l", "audio", "auto")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_BIG = 9_007_199_254_740_991

# короткие псевдонимы → publicName (расширяется файлом data/arena/model_aliases.json)
ALIASES = {
    "claude": "claude-sonnet-4-5-20250929",
    "sonnet": "claude-sonnet-4-5-20250929",
    "claude-sonnet": "claude-sonnet-4-5-20250929",
    "claude-sonnet-4.5": "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "haiku": "claude-haiku-4-5-20251001",
    "max": "Max",
    "flux": "flux-2-pro",
    "gemini": "gemini-3.1-pro-preview",
    "gpt": "gpt-5.2-high",
    "grok": "grok-4.5-search",
}


def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower().strip()
    s = re.sub(r"[\s_.]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


class Model:
    __slots__ = ("id", "public_name", "name", "provider", "organization",
                 "selectable", "modalities", "ranks", "verified", "verified_at",
                 "caps_in", "caps_out")

    def __init__(self, raw):
        self.id = raw.get("id")
        self.public_name = raw.get("publicName") or raw.get("displayName") or raw.get("name") or ""
        self.name = raw.get("name") or ""
        self.provider = raw.get("provider") or ""
        self.organization = raw.get("organization") or ""
        self.selectable = bool(raw.get("userSelectable"))
        ranks = raw.get("rankByModality") or {}
        self.ranks = {k: v for k, v in ranks.items() if isinstance(v, (int, float)) and v < _BIG}
        self.modalities = sorted(self.ranks.keys())
        caps = raw.get("capabilities") or {}
        self.caps_in = (caps.get("inputCapabilities") or {})
        self.caps_out = (caps.get("outputCapabilities") or {})
        self.verified = False
        self.verified_at = None

    @property
    def best_rank(self):
        return min(self.ranks.values()) if self.ranks else _BIG

    def supports(self, modality):
        if not modality or modality in ("auto", "chat"):
            return True
        return modality in self.ranks

    def to_openai(self):
        return {
            "id": self.public_name or self.id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": self.organization or self.provider or "arena",
            "arena_id": self.id,
            "arena_provider": self.provider,
            "arena_modalities": self.modalities,
            "arena_selectable": self.selectable,
            "arena_verified": self.verified,
        }


class Registry:
    def __init__(self, catalog_path, verified_path=None, aliases_path=None):
        self.catalog_path = catalog_path
        self.verified_path = verified_path
        self.aliases = dict(ALIASES)
        if aliases_path and os.path.exists(aliases_path):
            try:
                self.aliases.update({str(k).lower(): v
                                     for k, v in json.load(open(aliases_path)).items()})
            except Exception:
                pass
        self.models = []
        self.by_id = {}
        self.by_norm = {}
        self.load()

    # ------------------------------------------------------------------ load
    def load(self):
        raw = json.load(open(self.catalog_path))
        items = raw["models"] if isinstance(raw, dict) and "models" in raw else raw
        self.models = [Model(m) for m in items if m.get("id")]
        self.by_id = {m.id: m for m in self.models}
        self.by_norm = {}
        for m in self.models:
            for key in (m.public_name, m.name, m.id):
                k = _norm(key)
                if k and k not in self.by_norm:
                    self.by_norm[k] = m
        self.load_verified()
        return len(self.models)

    def load_verified(self):
        if not self.verified_path or not os.path.exists(self.verified_path):
            return
        try:
            d = json.load(open(self.verified_path))
        except Exception:
            return
        recs = d.get("models") if isinstance(d, dict) else d
        if isinstance(recs, dict):
            for mid, rec in recs.items():
                m = self.by_id.get(mid)
                if m and (rec is True or (isinstance(rec, dict) and rec.get("ok"))):
                    m.verified = True
                    m.verified_at = rec.get("at") if isinstance(rec, dict) else None

    def mark_verified(self, model_id, ok=True):
        m = self.by_id.get(model_id)
        if not m:
            return
        m.verified = ok
        m.verified_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # -------------------------------------------------------------- resolving
    def _candidates(self, modality=None):
        out = [m for m in self.models if m.selectable]
        if modality and modality not in ("auto", "chat"):
            out = [m for m in out if modality in m.ranks]
        return out

    def resolve(self, query, modality=None):
        """→ (Model|None, modality, error|None). Поддерживает суффикс ':search' и т.п."""
        q = (query or "").strip()
        if not q:
            return None, modality or "chat", "пустое имя модели"
        # суффикс модальности
        if ":" in q:
            base, suf = q.rsplit(":", 1)
            if suf.strip().lower() in MODALITIES:
                modality = suf.strip().lower()
                q = base.strip()
        modality = (modality or "chat").lower()

        # 1) UUID
        if _UUID_RE.match(q):
            m = self.by_id.get(q)
            if m:
                return m, modality, None
            return None, modality, "модель с id %s не найдена в каталоге" % q

        nq = _norm(q)
        pool = self._candidates(modality) or self.models

        # 2) точное совпадение
        for m in pool:
            if _norm(m.public_name) == nq or _norm(m.name) == nq:
                return m, modality, None
        m = self.by_norm.get(nq)
        if m:
            return m, modality, None

        # 3) псевдонимы
        alias = self.aliases.get(q.lower()) or self.aliases.get(nq)
        if alias:
            for m in pool:
                if _norm(m.public_name) == _norm(alias):
                    return m, modality, None

        # 4) частичное совпадение: сначала verified, потом ранг
        def score(m):
            np_, nm = _norm(m.public_name), _norm(m.name)
            s = 0
            if np_.startswith(nq) or nm.startswith(nq):
                s = 100
            elif nq in np_ or nq in nm:
                s = 60
            elif np_.startswith(nq.split("-")[0]) and len(nq.split("-")[0]) > 3:
                s = 20
            if not s:
                return None
            return (s + (30 if m.verified else 0), -m.best_rank)

        scored = [(score(m), m) for m in pool]
        scored = [(s, m) for s, m in scored if s]
        if scored:
            scored.sort(key=lambda x: (-x[0][0], -x[0][1]))
            best = scored[0][1]
            return best, modality, None

        near = ", ".join(sorted({m.public_name for m in pool
                                 if _norm(m.public_name).startswith(nq[:4])})[:5])
        return None, modality, ("модель '%s' не найдена%s" %
                                (q, (" (похожие: %s)" % near) if near else ""))

    def default_model(self, modality="chat", preferred=None):
        if preferred:
            m, mod, err = self.resolve(preferred, modality)
            if m:
                return m
        ver = [m for m in self._candidates(modality) if m.verified]
        if ver:
            ver.sort(key=lambda m: m.best_rank)
            return ver[0]
        pool = self._candidates(modality)
        pool.sort(key=lambda m: m.best_rank)
        return pool[0] if pool else None

    # ------------------------------------------------------------- listing
    def openai_models(self, only_selectable=True, only_verified=False):
        out = []
        for m in self.models:
            if only_selectable and not m.selectable:
                continue
            if only_verified and not m.verified:
                continue
            out.append(m.to_openai())
        out.sort(key=lambda d: (not d["arena_verified"], d["owned_by"], d["id"]))
        return out

    def stats(self):
        return {
            "total": len(self.models),
            "selectable": sum(1 for m in self.models if m.selectable),
            "verified": sum(1 for m in self.models if m.verified),
            "by_modality": {mod: sum(1 for m in self.models if mod in m.ranks)
                            for mod in ("chat", "webdev", "search", "image", "video")},
        }
