"""Sessions: one SQLite table in ~/.rootcode/sessions.db, saved after every turn."""

import json
import os
import re
import sqlite3
import time

from .modes import strip_plan_note
from .settings import DB_PATH, HOME_DIR, state
from .ui import ACCENT, BOLD, DIM, GREY, RESET, YELLOW, ask, tidy_path


def ago(t):
    secs = time.time() - t
    if secs < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"


def prompts(messages):
    """What the user typed, in order (tool results and Root's own nudges aside)."""
    return [strip_plan_note(m["content"]) for m in messages
            if m["role"] == "user" and isinstance(m.get("content"), str)]


def first_prompt(messages):
    typed = prompts(messages)
    return typed[0].strip().split("\n")[0][:70] if typed else "(untitled)"


class Sessions:
    def __init__(self):
        self.id, self.db = None, None
        try:
            os.makedirs(HOME_DIR, exist_ok=True)
            fresh = not os.path.exists(DB_PATH)
            self.db = sqlite3.connect(DB_PATH)
            if fresh:
                os.chmod(DB_PATH, 0o600)    # conversations can hold code and secrets: yours only
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, cwd TEXT, title TEXT, "
                            "created REAL, updated REAL, turns INTEGER, provider TEXT, model TEXT, "
                            "messages TEXT, todos TEXT)")
        except (sqlite3.Error, OSError) as e:
            self.db = None
            print(f"  {YELLOW}Sessions won't be saved: {e}{RESET}")

    def save(self, messages):
        if state.incognito or not self.db or not messages:
            return
        row = (time.time(), len(prompts(messages)), state.provider, state.model,
               json.dumps(messages), json.dumps(state.todos))
        try:
            if self.id is None:
                self.id = self.db.execute(
                    "INSERT INTO sessions (cwd, title, created, updated, turns, provider, model, messages, todos) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (os.getcwd(), first_prompt(messages), time.time()) + row
                ).lastrowid
            else:
                self.db.execute("UPDATE sessions SET updated=?, turns=?, provider=?, model=?, messages=?, todos=? "
                                "WHERE id=?", row + (self.id,))
            self.db.commit()
        except sqlite3.Error as e:
            print(f"  {YELLOW}Could not save the session: {e}{RESET}")

    def recent(self, search="", limit=20):
        if not self.db:
            return []
        return self.db.execute("SELECT id, cwd, title, updated, turns FROM sessions WHERE title LIKE ? "
                               "ORDER BY updated DESC LIMIT ?", (f"%{search}%", limit)).fetchall()

    def latest_here(self):
        if not self.db:
            return None
        row = self.db.execute("SELECT id FROM sessions WHERE cwd=? ORDER BY updated DESC LIMIT 1",
                              (os.getcwd(),)).fetchone()
        return row[0] if row else None

    def load(self, sid, messages):
        cwd, data, plan = self.db.execute("SELECT cwd, messages, todos FROM sessions WHERE id=?", (sid,)).fetchone()
        messages[:], state.todos[:] = json.loads(data), json.loads(plan or "[]")
        self.id = sid
        state.usage = len(json.dumps(messages)) // 4
        if cwd != os.getcwd() and os.path.isdir(cwd):
            os.chdir(cwd)                   # tools use relative paths: go back to the session's folder
            print(f"  {DIM}✻ Moved to {tidy_path(cwd)}, where this session ran{RESET}")
        recap(messages)


def recap(messages):
    """Show where a resumed conversation left off."""
    typed = prompts(messages)
    asked = typed[-1] if typed else ""
    said = next((m["content"] for m in reversed(messages) if m["role"] == "assistant" and m.get("content")), "")
    print(f"\n  {GREY}Resumed · {len(typed)} prompt{'s' if len(typed) != 1 else ''}{RESET}")
    if asked:
        print(f"  {DIM}you:{RESET}  {asked.strip()[:200]}")
    if said:
        print(f"  {DIM}root:{RESET} {re.sub(chr(10) + '+', ' ', said.strip())[:300]}")


def pick_session(sessions, messages, search=""):
    rows = sessions.recent(search)
    if not rows:
        print(f"  {DIM}No saved sessions{' match ' + repr(search) if search else ' yet'}.{RESET}")
        return
    here = os.getcwd()
    print(f"\n  {BOLD}Resume a session{RESET}")
    for i, (sid, cwd, title, updated, turns) in enumerate(rows, 1):
        where = "" if cwd == here else " · …/" + "/".join(cwd.rstrip("/").split("/")[-2:])
        print(f"  {DIM}{i:>2}.{RESET} {title}  {DIM}{ago(updated)} · {turns} prompt{'s' if turns != 1 else ''}{where}{RESET}")
    answer = ask(f"  {ACCENT}❯{RESET} {DIM}[number; enter cancels]{RESET} ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(rows):
        sessions.load(rows[int(answer) - 1][0], messages)
    else:
        print(f"  {DIM}Cancelled.{RESET}")
