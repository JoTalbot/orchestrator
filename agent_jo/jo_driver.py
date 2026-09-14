#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jo-драйвер: автономно ведёт проект в ChatGPT от имени Jo.

Цикл:
  1. Читает состояние проекта (git, ROADMAP, статус).
  2. Если чата нет — открывает папку проекта в ChatGPT и стартует новый чат
     сообщением в стиле Jo.
  3. Если чат есть — анализирует последний ответ ассистента и пишет следующее
     сообщение в стиле Jo (продолжи / да, делай всё сам / статус / исправь).
  4. Если контекст чата разросся — пересоздаёт чат в той же папке проекта
     с handoff-сообщением (текущее состояние + задачи).
  5. Пишет лог каждого шага. Всё на русском.

Запуск:
  python agent_jo/jo_driver.py --project ukraine \
      --project-url https://chatgpt.com/g/g-p-XXXX-ukraine \
      --repo-path /opt/orchestrator/projects/ukraine [--once]
"""

import argparse
import asyncio
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import jo_style
from chatgpt_ui import ChatGPTUI

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".secrets"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- репозиторий

def repo_state(repo_path: str) -> dict:
    """Состояние проекта: коммиты, открытые задачи роадмапа, статус."""
    out: dict = {"commits": "(git недоступен)", "tasks": []}
    try:
        r = subprocess.run(["git", "-C", repo_path, "fetch", "--quiet", "--prune"],
                           capture_output=True, timeout=120)
        r = subprocess.run(["git", "-C", repo_path, "log", "--oneline", "-8"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            out["commits"] = r.stdout.strip()
    except Exception as e:
        out["commits"] = f"(git: {e})"
    roadmap = Path(repo_path) / "docs" / "ROADMAP.md"
    if roadmap.exists():
        try:
            text = roadmap.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        tasks = []
        for m in re.finditer(r"-\s+\*\*([A-Z]+-\d+)\s*[—–-]\s*([^*]*?)\*\*:\s*([^\n]+)", text):
            code, name, desc = m.group(1), m.group(2), m.group(3)
            if re.search(r"Implemented", desc):
                continue
            tasks.append(f"{code} — {desc.strip()[:90]}")
        out["tasks"] = tasks[:4]
    for cand in ("STATUS.md", "docs/STATUS.md", ".github/state/STATUS.md"):
        p = Path(repo_path) / cand
        if p.exists():
            try:
                out["status"] = p.read_text(encoding="utf-8")[:1500]
            except Exception:
                pass
            break
    return out


def repo_summary_text(st: dict) -> str:
    parts = [f"Коммиты:\n{st['commits']}"]
    if st.get("tasks"):
        parts.append("Задачи:\n" + "\n".join("- " + t for t in st["tasks"]))
    if st.get("status"):
        parts.append("Статус:\n" + st["status"])
    return "\n\n".join(parts)[:3500]


def read_secret(name: str, default: str = "") -> str:
    p = SECRETS / name
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return default


# ---------------------------------------------------------------- состояние

class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------- драйвер

class JoDriver:
    def __init__(self, args):
        self.args = args
        self.ui = ChatGPTUI(cdp_base=args.cdp, log=log)
        self.state = State(Path(args.state_file))
        self.cycles = self.state.get("cycles", 0)
        self.stuck_streak = 0

    @staticmethod
    def _conv_from_url(url: str) -> str | None:
        m = re.search(r"/c/([0-9a-fA-F-]{36})", url or "")
        return m.group(1) if m else None

    async def open_project_fresh_chat(self):
        """Открывает страницу проекта (новый чат в папке проекта)."""
        log(f"открываю папку проекта: {self.args.project_url}")
        await self.ui.tab.navigate(self.args.project_url, timeout=120)
        await asyncio.sleep(12)
        await self.ui._dismiss_popups()

    async def run_cycle(self) -> None:
        st = repo_state(self.args.repo_path)
        ctx = {
            "repo_name": self.args.repo_name,
            "repo_url": self.args.repo_url,
            "github_pat": read_secret("github_pat.txt"),
            "repo_summary": repo_summary_text(st),
            "tasks": ", ".join(st.get("tasks", [])) or None,
        }
        conv_id = self.state.get("conversation_id")
        last_reply = self.state.get("last_reply_tail")

        if not conv_id:
            # ---- старт нового чата в папке проекта
            await self.open_project_fresh_chat()
            user = await self.ui.session_user()
            log(f"сессия ChatGPT: {user}")
            msg = jo_style.compose("INIT", ctx, 0)
            log(f"→ новый чат: {msg[:120]}...")
            reply = await self.ui.send(msg, self.args.reply_timeout)
            self._after_reply(reply, msg)
            return

        await self.ui.ensure_alive()
        # ---- продолжить ИМЕННО в существующем чате
        url = await self.ui.current_url()
        if self._conv_from_url(url) != conv_id:
            log(f"перехожу в чат {conv_id[:8]}…")
            await self.ui.tab.navigate(f"https://chatgpt.com/c/{conv_id}", timeout=120)
            await asyncio.sleep(10)
            await self.ui._dismiss_popups()
        # ---- проверка размера контекста
        n_msgs = await self.ui.message_count()
        log(f"чат {conv_id[:8]}…, сообщений на экране: {n_msgs}")
        if n_msgs > self.args.max_messages:
            log("контекст разросся — пересоздаю чат в той же папке")
            await self.open_project_fresh_chat()
            msg = jo_style.compose("HANDOFF", ctx, self.cycles)
            log(f"→ handoff: {msg[:120]}...")
            reply = await self.ui.send(msg, self.args.reply_timeout)
            self._after_reply(reply, msg)
            return

        # ---- что происходит в чате
        if not last_reply:
            try:
                msgs = await self.ui.assistant_messages()
                last_reply = msgs[-1] if msgs else None
            except Exception:
                last_reply = None
        kind = jo_style.decide(last_reply)
        if kind == "OVERFLOW":
            await self.open_project_fresh_chat()
            msg = jo_style.compose("HANDOFF", ctx, self.cycles)
        else:
            msg = jo_style.compose(kind, ctx, self.cycles)
        log(f"→ [{kind}] {msg[:140]}")
        reply = await self.ui.send(msg, self.args.reply_timeout)
        self._after_reply(reply, msg)

    def _after_reply(self, reply: str | None, sent: str) -> None:
        self.cycles += 1
        self.state.set("cycles", self.cycles)
        self.state.set("conversation_id", self.ui.conversation_id)
        self.state.set("last_sent", sent[:400])
        if reply:
            tail = reply[-800:].replace("\n", " ")
            self.state.set("last_reply_tail", tail)
            self.stuck_streak = 0
            log(f"← ответ ассистента ({len(reply)} симв.): {tail[:220]}")
        else:
            self.stuck_streak += 1
            self.state.set("last_reply_tail", None)
            log(f"← ответа нет (таймаут). stuck_streak={self.stuck_streak}")
            if self.stuck_streak >= 3:
                log("3 таймаута подряд — сбрасываю чат")
                self.state.set("conversation_id", None)
                self.stuck_streak = 0

    async def run(self) -> None:
        await self.ui.start()
        log(f"сессия ChatGPT: {await self.ui.session_user()}")
        if self.args.once:
            await self.run_cycle()
            await self.ui.close()
            return
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                log(f"ошибка цикла: {type(e).__name__}: {e}")
            delay = self.args.cycle_delay
            log(f"следующий цикл через {delay}с (всего циклов: {self.cycles})")
            await asyncio.sleep(delay)


def main() -> None:
    ap = argparse.ArgumentParser(description="Jo-драйвер проекта в ChatGPT")
    ap.add_argument("--project", required=True, help="ключ проекта (имя папки)")
    ap.add_argument("--project-url", required=True,
                    help="URL папки проекта в ChatGPT (https://chatgpt.com/g/g-p-...)")
    ap.add_argument("--repo-name", default="")
    ap.add_argument("--repo-url", default="")
    ap.add_argument("--repo-path", default="")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--state-file", default="")
    ap.add_argument("--once", action="store_true",
                    help="один цикл и выход (тест)")
    ap.add_argument("--max-messages", type=int, default=200,
                    help="порог пересоздания чата (сообщений на экране)")
    ap.add_argument("--reply-timeout", type=float, default=1500,
                    help="таймаут ожидания ответа, сек")
    ap.add_argument("--cycle-delay", type=int, default=120,
                    help="пауза между циклами, сек")
    args = ap.parse_args()
    if not args.repo_name:
        args.repo_name = f"JoTalbot/{args.project}"
    if not args.repo_url:
        args.repo_url = f"https://github.com/JoTalbot/{args.project}"
    if not args.repo_path:
        args.repo_path = str(ROOT / "projects" / args.project)
    if not args.state_file:
        args.state_file = str(ROOT / "agent_jo" / f"state_{args.project}.json")

    driver = JoDriver(args)
    asyncio.run(driver.run())


if __name__ == "__main__":
    main()
