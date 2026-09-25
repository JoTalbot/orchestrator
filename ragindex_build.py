#!/usr/bin/env python3
"""
ragindex — полнотекстовая память по истории чатов (ChatGPT + Arena Agent Mode).

Только стандартная библиотека. Спроектировано под машину с малым объёмом свободной
памяти: файлы обрабатываются по одному, ничего не накапливается в RAM.

Источники:
  data/light/*.json         — ChatGPT, список сообщений [{role, text, create_time}, ...]
  data/arena/light/*.json   — Arena, {title, createdAt, messages: [{role, parts: [...]}]}
  (если light/ нет — берётся сырой chats/ того же формата)

Команды:
  build   --root /opt/orchestrator      построить/обновить индекс (инкрементально по mtime)
  query   "текст" --source arena -n 10  поиск
  context --chat <id> --seq <n> [-k 2]  сообщение + соседние + какие инструменты использовались
  stats                                 сводка по индексу

Пример:
  python3 build.py build --root /opt/orchestrator
  python3 build.py query "экспорт чатов chatgpt" -n 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

DB_NAME = "rag_index.db"
BATCH = 500
SNIPPET_CHARS = 400
MAX_TEXT = 200_000  # защита от аномально больших сообщений

# части, которые несут полезный текст для поиска
TEXT_PARTS = {"text", "reasoning"}
TOOL_PREFIX = "tool-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER, kind TEXT, indexed_at REAL
);
CREATE TABLE IF NOT EXISTS chats(
  id TEXT NOT NULL, source TEXT NOT NULL, title TEXT, created_at TEXT, updated_at TEXT,
  message_count INTEGER, product_mode TEXT, session_type TEXT,
  PRIMARY KEY (id, source)
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, chat_id TEXT, source TEXT, seq INTEGER,
  role TEXT, text TEXT, ts TEXT, tools TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(source, chat_id, seq);
-- FTS5 хранит собственный текст (без external content).
-- Причина: в external-content таблице внутренний алиас контент-таблицы конфликтует
-- с алиасами в триггерах, из-за чего подзапрос к chats ломал индекс
-- (проверено: sqlite3.OperationalError: no such column: T.title).
-- Цена: +~200 МБ на диске при 79 ГБ свободных — приемлемо.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, role, title, tokenize='unicode61'
);
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def now() -> float:
    return datetime.now(timezone.utc).timestamp()


def iso_from_epoch(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " …"


# --------------------------------------------------------------------------- #
#  Вымарывание секретов
#
#  В истории чатов реально лежат закрытые ключи и токены: пользователь вставлял их
#  в сообщения, и они же попали в аргументы инструментов (проверено на сервере:
#  сообщение начинается с "ssh -i ... -----BEGIN OPENSSH PRIVATE KEY-----",
#  а в tools.input виден ключ, записанный через write_file).
#  Индекс не должен становиться их концентратом.
# --------------------------------------------------------------------------- #

SECRET_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Всё от -----BEGIN <ТИП> до конца строки — в том числе когда заголовок
    # ОБОРВАН, без закрывающих тире и без слова KEY.
    #
    # Обязательно: названия чатов в экспорте обрезаны (проверено: title = 101 символ),
    # поэтому обрывается не только -----END, но и сам заголовок — в индексе
    # встречалось "-----BEGIN  OPENSSH  PRIVATE …". Прежний паттерн требовал
    # полного "PRIVATE KEY-----" и такое не ловил: после пересборки из сырого
    # экспорта нашлось 163 заголовка с незакрытым -----BEGIN.
    #
    # Отрицательный просмотр (?!\() нужен, чтобы не вымарывать исходники:
    # в истории чатов лежит код детектора секретов, и его регулярки
    # ("-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----") выглядят как заголовки.
    #
    # Один общий паттерн вместо отдельных для PRIVATE KEY / сертификатов / PGP:
    # проверено, что он покрывает все три и при этом не трогает ни исходники,
    # ни обычный текст. Держать несколько частных смысла нет — удаление любого
    # из них переставало ловиться тестами, то есть дубли маскировали регресс.
    re.compile(r"-----BEGIN (?!\()[A-Z0-9 ]{2,}[^\n]*[\s\S]*"),
    re.compile(r"\b(?:sk|pk|ghp|gho|ghs|github_pat|xox[baprs]|AIza)[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b(?=.{0,40}(?:token|secret|key))", re.IGNORECASE),
    # Любой непрерывный base64 длиной >=100 символов.
    # Обязательно: в истории нашлись cloudflared-токены туннеля и JWE-заголовки
    # (проверено: "cloudflared tunnel run --token eyJhIjoi..." — 248 символов,
    # а полных JWT из трёх сегментов в индексе 0, поэтому JWT-паттерн их не ловил).
    # Обычная проза и исходники таких прогонов не содержат, так что порог безопасен.
    re.compile(r"[A-Za-z0-9+/]{100,}={0,2}"),
]
REDACTED = "[КЛЮЧ_УДАЛЁН]"


def redact(text: str) -> tuple[str, int]:
    """Заменить секреты заглушкой. Возвращает (текст, число замен)."""
    if not text:
        return text, 0
    hits = 0
    for pat in SECRET_PATTERNS:
        text, n = pat.subn(REDACTED, text)
        hits += n
    return text, hits


# --------------------------------------------------------------------------- #
#  Парсеры источников
# --------------------------------------------------------------------------- #


def parse_chatgpt_light(path: str):
    """data/light/*.json -> (chat_meta, [message_dict, ...])"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    chat_id = os.path.splitext(os.path.basename(path))[0]
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        raw = data["messages"]
        meta = {"title": data.get("title"), "created_at": data.get("createdAt"),
                "updated_at": data.get("updatedAt")}
    elif isinstance(data, list):
        raw = data
        meta = {"title": None, "created_at": None, "updated_at": None}
    else:
        return None, []

    msgs = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            continue
        text = m.get("text") or m.get("content") or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        msgs.append(
            {
                "seq": i,
                "role": str(m.get("role") or "?"),
                "text": text[:MAX_TEXT],
                "ts": iso_from_epoch(m.get("create_time")),
                "tools": "",
            }
        )
    return {"id": chat_id, **meta}, msgs


# content_type, которые несут полезный текст в сыром экспорте ChatGPT
RAW_TEXT_TYPES = {"text", "multimodal_text", "execution_output", "system_error"}


def _plugin_namespace(meta: dict) -> str:
    """Имя инструмента из metadata.invoked_plugin.namespace.

    В сыром экспорте ChatGPT message.name всегда пуст (проверено на 120 файлах),
    зато у tool-сообщений есть metadata.invoked_plugin — у 1302 из 2717 он вида
    {"type": "remote", "namespace": "api_autosklo_org_ua__jit_plugin"}.
    """
    plugin = meta.get("invoked_plugin")
    if not isinstance(plugin, dict):
        return ""
    ns = plugin.get("namespace")
    return str(ns) if isinstance(ns, str) and ns else ""


def _conversation_order(mapping: dict, current_node):
    """Линейная цепочка сообщения от корня к current_node.

    Обходить mapping в порядке ключей нельзя: там лежат и отброшенные ветки.
    Проверено: файл с 1356 узлами даёт 1255 узлов в основной цепочке.
    """
    order, cur, seen = [], current_node, set()
    while isinstance(cur, str) and cur in mapping and cur not in seen:
        seen.add(cur)
        order.append(cur)
        node = mapping.get(cur)
        cur = node.get("parent") if isinstance(node, dict) else None
    order.reverse()
    return order


def parse_chatgpt_raw(path: str):
    """data/chats/*.json — сырой экспорт ChatGPT.

    Богаче light/, из которого он делается: в light остаются только {role, text}
    и теряются рассуждения модели, полезные нагрузки вызовов инструментов,
    их результаты и имена плагинов. Замер на одном чате: light 514 сообщений,
    raw 1255 узлов в основной цепочке.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return None, []
    mapping = data.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return None, []

    chat_id = str(data.get("conversation_id")
                  or os.path.splitext(os.path.basename(path))[0])
    meta = {
        "id": chat_id,
        "title": data.get("title"),
        "created_at": iso_from_epoch(data.get("create_time")),
        "updated_at": iso_from_epoch(data.get("update_time")),
    }

    msgs = []
    for i, key in enumerate(_conversation_order(mapping, data.get("current_node"))):
        node = mapping.get(key)
        m = node.get("message") if isinstance(node, dict) else None
        if not isinstance(m, dict):
            continue
        content = m.get("content") if isinstance(m.get("content"), dict) else {}
        ctype = str(content.get("content_type") or "")
        md = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}

        texts, tools = [], []
        parts = content.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, str) and part.strip():
                    texts.append(part.strip())
        # У части content_type текста в parts нет вовсе, он лежит в content.text:
        # code (полезная нагрузка вызова инструмента, например {"prompt": ...}),
        # execution_output (результат), system_error. Проверено на экспорте:
        # у execution_output parts=None, поэтому чтение только parts теряло
        # такие сообщения целиком.
        if parts is None or ctype in ("code", "execution_output", "system_error"):
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        if ctype == "thoughts":
            for th in content.get("thoughts") or []:
                if not isinstance(th, dict):
                    continue
                for field in ("summary", "content"):
                    v = th.get(field)
                    if isinstance(v, str) and v.strip():
                        texts.append(v.strip())
        if ctype == "reasoning_recap":
            v = content.get("content")
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())

        ns = _plugin_namespace(md)
        if ns:
            tools.append(ns)
        name = m.get("name")
        if isinstance(name, str) and name:
            tools.append(name)

        if not texts and not tools:
            continue
        body = "\n".join(texts)[:MAX_TEXT]
        if tools:
            uniq = sorted(set(tools))
            body = (body + "\n" if body else "") + "[tools: " + " ".join(uniq) + "]"
        msgs.append({
            "seq": i,
            "role": str((m.get("author") or {}).get("role") or "?"),
            "text": body,
            "ts": iso_from_epoch(m.get("create_time")),
            "tools": ",".join(sorted(set(tools))),
        })
    meta["message_count"] = len(msgs)
    return meta, msgs


def parse_chatgpt_any(path: str):
    """Определяет формат ChatGPT-файла по содержимому.

    Нужно, чтобы один и тот же парсер работал и с data/light (плоский список),
    и с data/chats (сырой экспорт с mapping) — иначе при смене источника
    индекс молча соберётся пустым.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("mapping"), dict) and data["mapping"]:
        return parse_chatgpt_raw(path)
    return parse_chatgpt_light(path)


def parse_arena(path: str):
    """
    Arena: поддерживает ОБА формата, которые реально встречаются на диске.

      data/arena/light/*.json  — плоский: {role, text, tools: [{tool, state, input}]}
      data/arena/chats/*.json  — полный:  {role, parts: [{type, text}]}

    Раньше парсер знал только parts[] и потому на light/ молча возвращал 0 сообщений
    (проверено: файл с message_count=98 давал 0). Теперь определяем формат по факту.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return None, []
    chat_id = data.get("id") or os.path.splitext(os.path.basename(path))[0]
    meta = {
        "id": chat_id,
        "title": data.get("title"),
        "created_at": data.get("createdAt"),
        "updated_at": data.get("updatedAt"),
        "message_count": data.get("messageCount"),
        "product_mode": data.get("productMode"),
        "session_type": data.get("type"),
    }

    msgs = []
    for i, m in enumerate(data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        texts, tools = [], []

        flat_text = m.get("text")
        if isinstance(flat_text, str) and flat_text.strip():
            texts.append(flat_text.strip())          # формат light
        for part in m.get("parts") or []:            # формат chats
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "")
            if ptype in TEXT_PARTS:
                t = part.get("text")
                if isinstance(t, str) and t.strip():
                    texts.append(t.strip())
            elif ptype.startswith(TOOL_PREFIX):
                tools.append(ptype[len(TOOL_PREFIX):])
        for t in m.get("tools") or []:               # tools из light
            name = t.get("tool") if isinstance(t, dict) else t
            if isinstance(name, str) and name:
                tools.append(
                    name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name
                )

        if not texts and not tools:
            continue
        body = "\n".join(texts)[:MAX_TEXT]
        if tools:
            body = (body + "\n" if body else "") + "[tools: " + " ".join(sorted(set(tools))) + "]"
        msgs.append(
            {
                "seq": i,
                "role": str(m.get("role") or "?"),
                "text": body,
                "ts": str(m.get("createdAt") or data.get("createdAt") or "")[:16],
                "tools": ",".join(sorted(set(tools))),
            }
        )
    return meta, msgs


PARSERS = {
    # parse_chatgpt_any сам различает light/ и сырой chats/, поэтому смена
    # источника не требует менять парсер
    "chatgpt": parse_chatgpt_any,
    "arena": parse_arena,
}


def source_dirs(root: str, prefer: str = "light") -> list[tuple[str, str]]:
    """[(kind, dir)] — какие каталоги индексировать.

    prefer="light" (по умолчанию) — лёгкая проекция: быстрее и компактнее.
    prefer="raw" — сырой экспорт: в нём есть рассуждения модели, вызовы
    инструментов, их результаты и имена плагинов, которых в light нет вовсе.
    Замер: light 165 МБ против chats 469 МБ; arena light 35 МБ против chats 81 МБ.
    """
    order = {"light": (0, 1), "raw": (1, 0)}.get(prefer)
    if order is None:
        raise ValueError("prefer должен быть 'light' или 'raw', получено %r" % (prefer,))
    out = []
    for kind, light, raw in (
        ("chatgpt", "data/light", "data/chats"),
        ("arena", "data/arena/light", "data/arena/chats"),
    ):
        candidates = (light, raw) if order[0] == 0 else (raw, light)
        for rel in candidates:
            d = os.path.join(root, rel)
            if os.path.isdir(d) and any(f.endswith(".json") for f in os.listdir(d)):
                out.append((kind, d))
                break
    return out


# --------------------------------------------------------------------------- #
#  Индексация
# --------------------------------------------------------------------------- #


def build(root: str, db_path: str, force: bool = False, prefer: str = "light") -> int:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    indexed = {}
    if not force:
        for path, mtime, size in conn.execute("SELECT path, mtime, size FROM sources"):
            indexed[path] = (mtime, size)

    total_files = total_msgs = skipped = errors = 0
    redacted_msgs = redacted_hits = 0
    pending = []

    def flush():
        """Записать накопленные сообщения и синхронно наполнить FTS теми же rowid."""
        nonlocal pending
        if not pending:
            return
        conn.executemany(
            "INSERT INTO messages(chat_id, source, seq, role, text, ts, tools) "
            "VALUES (?,?,?,?,?,?,?)",
            [p[:7] for p in pending],
        )
        # executemany не возвращает надёжный lastrowid — берём фактический максимум
        last = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        first = last - len(pending) + 1
        # title для FTS берём из таблицы chats — там он уже вымаран.
        # Брать из pending нельзя: там лежит сырое название (проверено: в FTS
        # попадал закрытый ключ целиком, хотя chats.title был очищен).
        conn.executemany(
            "INSERT INTO messages_fts(rowid, text, role, title) VALUES (?,?,?,?)",
            [
                (
                    first + i,
                    p[4],
                    p[3],
                    conn.execute(
                        "SELECT title FROM chats WHERE id=? AND source=?", (p[0], p[1])
                    ).fetchone()[0]
                    or "",
                )
                for i, p in enumerate(pending)
            ],
        )
        pending = []
        conn.commit()

    def delete_chat(kind: str, chat_id: str) -> None:
        """Удалить сообщения чата из messages И из FTS (иначе поиск найдёт призраков)."""
        rowids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM messages WHERE source=? AND chat_id=?", (kind, chat_id)
            )
        ]
        if rowids:
            conn.executemany("DELETE FROM messages_fts WHERE rowid=?", [(r,) for r in rowids])
            conn.execute("DELETE FROM messages WHERE source=? AND chat_id=?", (kind, chat_id))

    for kind, directory in source_dirs(root, prefer=prefer):
        log(f"[{kind}] сканирую {directory}")
        for name in sorted(os.listdir(directory)):  # по одному файлу, без накопления
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            total_files += 1
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not force and indexed.get(path) == (st.st_mtime, st.st_size):
                skipped += 1
                continue
            try:
                meta, msgs = PARSERS[kind](path)
            except MemoryError:
                errors += 1
                log(f"  ПРОПУЩЕН (не хватило памяти) {name}: {st.st_size / 1e6:.1f} МБ")
                continue
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, RecursionError) as exc:
                errors += 1
                log(f"  ОШИБКА {name}: {exc}")
                continue
            if not meta:
                errors += 1
                continue
            # перестроение одного чата: удаляем старое из обеих таблиц
            delete_chat(kind, meta["id"])
            # название чата тоже индексируется и показывается в выдаче —
            # а в названиях реально встречаются закрытые ключи (проверено на сервере)
            safe_title, title_hits = redact(str(meta.get("title") or ""))
            redacted_hits += title_hits
            redacted_msgs += 1 if title_hits else 0
            conn.execute(
                "INSERT OR REPLACE INTO chats(id, source, title, created_at, updated_at,"
                " message_count, product_mode, session_type) VALUES (?,?,?,?,?,?,?,?)",
                (
                    meta["id"], kind, safe_title, meta.get("created_at"),
                    meta.get("updated_at"), meta.get("message_count"),
                    meta.get("product_mode"), meta.get("session_type"),
                ),
            )
            for m in msgs:
                if not m["text"].strip():
                    continue
                safe, hits = redact(m["text"])
                redacted_msgs += 1 if hits else 0
                redacted_hits += hits
                pending.append(
                    (
                        meta["id"], kind, m["seq"], m["role"], safe, m["ts"],
                        m["tools"], meta.get("title") or "",
                    )
                )
                total_msgs += 1
            conn.execute(
                "INSERT OR REPLACE INTO sources(path, mtime, size, kind, indexed_at) "
                "VALUES (?,?,?,?,?)",
                (path, st.st_mtime, st.st_size, kind, now()),
            )
            if len(pending) >= BATCH:
                flush()
        flush()

    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
    conn.commit()
    n_msgs = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    n_chats = conn.execute("SELECT count(*) FROM chats").fetchone()[0]
    log(
        f"готово: файлов {total_files} (пропущено без изменений {skipped}, ошибок {errors}), "
        f"добавлено сообщений {total_msgs}; в индексе {n_msgs} сообщений из {n_chats} чатов"
    )
    log(f"вымарано секретов: {redacted_hits} в {redacted_msgs} сообщениях")
    log(f"база: {db_path} ({os.path.getsize(db_path) / 1e6:.1f} МБ)")
    conn.close()
    return 0 if errors == 0 else 1


# --------------------------------------------------------------------------- #
#  Поиск
# --------------------------------------------------------------------------- #


def fts_query(text: str) -> str:
    """Преобразование запроса в FTS5.

    Слова в двойных кавычках становятся фразой: «"утечка вкладок" хром» ->
    '"утечка вкладок" OR "хром"'. Всё остальное режется на слова и соединяется
    через OR, как раньше — старые запросы работают identically.

    Кавычки внутри токена экранируются удвоением, поэтому синтаксис FTS5
    сломать вводом нельзя.
    """
    parts: list[str] = []
    for m in re.finditer(r'"([^"]+)"|([^\s"]+)', text or ""):
        phrase, word = m.group(1), m.group(2)
        tok = (phrase if phrase is not None else word).strip()
        if len(tok) <= 1 and phrase is None:
            continue          # одиночные буквы вне фраз не несут смысла
        if tok:
            parts.append('"' + tok.replace('"', '""') + '"')
    return " OR ".join(parts) if parts else '""'


# Понижение роли tool в ранжировании.
#
# Что измерено на боевом индексе (/opt/orchestrator/data/rag_index.db, 50954 сообщения):
#   * role=tool — 27219 сообщений (53% индекса), средняя длина 4341 символ
#     против 1488 у assistant и 563 у user;
#   * но в выдаче tool НЕ доминирует: на 10 типовых запросах доля tool в топ-5
#     составила 18% (9 из 50 позиций), а не 53%. То есть bm25 сам по себе
#     в среднем справляется — длина документа нормализуется.
#   * проблема точечная, а не системная: на запросе «redact секреты» топ-5 был
#     tool,tool,tool,tool,user, на «обучение lora» — tool,tool,assistant,tool,tool.
#     То есть на отдельных запросах вывод инструментов забивает всю выдачу.
#
# Поэтому понижение сделано настраиваемым, а не зашитым: 0.2 убирает tool из топ-5
# полностью (замер: доля tool 18% -> 0%), 1.0 возвращает прежнее поведение,
# include_tool_output=False в инструменте убирает его жёстко.
#
# Проверять нужно на реальном корпусе: на синтетике в несколько десятков
# документов bm25 ведёт себя иначе и выводы не переносятся (убедился на практике).
DEFAULT_ROLE_WEIGHTS = {"user": 1.0, "assistant": 1.0, "tool": 0.2}


def _role_weight_case(weights: dict | None, default: float = 1.0) -> str:
    """CASE-выражение для весов ролей. Значения приводятся к float, имена
    ролей экранируются — конкатенация сырых строк в SQL здесь недопустима."""
    w = dict(weights or DEFAULT_ROLE_WEIGHTS)
    # ВАЖНО: m.role, а не messages_fts.role.
    # В FTS5 оператор '=' над колонкой — это полнотекстовое сравнение (MATCH),
    # а не сравнение строк: на проверке role='assistant' возвращало 1 и для
    # строки с role='tool', из-за чего множители применялись наугад
    # (наблюдали score=-8.80 при bm25=-4.14, т.е. множитель 2.12 вместо 1.0).
    clauses = [
        "WHEN m.role = '%s' THEN %r" % (str(role).replace("'", "''"), float(mult))
        for role, mult in sorted(w.items())
    ]
    return "CASE " + " ".join(clauses) + (" ELSE %r END" % float(default))


def query(db_path: str, text: str, n: int = 10, source: str | None = None,
          role: str | None = None, role_weights: dict | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT m.id, m.chat_id, m.source, m.seq, m.role, m.ts, m.tools, c.title, "
        "       snippet(messages_fts, 0, '>>>', '<<<', ' … ', 24) AS snip, "
        "       bm25(messages_fts) * " + _role_weight_case(role_weights) + " AS score "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
        "LEFT JOIN chats c ON c.id = m.chat_id AND c.source = m.source "
        "WHERE messages_fts MATCH ?"
    )
    params: list = [fts_query(text)]
    if source:
        sql += " AND m.source = ?"
        params.append(source)
    if role:
        sql += " AND m.role = ?"
        params.append(role)
    sql += " ORDER BY score LIMIT ?"
    params.append(n)
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def context(db_path: str, chat_id: str, seq: int, k: int = 2,
            source: str | None = None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT m.seq, m.role, m.ts, m.tools, m.text, c.title "
        "FROM messages m LEFT JOIN chats c ON c.id=m.chat_id AND c.source=m.source "
        "WHERE m.chat_id = ? AND m.seq BETWEEN ? AND ?"
    )
    params: list = [chat_id, max(0, seq - k), seq + k]
    if source:
        sql += " AND m.source = ?"
        params.append(source)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY m.seq", params)]
    conn.close()
    return rows


def stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    out = {
        "db": db_path,
        "size_mb": round(os.path.getsize(db_path) / 1e6, 1),
        "messages": conn.execute("SELECT count(*) FROM messages").fetchone()[0],
        "chats": conn.execute("SELECT count(*) FROM chats").fetchone()[0],
        "by_source": dict(conn.execute("SELECT source, count(*) FROM messages GROUP BY source")),
        "by_role": dict(conn.execute("SELECT role, count(*) FROM messages GROUP BY role")),
        "date_min": conn.execute("SELECT min(ts) FROM messages WHERE ts<>''").fetchone()[0],
        "date_max": conn.execute("SELECT max(ts) FROM messages WHERE ts<>''").fetchone()[0],
    }
    out["top_tools"] = {}
    import collections

    cnt = collections.Counter()
    for (tools,) in conn.execute("SELECT tools FROM messages WHERE tools<>''"):
        for t in str(tools).split(","):
            if t:
                cnt[t] += 1
    out["top_tools"] = dict(cnt.most_common(10))
    conn.close()
    return out


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Память по истории чатов")
    ap.add_argument("cmd", choices=["build", "query", "context", "stats"])
    ap.add_argument("--prefer", choices=["light", "raw"], default="light",
                    help="light (по умолчанию) — компактная проекция; raw — сырой экспорт "
                         "с рассуждениями модели, вызовами инструментов и именами плагинов")
    ap.add_argument("--tool-weight", type=float, default=DEFAULT_ROLE_WEIGHTS["tool"],
                    help="вес роли tool в ранжировании, 0..1 (по умолчанию %(default)s; "
                         "1.0 отключает понижение)")
    ap.add_argument("text", nargs="?", help="строка поиска для query")
    ap.add_argument("--root", default="/opt/orchestrator")
    ap.add_argument("--db", default=None, help="путь к базе (по умолчанию <root>/data/rag_index.db)")
    ap.add_argument("--source", choices=["chatgpt", "arena"])
    ap.add_argument("--role", choices=["user", "assistant"])
    ap.add_argument("--chat", help="id чата для context")
    ap.add_argument("--seq", type=int, help="номер сообщения для context")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("-k", type=int, default=2, help="сколько соседей показывать в context")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force", action="store_true", help="перестроить индекс с нуля")
    args = ap.parse_args(argv)

    db = args.db or os.path.join(args.root, "data", DB_NAME)

    if args.cmd == "build":
        os.makedirs(os.path.dirname(db), exist_ok=True)
        return build(args.root, db, force=args.force, prefer=args.prefer)

    if not os.path.isfile(db):
        print(f"нет индекса: {db}. Сначала: build --root {args.root}", file=sys.stderr)
        return 2

    if args.cmd == "stats":
        print(json.dumps(stats(db), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "query":
        if not args.text:
            print("нужна строка поиска", file=sys.stderr)
            return 2
        weights = dict(DEFAULT_ROLE_WEIGHTS)
        weights["tool"] = args.tool_weight
        rows = query(db, args.text, n=args.n, source=args.source, role=args.role,
                     role_weights=weights)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(f"найдено: {len(rows)} (показано до {args.n})")
            for r in rows:
                tools = f" | tools: {r['tools']}" if r.get("tools") else ""
                print(f"\n[{r['source']}] {clip(r['title'] or '(без названия)', 60)}")
                print(f"  {r['role']} · {r['ts']} · chat={r['chat_id']} seq={r['seq']}{tools}")
                print(f"  {r['snip']}")
        return 0

    if args.cmd == "context":
        if not args.chat or args.seq is None:
            print("нужны --chat и --seq", file=sys.stderr)
            return 2
        rows = context(db, args.chat, args.seq, k=args.k, source=args.source)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                marker = ">>" if r["seq"] == args.seq else "  "
                tools = f" [{r['tools']}]" if r.get("tools") else ""
                print(f"{marker} #{r['seq']} {r['role']} {r['ts']}{tools}")
                print("   " + clip(r["text"], 600).replace("\n", "\n   "))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
