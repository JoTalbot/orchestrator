#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сбор всех чатов ChatGPT-аккаунта через неофициальный backend-api (chatgpt.com).

Ключевая особенность: запросы идут через curl_cffi с имитацией TLS-отпечатка
Chrome — это обходит блокировку Cloudflare на серверах с IP дата-центров
(без этого chatgpt.com отдаёт 403 на любой запрос).

Что делает:
  * пагинация по всем чатам аккаунта (offset/limit, сортировка по обновлению);
  * полная выгрузка каждого чата: GET /backend-api/conversation/{id};
  * устойчивость: ретраи при сетевых ошибках, 429/5xx/403 с экспоненциальной
    задержкой и учётом Retry-After;
  * возобновляемость: уже скачанные и не изменившиеся чаты пропускаются
    (по update_time), индекс сохраняется каждые 25 чатов;
  * атомарная запись файлов (tmp + rename), безопасно прерывать в любой момент.

Выходные данные (data/):
  chats/{id}.json  — сырой ответ backend-api для чата (полное дерево mapping);
  light/{id}.json  — лёгкая выжимка: только сообщения (role, text, время);
  index.json       — индекс всех чатов (id, title, время, число сообщений);
  errors.json      — чаты, которые не удалось скачать;
  summary.json     — итоговая статистика.

Токен:
  * переменная окружения CHATGPT_ACCESS_TOKEN, либо
  * аргумент --token, либо
  * файл .secrets/chatgpt_token.txt (права 600, не коммитится).

Как получить access token:
  1) залогиньтесь на https://chatgpt.com в браузере;
  2) откройте в ТОМ ЖЕ браузере https://chatgpt.com/api/auth/session;
  3) скопируйте значение поля "accessToken" из JSON;
  4) вставьте его в .secrets/chatgpt_token.txt на сервере.

Пример запуска:
  python3 chatgpt_export/export_chats.py
  python3 chatgpt_export/export_chats.py --limit 5 --force   # тест на 5 чатах
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as cf_requests  # обход Cloudflare
except ImportError:  # pragma: no cover
    cf_requests = None

import requests

API_BASE = "https://chatgpt.com/backend-api"
PAGE_LIMIT = 100  # максимум на страницу списка чатов

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class AuthError(Exception):
    pass


class ApiError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    env = os.environ.get("CHATGPT_ACCESS_TOKEN")
    if env:
        return env.strip()
    tf = Path(args.token_file)
    if tf.exists():
        t = tf.read_text().strip()
        if t:
            return t
    log("Токен не найден. Укажите --token, переменную CHATGPT_ACCESS_TOKEN "
        "или положите токен в файл .secrets/chatgpt_token.txt")
    sys.exit(2)


def make_session(token: str):
    """Сессия curl_cffi (Chrome TLS), fallback на обычный requests."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
    }
    if cf_requests is not None:
        s = cf_requests.Session(impersonate="chrome")
        s.headers.update(headers)
        return s
    log("ВНИМАНИЕ: curl_cffi не установлен, работаем через обычный requests "
        "(Cloudflare может блокировать с дата-центровых IP)")
    s = requests.Session()
    s.headers.update(headers)
    return s


def api_get(session, url: str, params: dict | None = None,
            retries: int = 8, quiet: bool = False):
    """GET с ретраями: сетевые ошибки, 429 (Retry-After), 5xx, 403."""
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=60)
        except Exception as e:
            last_err = e
            wait = min(60, 2 ** attempt)
            if not quiet:
                log(f"Сетевая ошибка ({type(e).__name__}): {e}; "
                    f"повтор через {wait}с ({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        if r.status_code == 401:
            raise AuthError("Токен недействителен или истёк (HTTP 401). "
                            "Обновите access token.")
        if r.status_code == 429:
            try:
                retry_after = int(r.headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError):
                retry_after = 0
            wait = max(retry_after, min(120, 5 * (2 ** attempt))) + random.uniform(0, 2)
            if not quiet:
                log(f"Rate limit (429). Ждём ~{wait:.0f}с ({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        if r.status_code == 403:
            # возможно временный ответ Cloudflare
            wait = min(60, 2 ** attempt) + random.uniform(0, 2)
            if not quiet:
                log(f"403 Forbidden. Повтор через {wait:.0f}с ({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = min(60, 2 ** attempt)
            if not quiet:
                log(f"Сервер вернул {r.status_code}. Повтор через {wait}с "
                    f"({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} на {url}: {r.text[:300]}")
        return r.json()
    raise ApiError(f"Не удалось получить {url} за {retries} попыток: {last_err}")


def list_conversations(session, include_archived: bool = False) -> list[dict]:
    """Возвращает метаданные всех чатов (id, title, create/update_time)."""
    items: list[dict] = []
    offset = 0
    params: dict = {"limit": PAGE_LIMIT, "order": "updated", "offset": 0}
    if include_archived:
        params["archived"] = "true"
    while True:
        params["offset"] = offset
        data = api_get(session, f"{API_BASE}/conversations", params=params)
        batch = data.get("items", [])
        items.extend(batch)
        total = data.get("total")
        offset += len(batch)
        extra = ""
        if total:
            extra = f" из {total}"
        if data.get("has_missing_conversations"):
            extra += " (has_missing_conversations)"
        log(f"Метаданные чатов получены: {len(items)}{extra}")
        if not batch:
            break
        if total and offset >= total:
            break
        if len(batch) < PAGE_LIMIT:
            break
        time.sleep(0.3)
    return items


def fetch_conversation(session, cid: str) -> dict:
    return api_get(session, f"{API_BASE}/conversation/{cid}")


def extract_light(conv: dict) -> list[dict]:
    """Выжимка текста сообщений из полного дерева mapping."""
    mapping = conv.get("mapping") or {}
    msgs: list[dict] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author") or {}
        role = author.get("role", "unknown")
        content = msg.get("content") or {}
        parts = content.get("parts") or []
        text_parts: list[str] = []
        part_types: list[str] = []
        for p in parts:
            if isinstance(p, str):
                text_parts.append(p)
            elif isinstance(p, dict):
                ct = p.get("content_type")
                if ct:
                    part_types.append(ct)
        text = "".join(text_parts)
        if not text and not part_types:
            continue  # системные/пустые узлы пропускаем
        msgs.append({
            "id": msg.get("id"),
            "role": role,
            "create_time": msg.get("create_time"),
            "status": msg.get("status"),
            "content_type": content.get("content_type"),
            "text": text,
            "part_types": part_types,
        })
    msgs.sort(key=lambda m: m.get("create_time") or 0)
    return msgs


def save_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Сбор всех чатов ChatGPT-аккаунта через backend-api")
    ap.add_argument("--token", help="Access token напрямую (небезопасно, лучше файл)")
    ap.add_argument("--token-file", default=".secrets/chatgpt_token.txt")
    ap.add_argument("--data-dir", default="data", help="Каталог для результатов")
    ap.add_argument("--limit", type=int, default=0,
                    help="Ограничить число скачиваемых чатов (0 = все)")
    ap.add_argument("--force", action="store_true",
                    help="Перекачать даже уже скачанные чаты")
    ap.add_argument("--only-meta", action="store_true",
                    help="Только список чатов (метаданные), без содержимого")
    ap.add_argument("--include-archived", action="store_true",
                    help="Попробовать включить архивные чаты в список")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="Базовая пауза между запросами, сек (default 0.5)")
    args = ap.parse_args()

    token = get_token(args)
    session = make_session(token)

    # Проверка токена и вывод имени аккаунта
    try:
        me = api_get(session, f"{API_BASE}/me", quiet=True)
    except AuthError as e:
        log(f"АВТОРИЗАЦИЯ: {e}")
        sys.exit(3)
    log(f"Аккаунт подтверждён: {me.get('name', '?')} "
        f"({me.get('email', 'email скрыт')})")

    root = Path(args.data_dir)
    chats_dir = root / "chats"
    light_dir = root / "light"
    chats_dir.mkdir(parents=True, exist_ok=True)
    light_dir.mkdir(parents=True, exist_ok=True)

    index_path = root / "index.json"
    index: dict = {}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            log(f"Загружен индекс: {len(index)} чатов уже известны")
        except json.JSONDecodeError:
            log("ВНИМАНИЕ: index.json повреждён, начинаем индекс заново")

    metas = list_conversations(session, include_archived=args.include_archived)
    log(f"Всего чатов в списке: {len(metas)}")

    if args.only_meta:
        meta_path = root / "chat_list.json"
        save_json_atomic(meta_path, metas)
        log(f"Список чатов сохранён: {meta_path}")
        return

    errors: list[dict] = []
    done = new = skipped = 0
    try:
        for i, meta in enumerate(metas):
            cid = meta.get("id")
            if not cid:
                continue
            if args.limit and i >= args.limit:
                log(f"Остановка по --limit {args.limit}")
                break
            existing = index.get(cid)
            raw_path = chats_dir / f"{cid}.json"
            if (not args.force and existing and raw_path.exists()
                    and (meta.get("update_time") or 0)
                    <= (existing.get("update_time") or 0)):
                skipped += 1
                continue
            try:
                conv = fetch_conversation(session, cid)
                save_json_atomic(raw_path, conv)
                light = extract_light(conv)
                save_json_atomic(light_dir / f"{cid}.json", light)
                index[cid] = {
                    "id": cid,
                    "title": meta.get("title") or conv.get("title") or "",
                    "create_time": meta.get("create_time"),
                    "update_time": meta.get("update_time"),
                    "messages": len(light),
                    "downloaded_at": int(time.time()),
                }
                new += 1
                if new % 10 == 1 or new <= 3:
                    log(f"  [{i + 1}/{len(metas)}] + '{index[cid]['title'][:60]}' "
                        f"({len(light)} сообщений)")
            except Exception as e:
                log(f"  ОШИБКА чата {cid}: {e}")
                errors.append({"id": cid, "title": meta.get("title"),
                               "error": str(e)})
            done += 1
            if done % 25 == 0:
                save_json_atomic(index_path, index)
                log(f"Прогресс: обработано {done}/{len(metas)} "
                    f"(новых {new}, пропущено {skipped}, ошибок {len(errors)})")
            time.sleep(random.uniform(args.sleep, args.sleep * 1.8))
    except KeyboardInterrupt:
        log("Прервано пользователем, сохраняем прогресс...")

    save_json_atomic(index_path, index)
    if errors:
        save_json_atomic(root / "errors.json", errors)
    summary = {
        "total_in_list": len(metas),
        "processed": done,
        "new": new,
        "skipped": skipped,
        "errors": len(errors),
        "index_size": len(index),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json_atomic(root / "summary.json", summary)
    log(f"ГОТОВО: {summary}")
    if errors:
        log(f"Ошибки сохранены в data/errors.json — "
            f"перезапустите скрипт, чтобы докачать (--force, если нужно).")


if __name__ == "__main__":
    main()
