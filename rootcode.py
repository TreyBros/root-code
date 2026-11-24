#!/usr/bin/env python3
"""
Root: a lightweight terminal coding agent in one file. Python 3.9+, no dependencies.

    rootcode                       interactive; asks before running commands or changing files
    rootcode "fix the failing tests"   one task, then exit
    rootcode /model                pick provider, enter key, pick model
    rootcode --plan                start in plan mode (explore read-only, propose a plan, you approve it)
    rootcode --auto                start in auto mode (no permission prompts)
    rootcode --continue  (or -c)   pick up the latest session in this folder
    rootcode --resume              choose a past session to continue
                                   default is approve mode: asks before commands and file changes.
                                   shift+tab or /mode switches modes while it runs.
    rootcode --ollama --model=qwen3.6          flags pick provider and model for one run

Providers: anthropic, openai, openrouter, ollama, and local (any OpenAI-compatible server:
LM Studio, llama.cpp, vLLM). /model saves your choice to ~/.rootcode/config.json.

Environment (wins over the saved choice):
    ANTHROPIC_API_KEY  OPENAI_API_KEY  OPENROUTER_API_KEY  ROOTCODE_LOCAL_KEY
    ROOTCODE_PROVIDER  ROOTCODE_MODEL  ROOTCODE_BASE_URL  ROOTCODE_COMPACT_TOKENS
    OLLAMA_HOST (default http://localhost:11434)   NO_COLOR

The design: a loop the model exits by not calling a tool, a handful of sharp tools,
errors fed back as text, output kept small, and a summary when the context fills up.
"""

import difflib
import getpass
import glob as globlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request

try:
    import select
    import termios
    import tty
except ImportError:                     # Windows: plain input() instead of Root's own input line
    termios = None

VERSION = "1.0"
HOME_DIR = os.path.expanduser("~/.rootcode")
CONFIG_PATH = os.path.join(HOME_DIR, "config.json")
HISTORY_PATH = os.path.join(HOME_DIR, "history.jsonl")
DB_PATH = os.path.join(HOME_DIR, "sessions.db")
UA = f"root-code/{VERSION}"
MAX_OUTPUT_TOKENS = 16000
MAX_TOOL_CHARS = 30000                  # anything longer is cut to its head and tail
MAX_TOOL_CHARS_LOCAL = 12000            # local models read slowly: keep tool output tighter
LOG_PATH = os.path.join(os.path.expanduser("~/.rootcode"), "log.jsonl")

# ----------------------------------------------------------------------------
# Look: 256-colour ANSI, off when piped or NO_COLOR is set
# ----------------------------------------------------------------------------

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def sgr(code):
    return f"\033[{code}m" if TTY else ""


def fg(n):
    return sgr(f"38;5;{n}")


RESET, BOLD, DIM, STRIKE = sgr(0), sgr(1), sgr(2), sgr(9)
ACCENT, RED, GREEN, YELLOW, GREY, CODE = fg(42), fg(203), fg(114), fg(179), fg(245), fg(180)
RED_BG, GREEN_BG = sgr("48;5;52"), sgr("48;5;22")

LOGO = [r + o + o2 + t for r, o, o2, t in zip(
    ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "])]
LOGO_SHADES = [120, 84, 48, 42, 36, 30]
VERBS = ["Thinking", "Rooting", "Digging", "Branching", "Sprouting", "Grafting", "Pruning",
         "Cultivating", "Germinating", "Untangling", "Composting", "Growing"]


def width():
    return shutil.get_terminal_size((100, 24)).columns


def tidy_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def human(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class Spinner:
    """An animated status line on its own thread: frame, shimmering label, timer, tokens."""
    FRAMES = "·✢✳✶✻✽✻✶✳✢"

    def __init__(self):
        self.on, self.thread, self.lock = False, None, threading.Lock()
        self.label, self.t0, self.chars, self.reading, self.preview = "", time.time(), 0, 0, ""

    def start(self, label, t0=None):
        self.label = label
        if t0:
            self.t0 = t0
        if self.on or not TTY:
            return
        self.on = True
        self.thread = threading.Thread(target=self._spin, daemon=True)
        self.thread.start()

    def _spin(self):
        i = 0
        while self.on:
            with self.lock:
                if self.on:
                    sys.stdout.write("\r\033[K" + self._line(i))
                    sys.stdout.flush()
            i += 1
            time.sleep(0.08)

    def _line(self, i):
        pos = i % (len(self.label) + 8) - 4
        label = "".join((BOLD + ACCENT if abs(j - pos) <= 1 else ACCENT) + ch
                        for j, ch in enumerate(self.label)) + RESET
        if self.chars >= 40:
            extra = f" · ↓ {human(self.chars // 4)} tokens"
        elif self.reading:              # before the first token: the model is reading the conversation
            extra = f" · reading {human(self.reading)} tokens"
        else:
            extra = ""
        hint = f"({int(time.time() - self.t0)}s{extra}" + ("" if self.preview else " · ctrl+c to interrupt") + ")"
        line = f"{ACCENT}{self.FRAMES[i % len(self.FRAMES)]}{RESET} {label}{ACCENT}…{RESET} {DIM}{hint}{RESET}"
        room = width() - len(self.label) - len(hint) - 7
        if room < 0:
            return line.split(" " + DIM)[0]
        if self.preview and room > 12:   # what the model is thinking, newest words last
            line += f" {GREY}{'…' + self.preview[-(room - 2):] if len(self.preview) > room - 1 else self.preview}{RESET}"
        return line

    def stop(self):
        if not self.on:
            return
        with self.lock:
            self.on = False
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self.thread.join()


spinner = Spinner()


class Markdown:
    """Line-buffered rendering of the parts of markdown that matter in a terminal."""

    def __init__(self):
        self.buf, self.fence, self.started = "", False, False

    def feed(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)

    def flush(self):
        if self.buf:
            self.emit(self.buf)
            self.buf = ""

    def emit(self, line):
        if not self.started and not line.strip():
            return                      # skip leading blank lines
        prefix = "  " if self.started else f"{ACCENT}⏺{RESET} "
        self.started = True
        stripped = line.strip()
        if stripped.startswith("```"):
            self.fence = not self.fence
            out = f"{DIM}╭─ {stripped[3:]}{RESET}" if self.fence else f"{DIM}╰─{RESET}"
        elif self.fence:
            out = f"{DIM}│{RESET} {CODE}{line}{RESET}"
        elif re.match(r"#{1,6}\s", stripped):
            out = BOLD + ACCENT + stripped.lstrip("#").strip() + RESET
        else:                           # prose: wrap at word boundaries, keep `code` and **bold** in one piece
            text = re.sub(r"^(\s*)[-*] ", r"\1• ", line)
            text = re.sub(r"(`[^`]+`|\*\*[^*]+\*\*)", lambda m: m[1].replace(" ", "\u00a0"), text)
            body = text.lstrip()
            hang = len(text) - len(body) + (2 if body.startswith("• ") else len(body.split(" ")[0]) + 1
                                             if re.match(r"\d+\. ", body) else 0)
            pieces = textwrap.wrap(text, max(width() - 4, 20), subsequent_indent=" " * hang,
                                   break_on_hyphens=False) or [""]
            for k, piece in enumerate(pieces):
                piece = re.sub(r"`([^`]+)`", lambda m: CODE + m[1] + RESET, piece.replace("\u00a0", " "))
                piece = re.sub(r"\*\*([^*]+)\*\*", lambda m: BOLD + m[1] + RESET, piece)
                print(("  " if k else prefix) + piece, flush=True)
            return
        print(prefix + out, flush=True)


def ask(prompt):
    """A short answer to a question: a number, y/n, a key."""
    return input(prompt)

# ----------------------------------------------------------------------------
# Providers: two wire formats cover all of them
# ----------------------------------------------------------------------------

PROVIDERS = {
    "anthropic":  {"label": "Anthropic", "key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5",
                   "url": "https://api.anthropic.com/v1", "ctx": 140000},
    "openai":     {"label": "OpenAI", "key_env": "OPENAI_API_KEY", "model": "gpt-6.2-sol",
                   "url": "https://api.openai.com/v1", "ctx": 140000},
    "openrouter": {"label": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "model": "anthropic/claude-sonnet-5.5",
                   "url": "https://openrouter.ai/api/v1", "ctx": 140000},
    "ollama":     {"label": "Ollama (local)", "key_env": None, "model": "",   # first installed model
                   "url": None, "ctx": 48000},
    "local":      {"label": "Local server (LM Studio, llama.cpp, vLLM)", "key_env": "ROOTCODE_LOCAL_KEY",
                   "model": "", "url": None, "ctx": 24000, "key_optional": True},
}


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config():
    os.makedirs(HOME_DIR, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


CONFIG = load_config()


def flag(name):
    for arg in sys.argv[1:]:
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None


def api_key(provider):
    env = PROVIDERS[provider]["key_env"]
    return (env and os.environ.get(env)) or CONFIG.get("keys", {}).get(provider)


def base_url(provider):
    if os.environ.get("ROOTCODE_BASE_URL") and provider == PROVIDER:
        return os.environ["ROOTCODE_BASE_URL"].rstrip("/")
    if provider == "ollama":
        return ollama_host() + "/v1"
    if provider == "local":
        return CONFIG.get("local_url", "http://localhost:1234/v1").rstrip("/")
    return PROVIDERS[provider]["url"]


def ollama_host():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return (host if host.startswith("http") else "http://" + host).rstrip("/")


def auth(provider, key):
    if provider == "anthropic":
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def pick_provider():
    for name in PROVIDERS:
        if f"--{name}" in sys.argv:
            return name
    chosen = flag("provider") or os.environ.get("ROOTCODE_PROVIDER") or CONFIG.get("provider")
    if chosen:
        if chosen not in PROVIDERS:
            sys.exit(f"Unknown provider {chosen!r}. Choose one of: {', '.join(PROVIDERS)}")
        return chosen
    for name in ("anthropic", "openrouter", "openai"):
        if api_key(name):
            return name
    return "ollama"


PROVIDER = pick_provider()
MODEL = (flag("model") or os.environ.get("ROOTCODE_MODEL")
         or (CONFIG.get("model") if CONFIG.get("provider") == PROVIDER else None)
         or PROVIDERS[PROVIDER]["model"])


def compact_at():
    return int(os.environ.get("ROOTCODE_COMPACT_TOKENS", PROVIDERS[PROVIDER]["ctx"]))


MODE = ("plan" if "--plan" in sys.argv
        else "auto" if "--auto" in sys.argv or "--yolo" in sys.argv else "approve")
MODE_ORDER = ("approve", "auto", "plan")            # shift+tab cycles in this order
MODE_LOOK = {"approve": (GREEN, "approve mode", "asks before commands and file changes"),
             "auto":    (YELLOW, "⏵⏵ auto mode", "runs commands and edits without asking"),
             "plan":    (fg(75), "⏸ plan mode", "read-only: explores, then proposes a plan")}
PLAN_NOTE = ("[Plan mode is on. Do not change any files yet. Explore with read, glob and read-only shell "
             "commands, then reply with a concise numbered plan: what you will change in which files, and how "
             "you will verify it. The user approves the plan before you build.]\n\n")
SAFE_COMMANDS = {"ls", "cat", "head", "tail", "wc", "pwd", "rg", "grep", "find", "tree", "file", "stat", "du",
                 "which", "echo", "sort", "uniq", "cut", "diff", "basename", "dirname", "realpath", "git"}
SAFE_GIT = {"status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "grep"}


def read_only(command):
    """True for shell commands that only look: these run without asking in plan and approve modes."""
    command = re.sub(r"\d?>&\d|\d?>\s*/dev/null", "", command)
    if re.search(r"[>`]|\$\(|<\(", command):
        return False
    for part in re.split(r"\|\||&&|[|;\n]", command):
        words = part.split()
        if not words:
            continue
        if words[0] not in SAFE_COMMANDS:
            return False
        if words[0] == "git" and (len(words) < 2 or words[1] not in SAFE_GIT):
            return False
        if words[0] == "find" and any(w in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for w in words):
            return False
    return True


def set_mode(mode, quiet=False):
    global MODE
    MODE = mode
    if not quiet:
        colour, label, about = MODE_LOOK[mode]
        print(f"  {colour}{label}{RESET} {DIM}· {about}{RESET}")
THINK_LEVELS = ("default", "off", "low", "medium", "high")


def think_level():
    return flag("think") or os.environ.get("ROOTCODE_THINK") or CONFIG.get("think", "default")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status} {message}" if status else message)
        self.status = status


def error_text(raw):
    try:
        err = json.loads(raw).get("error", raw)
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]


def http(url, headers=None, body=None, timeout=600, retries=5):
    """Open a request, retrying rate limits, overloads and dropped connections with backoff."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "content-type": "application/json", **(headers or {})})
    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            message = error_text(e.read())
            if e.code not in (408, 409, 429, 500, 502, 503, 504, 529) or attempt == retries:
                raise ApiError(e.code, message) from None
            wait = float(e.headers.get("retry-after") or min(2 ** attempt, 30))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == retries:
                reason = getattr(e, "reason", e)
                raise ApiError(None, f"cannot reach {url.split('/v1')[0]} ({reason})") from None
            wait, message = min(2 ** attempt, 30), "connection problem"
        spinner.label = f"Retrying in {int(wait)}s ({message[:40]})"
        time.sleep(wait)


def get_json(url, headers=None):
    with http(url, headers, timeout=20, retries=1) as response:
        return json.load(response)


def sse(response):
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            yield line[5:].strip()


class Reply:
    def __init__(self):
        self.text, self.calls, self.stop, self.usage, self.reasoning_details = "", [], "end", 0, {}

    def add_call(self, call_id, name):
        call = {"id": call_id or f"call_{len(self.calls)}_{int(time.time() * 1000)}",
                "name": name or "", "arguments": ""}
        self.calls.append(call)
        return call


def to_anthropic(messages):
    """The OpenAI-style history used internally, as Anthropic content blocks."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            role, blocks = "user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                     "content": m["content"]}]
        elif m["role"] == "user":
            role, blocks = "user", [{"type": "text", "text": m["content"]}]
        else:
            role, blocks = "assistant", [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            if not blocks:
                continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    if out:                             # one moving cache breakpoint keeps a long loop cheap
        out[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return out


def to_openai(messages, provider):
    out = []
    for m in messages:
        m = dict(m)
        details = m.pop("reasoning_details", None)
        if details and provider == "openrouter":   # Gemini and others need their reasoning back
            m["reasoning_details"] = details
        out.append(m)
    return out


def chat(provider, key, model, system, messages, tools, on, max_tokens=MAX_OUTPUT_TOKENS, think=None):
    """One streamed model call. `on` gets text, reasoning, tool and progress events."""
    reply, url = Reply(), base_url(provider)
    if provider == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": to_anthropic(messages),
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
        blocks = {}
        with http(url + "/messages", auth(provider, key), body) as response:
            for data in sse(response):
                event = json.loads(data)
                kind = event.get("type")
                if kind == "message_start":
                    u = event["message"].get("usage") or {}
                    reply.usage = sum(u.get(k) or 0 for k in
                                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                elif kind == "content_block_start" and event["content_block"]["type"] == "tool_use":
                    block = event["content_block"]
                    blocks[event["index"]] = reply.add_call(block["id"], block["name"])
                    on.tool(block["name"])
                elif kind == "content_block_delta":
                    delta = event["delta"]
                    if delta["type"] == "text_delta":
                        reply.text += delta["text"]
                        on.text(delta["text"])
                    elif delta["type"] == "input_json_delta":
                        blocks[event["index"]]["arguments"] += delta["partial_json"]
                        on.progress(delta["partial_json"])
                    elif delta["type"] == "thinking_delta":
                        on.reasoning(delta.get("thinking", ""))
                elif kind == "message_delta":
                    reply.stop = {"tool_use": "tool", "max_tokens": "length"}.get(
                        event["delta"].get("stop_reason"), "end")
                    reply.usage += (event.get("usage") or {}).get("output_tokens") or 0
                elif kind == "error":
                    raise ApiError(None, error_text(json.dumps(event)))
        return reply

    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": system}] + to_openai(messages, provider)}
    body["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens
    if think and think != "default":
        if provider == "openrouter":
            body["reasoning"] = {"enabled": False} if think == "off" else {"effort": think}
        else:
            body["reasoning_effort"] = "none" if think == "off" else think
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
    slots = {}
    with http(url + "/chat/completions", auth(provider, key), body) as response:
        for data in sse(response):
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("error"):
                raise ApiError(None, error_text(json.dumps(event)))
            if event.get("usage"):
                reply.usage = (event["usage"].get("prompt_tokens") or 0) + (event["usage"].get("completion_tokens") or 0)
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning") or delta.get("reasoning_content")
                if thought:
                    on.reasoning(thought)
                for d in delta.get("reasoning_details") or []:
                    slot = reply.reasoning_details.setdefault(d.get("index", 0), {})
                    for k, v in d.items():
                        slot[k] = slot.get(k, "") + v if k in ("text", "summary", "data") and isinstance(v, str) else v
                if delta.get("content"):
                    reply.text += delta["content"]
                    on.text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    i = tc.get("index")
                    if i is None:
                        i = next((k for k, c in slots.items() if c["id"] == tc.get("id")), len(slots))
                    if i not in slots:
                        slots[i] = reply.add_call(tc.get("id"), fn.get("name"))
                        on.tool(fn.get("name") or "tool")
                    args = fn.get("arguments") or ""
                    args = json.dumps(args) if isinstance(args, dict) else args
                    slots[i]["arguments"] += args
                    on.progress(args)
                if choice.get("finish_reason"):
                    reply.stop = {"tool_calls": "tool", "length": "length"}.get(choice["finish_reason"], "end")
    return reply


def list_models(provider, key):
    if provider == "ollama":
        names = [m["name"] for m in get_json(ollama_host() + "/api/tags")["models"]]
        return [n for n in names if "embed" not in n]


def without_tools(provider, models):
    """Ollama models whose template can't do tool calls (Root needs them)."""
    if provider != "ollama":
        return set()
    missing = set()
    for name in models:
        try:
            show = http(ollama_host() + "/api/show", body={"model": name}, timeout=10, retries=0)
            with show:
                if "tools" not in (json.load(show).get("capabilities") or ["tools"]):
                    missing.add(name)
        except (ApiError, ValueError, OSError):
            pass
    return missing
    if provider == "openrouter":            # only models that can call tools
        data = get_json(base_url(provider) + "/models")["data"]
        return sorted(m["id"] for m in data if "tools" in (m.get("supported_parameters") or []))
    if provider == "anthropic":
        return [m["id"] for m in get_json(base_url(provider) + "/models?limit=1000", auth(provider, key))["data"]]
    ids = sorted(m["id"] for m in get_json(base_url(provider) + "/models", auth(provider, key))["data"])
    if provider == "openai":                # drop models that can't chat
        skip = r"embed|tts|whisper|dall-e|davinci|babbage|moderation|image|audio|realtime|transcribe|sora|search"
        ids = [i for i in ids if not re.search(skip, i)]
    return ids

# ----------------------------------------------------------------------------
# System prompt: describe the environment and what "done" means. Nothing else.
# ----------------------------------------------------------------------------

def system_prompt():
    parts = [
        "You are Root, a coding agent working in the user's terminal.",
        f"Working directory: {os.getcwd()}\nPlatform: {sys.platform}\nDate: {time.strftime('%Y-%m-%d')}",
        "Keep working until the task is fully done. Do not stop to ask whether to continue; "
        "ask only when you are blocked on a decision that is the user's to make.",
        "Look before you change: read the relevant files and search the codebase first. "
        "After changing code, verify it by running the tests, the build, or the program itself. "
        "If a tool fails, read the error and try another approach.",
        "Use paths relative to the working directory (for example src/app.py) rather than absolute paths.",
        "For work with more than a few steps, keep a plan with the todo tool and update it as you go.",
        "When finished, reply with a short summary of what you did and what you verified. Be brief. "
        "Your replies are shown in a terminal: use short paragraphs, lists and code blocks, not tables.",
    ]
    for name in ("ROOT.md", "AGENTS.md", "CLAUDE.md"):   # project instructions, first one found wins
        if os.path.isfile(name):
            with open(name, encoding="utf-8", errors="replace") as f:
                parts.append(f"Project instructions from {name}:\n{f.read()[:20000]}")
            break
    return "\n\n".join(parts)

# ----------------------------------------------------------------------------
# Tools: few and powerful. Every tool returns text, including its errors.
# ----------------------------------------------------------------------------

def schema(props, required):
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"name": "bash", "description":
        "Run a shell command in the working directory and return its combined output and exit code. "
        "Use it to search (rg, grep, find), run tests and builds, and use git. Commands are killed after "
        "the timeout, so start servers and other long-running jobs in the background "
        "(for example with nohup ... &) and check on them later.",
     "parameters": schema({"command": {"type": "string"},
                           "timeout": {"type": "integer", "description": "Seconds before the command is killed. Default 120."}},
                          ["command"])},
    {"name": "read", "description": "Read a text file with line numbers. Use offset and limit for large files.",
     "parameters": schema({"path": {"type": "string"},
                           "offset": {"type": "integer", "description": "First line to read, starting at 1."},
                           "limit": {"type": "integer", "description": "Number of lines to read. Default 2000."}},
                          ["path"])},
    {"name": "write", "description": "Create a file or replace its whole content. Prefer edit for changes to existing files.",
     "parameters": schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "edit", "description":
        "Replace one exact string in a file. old must match the file exactly, including whitespace, "
        "and must occur once; include surrounding lines to make it unique, or set replace_all.",
     "parameters": schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                           "replace_all": {"type": "boolean"}}, ["path", "old", "new"])},
    {"name": "glob", "description": "List files matching a glob pattern such as src/**/*.py, newest first.",
     "parameters": schema({"pattern": {"type": "string"}}, ["pattern"])},
    {"name": "todo", "description": "Record or update your plan. Send the full list every time. "
                                    "Statuses: pending, in_progress, done.",
     "parameters": schema({"items": {"type": "array", "items": schema(
         {"task": {"type": "string"}, "status": {"type": "string"}}, ["task", "status"])}}, ["items"])},
]
TITLES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Update", "glob": "Search", "todo": "Plan"}
NEEDS_PERMISSION = {"bash", "write", "edit"}
todos = []


def clip(text):
    """Keep tool output small: the head and the tail matter, the middle rarely does."""
    limit = MAX_TOOL_CHARS if PROVIDER not in ("ollama", "local") else MAX_TOOL_CHARS_LOCAL
    if len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half] + f"\n\n... [{len(text) - limit} characters cut; "
            "narrow the command or read a specific range to see more] ...\n\n" + text[-half:])


def tool_bash(command, timeout=120):
    try:
        done = subprocess.run(command, shell=True, capture_output=True, text=True,
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        partial = e.stdout or b""
        partial = partial.decode(errors="replace") if isinstance(partial, bytes) else partial
        return f"{partial}\n[killed after {timeout}s; run long jobs in the background or raise the timeout]"
    output = (done.stdout or "") + (done.stderr or "")
    return f"{output.rstrip()}\n[exit code {done.returncode}]"


def tool_read(path, offset=1, limit=2000):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start = max(offset, 1) - 1
    chunk = lines[start:start + limit]
    body = "".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(chunk))
    rest = len(lines) - (start + len(chunk))
    return (body or "[file is empty]") + (f"\n[{rest} more lines; read again with offset]" if rest > 0 else "")


def tool_write(path, content):
    folder = os.path.dirname(os.path.abspath(path))
    inside = os.path.abspath(path).startswith(os.getcwd() + os.sep)
    if not os.path.isdir(folder) and not inside:
        return f"Error: the folder {folder} does not exist. Check the path; prefer paths relative to the working directory."
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {content.count(chr(10)) + 1} lines to {path}"


def tool_edit(path, old, new, replace_all=False):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    count = text.count(old)
    if count == 0:
        return "Error: old was not found in the file. Read the file again and copy the exact text."
    if count > 1 and not replace_all:
        return f"Error: old occurs {count} times. Add surrounding lines to make it unique, or set replace_all."
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new) if replace_all else text.replace(old, new, 1))
    n = count if replace_all else 1
    return f"Updated {path} ({n} replacement{'s' if n > 1 else ''})"


def tool_glob(pattern):
    paths = [p for p in globlib.glob(pattern, recursive=True) if os.path.isfile(p)]
    paths.sort(key=os.path.getmtime, reverse=True)
    return "\n".join(paths[:500]) or "No files matched."


def tool_todo(items):
    todos[:] = items
    return "Plan saved."


RUNNERS = {"bash": tool_bash, "read": tool_read, "write": tool_write,
           "edit": tool_edit, "glob": tool_glob, "todo": tool_todo}


def show_call(name, args):
    label = args.get("command") or args.get("path") or args.get("pattern") or ""
    label = str(label).strip().split("\n")[0]
    room = max(width() - len(TITLES.get(name, name)) - 8, 20)
    label = label if len(label) <= room else label[:room - 1] + "…"
    print(f"\n{ACCENT}⏺{RESET} {BOLD}{TITLES.get(name, name)}{RESET}" + (f"({label})" if label else ""))


def tree(lines, colour=GREY):
    for i, line in enumerate(lines):
        print(f"  {DIM}{'⎿ ' if i == 0 else '  '}{RESET} {colour}{line}{RESET}")


def show_diff(path, old, new):
    """Line-numbered red/green preview of an edit, shown before asking permission."""
    start = 1
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        start = text[:text.index(old)].count("\n") + 1
    except (OSError, ValueError):
        pass
    a, b, shown, w = old.split("\n"), new.split("\n"), 0, width() - 12
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        rows = []
        if tag == "equal":
            keep = a[i1:i2]
            rows = [(start + i1 + k, " ", line, "") for k, line in enumerate(keep)]
            if len(rows) > 4:
                rows = rows[:2] + [(None, " ", "…", "")] + rows[-2:]
        else:
            rows = [(start + i1 + k, "-", line, RED_BG) for k, line in enumerate(a[i1:i2])]
            rows += [(start + j1 + k, "+", line, GREEN_BG) for k, line in enumerate(b[j1:j2])]
        for num, sign, line, bg in rows:
            if shown == 30:
                print(f"      {DIM}… more changes{RESET}")
                return
            num = f"{num:>4}" if num else "    "
            print(f"    {DIM}{num}{RESET} {bg}{sign} {line[:w]}{RESET}" if sign != " " else f"    {DIM}{num}   {line[:w]}{RESET}")
            shown += 1


def show_preview(name, args):
    if name == "edit" and isinstance(args.get("old"), str) and isinstance(args.get("new"), str):
        show_diff(args.get("path", ""), args["old"], args["new"])
    elif name == "write" and isinstance(args.get("content"), str):
        lines = args["content"].split("\n")
        verb = "Overwrite" if os.path.exists(str(args.get("path", ""))) else "Create"
        tree([f"{verb} with {len(lines)} lines"])
        for k, line in enumerate(lines[:8], 1):
            print(f"    {DIM}{k:>4}{RESET} {GREEN_BG}+ {line[:width() - 12]}{RESET}")
        if len(lines) > 8:
            print(f"         {DIM}… {len(lines) - 8} more lines{RESET}")


def show_result(name, result):
    if name == "todo":
        marks = {"done": f"{GREEN}☒{RESET} {DIM}{STRIKE}", "in_progress": f"{YELLOW}◼{RESET} {BOLD}"}
        for i, item in enumerate(todos):
            mark = marks.get(item.get("status"), f"{GREY}☐{RESET} ")
            print(f"  {DIM}{'⎿ ' if i == 0 else '  '}{RESET} {mark}{item.get('task')}{RESET}")
        return
    if result.startswith(("Error", "The user declined")):
        tree([result.split("\n")[0][:width() - 8]], RED)
        return
    if name == "bash":
        body, _, code = result.rpartition("\n[exit code ")
        lines = body.splitlines() if code else result.splitlines()
        shown = [line[:width() - 8] for line in lines[:4]] or ["(no output)"]
        if len(lines) > 4:
            shown.append(f"… +{len(lines) - 4} lines")
        tree(shown)
        if code and code.rstrip("]") != "0":
            print(f"     {RED}exit code {code.rstrip(']')}{RESET}")
    elif name == "read":
        n = sum(1 for line in result.splitlines() if re.match(r"\s*\d+\t", line))
        tree([f"Read {n} line{'s' if n != 1 else ''}"])
    elif name == "glob":
        n = 0 if result == "No files matched." else result.count("\n") + 1
        tree([f"Found {n} file{'s' if n != 1 else ''}"])
    else:
        tree([result.split("\n")[0]])


def allowed(name, args):
    if MODE == "auto" or (name == "bash" and read_only(str(args.get("command", "")))):
        return True
    try:
        answer = ask(f"  {YELLOW}?{RESET} Allow {BOLD}{TITLES[name]}{RESET}? "
                     f"{DIM}[y]es · [n]o · [a]uto mode from now on{RESET} ").strip().lower()
    except EOFError:
        return False
    if answer.startswith("a"):
        set_mode("auto")
        return True
    return answer in ("", "y", "yes")


def run_tool(name, raw_args, t0):
    """Never raise: whatever goes wrong becomes text the model can read and react to."""
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        show_call(name, {})
        result = f"Error: the arguments were not valid JSON ({e}). Send a JSON object."
        show_result(name, result)
        return result
    show_call(name, args)
    if name not in RUNNERS:
        result = f"Error: there is no tool named {name}. Available: {', '.join(RUNNERS)}."
    elif MODE == "plan" and name in ("write", "edit"):
        result = ("Error: plan mode is on, so files can't be changed yet. Finish exploring and reply with "
                  "your plan; the user approves it before you build.")
    else:
        show_preview(name, args)
        if name in NEEDS_PERMISSION and not allowed(name, args):
            result = "The user declined this action. Ask what they would like instead, or try another approach."
        else:
            if name == "bash":
                spinner.start("Running", t0)
            try:
                result = clip(str(RUNNERS[name](**args)))
            except Exception as e:      # bad arguments, missing file, permissions...
                result = f"Error: {type(e).__name__}: {e}"
            finally:
                spinner.stop()
    show_result(name, result)
    return result

# ----------------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------------

class Live:
    """Turns stream events into the spinner and rendered text."""

    def __init__(self, verb, t0, quiet=False, reading=0):
        self.verb, self.t0, self.quiet, self.md = verb, t0, quiet, Markdown()
        self.started, self.first, self.thought, self.wrote = time.time(), None, 0, 0
        spinner.chars, spinner.reading, spinner.preview = 0, reading, ""
        spinner.start(verb, t0)

    def mark(self):
        if self.first is None:
            self.first = time.time() - self.started
            spinner.reading = 0

    def text(self, s):
        self.mark()
        self.wrote += len(s)
        if self.quiet:
            return self.progress(s)
        spinner.stop()
        self.md.feed(s)

    def reasoning(self, s):
        self.mark()
        self.thought += len(s)
        self.md.flush()
        spinner.preview = re.sub(r"\s+", " ", spinner.preview + s)[-400:]
        spinner.start(self.verb, self.t0)
        self.progress(s)

    def tool(self, name):
        self.mark()
        self.md.flush()
        spinner.preview = ""
        spinner.start(f"{self.verb} · {TITLES.get(name, name)}", self.t0)

    def progress(self, s):
        self.mark()
        spinner.chars += len(s)

    def done(self):
        spinner.stop()
        self.md.flush()


client = {"key": None}
last_usage = [0]


def log_call(entry):
    """One line per model call in ~/.rootcode/log.jsonl, for working out why something was slow."""
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def trim_log():
    try:
        if os.path.getsize(LOG_PATH) > 1_000_000:
            with open(LOG_PATH, encoding="utf-8") as f:
                keep = f.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except OSError:
        pass


def call_model(messages, verb, t0, tools=True, quiet=False):
    estimate = (len(system_prompt()) + len(json.dumps(messages))) // 4
    live = Live(verb, t0, quiet, reading=estimate)
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": PROVIDER, "model": MODEL,
             "think": think_level(), "context_est": estimate}
    try:
        reply = chat(PROVIDER, client["key"], MODEL, system_prompt(), messages, TOOLS if tools else None,
                     live, think=think_level())
        entry.update(stop=reply.stop, tools=[c["name"] for c in reply.calls], usage=reply.usage)
    except BaseException as e:
        entry["error"] = "interrupted" if isinstance(e, KeyboardInterrupt) else str(e)[:200]
        raise
    finally:
        live.done()
        entry.update(first_token_s=round(live.first, 1) if live.first is not None else None,
                     total_s=round(time.time() - live.started, 1),
                     thinking_tokens=live.thought // 4, text_tokens=live.wrote // 4)
        log_call(entry)
    last_usage[0] = reply.usage or estimate
    return reply


def compact(messages, t0):
    """Replace a long history with a summary written for an agent that will resume the work."""
    request = messages + [{"role": "user", "content":
        "Stop and write a handoff summary for another agent that will resume this work with no other context. "
        "Include: the user's request in their words, what has been done, what remains, decisions made and why, "
        "and exact file paths, commands, error messages and identifiers that matter. Do not call tools."}]
    summary = call_model(request, "Compacting conversation", t0, tools=False, quiet=True).text
    plan = "\n".join(f"- [{i.get('status')}] {i.get('task')}" for i in todos)
    messages[:] = [{"role": "user", "content":
        f"Summary of the work so far:\n\n{summary}\n\nCurrent plan:\n{plan or '(none)'}\n\n"
        "Continue from here. Reread any file before editing it."}]
    print(f"{DIM}  ✻ Conversation compacted to save context{RESET}")


def looks_unfinished(text):
    """True when a reply that called no tool announces work instead of reporting it."""
    tail = (text or "").strip()[-250:].lower()
    if not tail:
        return True                     # thought, then said nothing
    if tail.endswith("?"):
        return False                    # a question for the user
    return tail.endswith(":") or bool(re.search(
        r"\b(let me (?!know)|i'll |i will |i'm going to |now i |next,? i )", tail.rsplit(". ", 2)[-1]))


def approve_plan(messages):
    """After a planning reply: build it (auto or approve mode), give feedback, or keep planning."""
    print(f"\n  {BOLD}Build this plan?{RESET}")
    print(f"  {DIM}1.{RESET} Yes, in auto mode {DIM}(no more prompts){RESET}")
    print(f"  {DIM}2.{RESET} Yes, approving each change")
    print(f"  {DIM}3.{RESET} No, keep planning")
    try:
        answer = ask(f"  {fg(75)}❯{RESET} {DIM}[1-3, or type what to change]{RESET} ").strip()
    except EOFError:
        answer = ""
    if answer in ("1", "2"):
        set_mode("auto" if answer == "1" else "approve")
        messages.append({"role": "user", "content":
            "The user approved the plan. Implement it now, step by step, and verify that it works."})
        return True
    if answer and answer != "3":        # feedback on the plan: keep planning with it
        messages.append({"role": "user", "content": PLAN_NOTE + answer})
        return True
    print(f"  {DIM}Still planning. Tell Root what to change, or press shift+tab to switch modes.{RESET}")
    return False


def run_turn(messages):
    """Call the model until it answers without asking for a tool."""
    t0, verb, nudges = time.time(), random.choice(VERBS), 0
    while True:
        reply = call_model(messages, verb, t0)
        if reply.stop == "length":
            # The reply was cut off, so any tool call in it may be incomplete. Drop the calls and nudge.
            if reply.text:
                messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user", "content":
                "Your last reply hit the output limit and was cut off. Continue, and write large files in smaller pieces."})
            continue
        message = {"role": "assistant", "content": reply.text or None}
        if reply.calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                     for c in reply.calls]
        if reply.reasoning_details:
            message["reasoning_details"] = [reply.reasoning_details[k] for k in sorted(reply.reasoning_details)]
        if reply.text or reply.calls:
            messages.append(message)
        if not reply.calls:
            if nudges < 2 and looks_unfinished(reply.text):
                nudges += 1
                print(f"{DIM}  ⎿ The model stopped mid-task; nudging it to continue{RESET}")
                messages.append({"role": "user", "content":
                    "You described your next step but did not call a tool, so nothing happened. "
                    "Do it now by calling the tool. If the task is fully done, reply with a short summary instead."})
                continue
            if MODE == "plan" and reply.text.strip() and approve_plan(messages):
                continue
            break
        answered = set()
        try:
            for call in reply.calls:
                result = run_tool(call["name"], call["arguments"], t0)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                answered.add(call["id"])
        except KeyboardInterrupt:
            spinner.stop()
        for call in reply.calls:        # every tool call must get a result, even after Ctrl+C
            if call["id"] not in answered:
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": "Interrupted by the user before this ran."})
        if len(answered) < len(reply.calls):
            print(f"\n{DIM}  ⎿ Interrupted · tell Root what to do instead{RESET}")
            return
        if last_usage[0] > compact_at():
            compact(messages, t0)
    secs = time.time() - t0
    took = f"{secs:.0f}s" if secs < 60 else f"{int(secs // 60)}m {int(secs % 60)}s"
    print(f"\n{ACCENT}✻{RESET} {DIM}Worked for {took} · {human(last_usage[0])} tokens in context{RESET}")

# ----------------------------------------------------------------------------
# /model: pick a provider, enter a key, pick a model, prove it answers, save it
# ----------------------------------------------------------------------------

def why(e):
    if getattr(e, "status", None) in (401, 403):
        return "the API key was rejected. Check it and run /model again."
    if getattr(e, "status", None) == 404:
        return "not found (check the model name or server address)."
    if "support tools" in str(e) or "tool" in str(e).lower() and getattr(e, "status", None) == 400:
        return ("this model can't use tools, and Root needs them to read, edit and run things. "
                "Pick a model with tool support. For an Ollama model imported from Hugging Face, see "
                "\"Models that can't use tools\" in the README: it's usually a missing chat template.")
    return str(e)


class Quiet:
    def text(self, s): pass
    def reasoning(self, s): pass
    def tool(self, name): pass
    def progress(self, s): pass


def choose_model():
    global PROVIDER, MODEL
    names = list(PROVIDERS)
    print(f"\n  {BOLD}Choose a provider{RESET}")
    for i, name in enumerate(names, 1):
        current = f"  {ACCENT}● current{RESET}" if name == PROVIDER else ""
        print(f"  {DIM}{i}.{RESET} {PROVIDERS[name]['label']}{current}")
    answer = ask(f"  {ACCENT}❯{RESET} {DIM}[1-{len(names)}, enter keeps current]{RESET} ").strip()
    provider = names[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(names) else PROVIDER

    if provider == "local":
        current = CONFIG.get("local_url", "http://localhost:1234/v1")
        entered = ask(f"  {BOLD}Server address{RESET} {DIM}(LM Studio :1234/v1, llama.cpp :8080/v1; "
                      f"enter keeps {current}){RESET} ").strip()
        CONFIG["local_url"] = (entered or current).rstrip("/")

    key, new_key = None, False
    if PROVIDERS[provider]["key_env"]:
        have = api_key(provider)
        optional = PROVIDERS[provider].get("key_optional")
        hint = f"enter keeps …{have[-4:]}" if have else ("optional, enter to skip" if optional else "typing is hidden")
        entered = getpass.getpass(f"  {BOLD}{PROVIDERS[provider]['label'].split(' (')[0]} API key{RESET} ({hint}): ").strip()
        key, new_key = entered or have, bool(entered)
        if not key and not optional:
            print(f"  {RED}A key is needed for {provider}.{RESET}")
            return

    spinner.start("Fetching models", time.time())
    try:
        models = list_models(provider, key)
    except Exception as e:
        spinner.stop()
        print(f"  {RED}Could not list models: {why(e)}{RESET}")
        return
    spinner.stop()
    if not models:
        hint = " Pull one first, e.g. ollama pull qwen3.6" if provider == "ollama" else ""
        print(f"  {RED}No models found.{hint}{RESET}")
        return

    toolless = without_tools(provider, models)
    shown, model = models, None
    print(f"\n  {BOLD}Choose a model{RESET} {DIM}({len(models)} available){RESET}")
    while model is None:
        for i, name in enumerate(shown[:30], 1):
            current = f"  {ACCENT}● current{RESET}" if name == MODEL and provider == PROVIDER else ""
            flag_ = f"  {YELLOW}no tools{RESET}" if name in toolless else ""
            print(f"  {DIM}{i:>2}.{RESET} {name}{current}{flag_}")
        if len(shown) > 30:
            print(f"  {DIM}    … {len(shown) - 30} more: type part of a name to narrow the list{RESET}")
        answer = ask(f"  {ACCENT}❯{RESET} {DIM}[number, name or search; enter cancels]{RESET} ").strip()
        if not answer:
            print(f"  {DIM}Cancelled, nothing changed.{RESET}")
            return
        if answer.isdigit() and 1 <= int(answer) <= min(30, len(shown)):
            model = shown[int(answer) - 1]
        elif answer in models:
            model = answer
        else:
            narrowed = [m for m in models if answer.lower() in m.lower()]
            if narrowed:
                shown = narrowed
            else:
                print(f"  {RED}No model matches {answer!r}.{RESET}")

    spinner.start(f"Checking {model}", time.time())
    try:
        reply = chat(provider, key, model, "Reply with just: OK", [{"role": "user", "content": "Say OK"}],
                     [t for t in TOOLS if t["name"] == "read"], Quiet(), max_tokens=300)
    except Exception as e:
        spinner.stop()
        print(f"  {RED}✗ That didn't work, nothing was changed: {why(e)}{RESET}")
        return
    spinner.stop()
    PROVIDER, MODEL, client["key"] = provider, model, key
    CONFIG["provider"], CONFIG["model"] = provider, model
    if new_key:
        CONFIG.setdefault("keys", {})[provider] = key
    save_config()
    said = reply.text.strip().replace("\n", " ")[:40]
    print(f"  {GREEN}✓{RESET} {BOLD}{model}{RESET} via {PROVIDERS[provider]['label']} "
          f"{DIM}· answered “{said or '…'}” · saved{RESET}")

# ----------------------------------------------------------------------------
# Sessions: one SQLite table in ~/.rootcode/sessions.db, saved after every turn
# ----------------------------------------------------------------------------

def ago(t):
    secs = time.time() - t
    if secs < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"


def first_prompt(messages):
    for m in messages:
        if m["role"] == "user" and isinstance(m.get("content"), str):
            text = m["content"][len(PLAN_NOTE):] if m["content"].startswith(PLAN_NOTE) else m["content"]
            return text.strip().split("\n")[0][:70]
    return "(untitled)"


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
        if not self.db or not messages:
            return
        turns = sum(1 for m in messages if m["role"] == "user" and isinstance(m.get("content"), str))
        row = (time.time(), turns, PROVIDER, MODEL, json.dumps(messages), json.dumps(todos))
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
        messages[:], todos[:] = json.loads(data), json.loads(plan or "[]")
        self.id = sid
        last_usage[0] = len(json.dumps(messages)) // 4
        if cwd != os.getcwd() and os.path.isdir(cwd):
            os.chdir(cwd)                   # tools use relative paths: go back to the session's folder
            print(f"  {DIM}✻ Moved to {tidy_path(cwd)}, where this session ran{RESET}")
        recap(messages)


def recap(messages):
    """Show where a resumed conversation left off."""
    asked = next((m["content"] for m in reversed(messages)
                  if m["role"] == "user" and isinstance(m.get("content"), str)), "")
    said = next((m["content"] for m in reversed(messages) if m["role"] == "assistant" and m.get("content")), "")
    asked = asked[len(PLAN_NOTE):] if asked.startswith(PLAN_NOTE) else asked
    prompts = sum(1 for m in messages if m["role"] == "user" and isinstance(m.get("content"), str))
    print(f"\n  {GREY}Resumed · {prompts} prompt{'s' if prompts != 1 else ''}{RESET}")
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

# ----------------------------------------------------------------------------
# The screen
# ----------------------------------------------------------------------------

COMMANDS = {"/help": "show this help",
            "/model": "switch provider, API key or model",
            "/mode": "cycle approve → auto → plan (or press shift+tab)",
            "/plan": "plan mode: explore read-only, then propose a plan",
            "/approve": "approve mode: ask before commands and file changes",
            "/auto": "auto mode: run commands and edits without asking",
            "/think": "how hard the model thinks: off, low, medium, high, default",
            "/resume": "continue a saved session (add words to search titles)",
            "/clear": "start a new conversation (the current one stays saved)",
            "/compact": "summarize the conversation to free context",
            "/init": "write ROOT.md: notes on this project for future sessions",
            "/exit": "quit (also /quit, ctrl+d)", "/quit": "quit"}
COMMAND_ARGS = {"/think": {"off": "fastest: no thinking (best for local models)", "low": "a little thinking",
                           "medium": "balanced", "high": "think hardest; slowest",
                           "default": "whatever the model does by default"}}
INIT_PROMPT = ("Look through this project and write a ROOT.md file at its root, for future coding sessions: what the "
               "project is, how to install, build, run and test it, where the important code lives, and conventions "
               "to follow. Keep it under 80 lines and only include what you verified. If ROOT.md exists, improve it.")


def banner():
    print()
    if TTY and width() >= 42:
        for row, shade in zip(LOGO, LOGO_SHADES):
            print(f"  {fg(shade)}{row}{RESET}")
    else:
        print(f"  {BOLD}{ACCENT}ROOT{RESET}")
    print(f"  {DIM}lightweight coding agent · v{VERSION}{RESET}\n")
    print(f"  {GREY}model{RESET}  {BOLD}{MODEL or '(none)'}{RESET} {DIM}via {PROVIDERS[PROVIDER]['label']}{RESET}")
    print(f"  {GREY}cwd{RESET}    {tidy_path(os.getcwd())}")
    colour, label, about = MODE_LOOK[MODE]
    print(f"  {GREY}mode{RESET}   {colour}{label}{RESET} {DIM}· {about}{RESET}")
    print(f"\n  {DIM}shift+tab switches modes · /resume continues a past session · /help for commands{RESET}")


def rule_text(note=""):
    pct = min(99, int(100 * last_usage[0] / max(compact_at(), 1)))
    colour, label, _ = MODE_LOOK[MODE]
    w = min(width(), 120) - 1           # one column spare: a full-width line can wrap in some terminals
    extra = f" · {PROVIDER}" + (f" · {pct}% context" if pct >= 1 else "") \
        + (f" · think {think_level()}" if think_level() != "default" else "")
    model = MODEL.split("/")[-1] if len(MODEL) > 24 else MODEL      # hf.co/org/name:tag → name:tag
    room = max(w - len(label) - len(extra) - 12, 8)
    model = model if len(model) <= room else model[:room - 1] + "…"
    info = f" {model}{extra} "
    if len(info) + len(label) + 10 > w:  # a very narrow terminal: just the mode and the model
        info = f" {model[:max(w - len(label) - 12, 1)]} "
    note = f" {note[:max(w - len(info) - len(label) - 12, 0)]} " if note else ""
    left = max(w - len(info) - len(label) - len(note) - 6, 1)
    return f"{DIM}──{RESET} {colour}{label}{RESET}{GREY}{note}{RESET} {DIM}{'─' * left}{info}──{RESET}"


class Prompt:
    """Root's input line: its status bar and text are redrawn together on every key, so switching modes
    happens in place. Handles history, arrows, word moves, multi-line paste, tab completion of /commands,
    shift+tab for modes and ctrl+c to clear. Falls back to input() where there is no terminal."""

    def __init__(self):
        self.history, self.drawn, self.buf, self.pos, self.note = [], 0, "", 0, ""
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                self.history = [json.loads(line) for line in f][-500:]
        except (OSError, ValueError):
            pass

    def remember(self, text):
        if not text or (self.history and self.history[-1] == text):
            return
        self.history.append(text)
        try:
            os.makedirs(HOME_DIR, exist_ok=True)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(text) + "\n")
        except OSError:
            pass

    def read(self):
        if not (termios and TTY and sys.stdin.isatty()):
            print("\n" + rule_text())
            lines = [input(f"{BOLD}{MODE_LOOK[MODE][0]}❯{RESET} ")]
            while lines[-1].endswith("\\"):
                lines[-1] = lines[-1][:-1]
                lines.append(input(f"{DIM}…{RESET} "))
            return "\n".join(lines).strip()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self.buf, self.pos, self.note, self.drawn, self.spot, self.draft = "", 0, "", 0, len(self.history), ""
        self.sel, self.hidden, self.final = 0, False, False
        armed = False                   # ctrl+c once clears; again on an empty line quits
        sys.stdout.write("\n\033[?2004h")               # bracketed paste: a paste arrives as one block
        try:
            tty.setraw(fd)
            self.draw()
            while True:
                key = self.key(fd)
                if key == "\x03":
                    if self.buf:
                        self.buf, self.pos, armed = "", 0, False
                    elif armed:
                        raise KeyboardInterrupt
                    else:
                        armed, self.note = True, "· press ctrl+c again to quit"
                    self.draw()
                    continue
                armed, self.note = False, ""
                if self.handle(key):
                    break
                self.draw()
            text = self.buf.strip()
            self.remember(text)
            return text
        finally:
            self.pos, self.note, self.final = len(self.buf), "", True
            self.draw()
            sys.stdout.write("\r\n\033[?2004l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def key(self, fd):
        byte = os.read(fd, 1)
        if byte == b"\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "esc"
            kind = os.read(fd, 1)
            if kind == b"O":
                return "esc O" + os.read(fd, 1).decode("latin-1")
            if kind != b"[":
                return "alt " + kind.decode("latin-1")
            seq = b""
            while not seq or not 0x40 <= seq[-1] <= 0x7E:
                seq += os.read(fd, 1)
            if seq == b"200~":           # pasted text, up to the end-of-paste marker
                data = b""
                while not data.endswith(b"\x1b[201~"):
                    data += os.read(fd, 4096)
                text = data[:-6].decode("utf-8", "replace")
                return ("paste", text.replace("\r\n", "\n").replace("\r", "\n"))
            return "esc [" + seq.decode("latin-1")
        if byte[0] >= 0xC0:              # the rest of a multi-byte character
            need = 1 if byte[0] < 0xE0 else 2 if byte[0] < 0xF0 else 3
            while len(byte) < need + 1:
                byte += os.read(fd, need + 1 - len(byte))
        return byte.decode("utf-8", "replace")

    def insert(self, text):
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)

    def word_left(self):
        i = self.pos
        while i > 0 and not self.buf[i - 1].isalnum():
            i -= 1
        while i > 0 and self.buf[i - 1].isalnum():
            i -= 1
        return i

    def word_right(self):
        i = self.pos
        while i < len(self.buf) and not self.buf[i].isalnum():
            i += 1
        while i < len(self.buf) and self.buf[i].isalnum():
            i += 1
        return i

    def menu(self):
        """Suggestions for what is being typed: /commands, then the arguments a command takes."""
        if self.final or self.hidden or not self.buf.startswith("/") or "\n" in self.buf:
            return []
        head, space, rest = self.buf.partition(" ")
        if not space:
            names = [c for c in COMMANDS if c != "/quit" or head.startswith("/q")]
            starts = [c for c in names if c.startswith(head)]
            inside = [c for c in names if len(head) > 1 and head[1:] in c[1:] and c not in starts]
            return [(c, COMMANDS[c]) for c in starts + inside]
        if head in COMMAND_ARGS and " " not in rest:
            return [(f"{head} {arg}", about) for arg, about in COMMAND_ARGS[head].items() if arg.startswith(rest)]
        return []

    def handle(self, key):
        """Apply one key. Returns True when the line is submitted."""
        items = self.menu()
        if items:
            self.sel = min(self.sel, len(items) - 1)
            pick = items[self.sel][0]
            if key in ("esc [A", "esc OA", "esc [B", "esc OB"):
                step = -1 if key.endswith("A") else 1
                self.sel = (self.sel + step) % len(items)
                return False
            if key in ("\t", "esc [C", "esc OC") and (key == "\t" or self.pos == len(self.buf)):
                self.buf = pick + (" " if key == "\t" else "")
                self.pos, self.sel = len(self.buf), 0
                return False
            if key in ("\r", "\n"):
                self.buf = pick
                return True
            if key == "esc":
                self.hidden = True
                return False
        before = self.buf
        submitted = self._handle(key)
        if self.buf != before:          # typing changes the suggestions: start again at the top
            self.sel, self.hidden = 0, False
        return submitted

    def _handle(self, key):
        if isinstance(key, tuple):                       # a paste
            self.insert(key[1])
        elif key in ("\r", "\n"):
            if not self.buf.endswith("\\"):
                return True
            self.buf, self.pos = self.buf[:-1] + "\n", len(self.buf)   # \ at the end: new line
        elif key == "\x04":
            if not self.buf:
                raise EOFError
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif key in ("\x7f", "\x08"):
            if self.pos:
                self.buf, self.pos = self.buf[:self.pos - 1] + self.buf[self.pos:], self.pos - 1
        elif key == "esc [3~":
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif key in ("esc [A", "esc OA", "\x10", "esc [B", "esc OB", "\x0e"):
            up = key in ("esc [A", "esc OA", "\x10")
            if self.spot == len(self.history):
                self.draft = self.buf
            self.spot = max(0, self.spot - 1) if up else min(len(self.history), self.spot + 1)
            self.buf = self.history[self.spot] if self.spot < len(self.history) else self.draft
            self.pos = len(self.buf)
        elif key in ("esc [D", "esc OD", "\x02"):
            self.pos = max(0, self.pos - 1)
        elif key in ("esc [C", "esc OC", "\x06"):
            self.pos = min(len(self.buf), self.pos + 1)
        elif key in ("esc [H", "esc OH", "esc [1~", "\x01"):
            self.pos = 0
        elif key in ("esc [F", "esc OF", "esc [4~", "\x05"):
            self.pos = len(self.buf)
        elif key in ("alt b", "esc [1;3D", "esc [1;5D", "esc [1;9D"):
            self.pos = self.word_left()
        elif key in ("alt f", "esc [1;3C", "esc [1;5C", "esc [1;9C"):
            self.pos = self.word_right()
        elif key in ("\x17", "alt \x7f"):
            start = self.word_left()
            self.buf, self.pos = self.buf[:start] + self.buf[self.pos:], start
        elif key == "\x15":
            self.buf, self.pos = self.buf[self.pos:], 0
        elif key == "\x0b":
            self.buf = self.buf[:self.pos]
        elif key == "\x0c":
            sys.stdout.write("\033[H\033[2J")
            self.drawn = 0
        elif key == "esc [Z":                            # shift+tab
            set_mode(MODE_ORDER[(MODE_ORDER.index(MODE) + 1) % len(MODE_ORDER)], quiet=True)
            self.note = "· " + MODE_LOOK[MODE][2]
        elif isinstance(key, str) and len(key) == 1 and key >= " ":
            self.insert(key)
        return False

    def draw(self):
        """Redraw the status bar and the input below it, then put the cursor where it belongs."""
        w = max(width(), 20)

        def place(text):                 # where the terminal cursor ends up after writing "❯ " + text
            row, col = 0, 2
            for ch in text:
                if ch == "\n":
                    row, col = row + 1, 2
                    continue
                if col >= w:
                    row, col = row + 1, 0
                col += 1
            return row, min(col, w - 1)

        shown = self.buf
        word = shown.split(" ")[0]
        if word in COMMANDS:             # a known /command lights up
            shown = ACCENT + word + RESET + shown[len(word):]
        items = self.menu()
        self.sel = min(self.sel, max(len(items) - 1, 0))
        ghost = ""                       # the rest of the highlighted suggestion, in grey
        if items and self.pos == len(self.buf) and items[self.sel][0].startswith(self.buf):
            ghost = items[self.sel][0][len(self.buf):]
        out = "\r" + (f"\033[{self.drawn}A" if self.drawn else "") + "\033[J"
        out += rule_text(self.note) + "\r\n" + f"{BOLD}{MODE_LOOK[MODE][0]}❯{RESET} " + shown.replace("\n", "\r\n  ")
        out += f"{GREY}{ghost}{RESET}" if ghost else ""
        end_row, _ = place(self.buf + ghost)
        row, col = place(self.buf[:self.pos])
        below = end_row - row
        top = max(0, self.sel - 7)                       # keep the highlighted line in view
        for i, (name, about) in enumerate(items[top:top + 8], top):
            label = name.split(" ", 1)[1] if " " in name else name
            mark = f"{ACCENT}›{RESET} {BOLD}{ACCENT}" if i == self.sel else f"  {GREY}"
            out += f"\r\n  {mark}{label:<10}{RESET} {DIM}{about[:max(w - 18, 0)]}{RESET}"
            below += 1
        if len(items) > top + 8:
            out += f"\r\n    {DIM}… {len(items) - top - 8} more{RESET}"
            below += 1
        if below:
            out += f"\033[{below}A"
        out += "\r" + (f"\033[{col}C" if col else "")
        self.drawn = row + 1
        sys.stdout.write(out)
        sys.stdout.flush()


def main():
    global MODEL
    client["key"] = api_key(PROVIDER)
    if not MODEL and PROVIDER == "ollama":     # no model chosen yet: use the first one installed
        try:
            MODEL = (list_models("ollama", None) or [""])[0]
        except Exception:
            pass
    task = " ".join(a for a in sys.argv[1:] if not a.startswith("--") and a != "-c")
    messages, sessions, line = [], Sessions(), Prompt()
    trim_log()
    first_run = not os.path.exists(CONFIG_PATH) and not task and not flag("provider") \
        and not any(f"--{p}" in sys.argv for p in PROVIDERS) and not os.environ.get("ROOTCODE_PROVIDER")
    banner()
    if first_run and sys.stdin.isatty():
        print(f"\n  {ACCENT}Welcome to Root.{RESET} Let's connect a model.")
        try:
            choose_model()
        except (EOFError, KeyboardInterrupt):
            print()
    if "--continue" in sys.argv or "-c" in sys.argv:
        sid = sessions.latest_here()
        if sid:
            sessions.load(sid, messages)
        else:
            print(f"\n  {DIM}No saved session in this folder yet; starting a new one.{RESET}")
    elif "--resume" in sys.argv:
        try:
            pick_session(sessions, messages)
        except (EOFError, KeyboardInterrupt):
            print()
    try:
        while True:
            if task:
                prompt, one_shot = task, True
            else:
                one_shot = False
                try:
                    prompt = line.read()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
            if not prompt:
                continue
            command = prompt.split()[0] if prompt.startswith("/") else None
            if command in ("/exit", "/quit"):
                return
            if command == "/init":
                prompt, command = INIT_PROMPT, None
            if command:
                if command == "/clear":
                    messages.clear()
                    todos.clear()
                    last_usage[0], sessions.id = 0, None
                    print(f"  {DIM}✻ New conversation · the last one is saved, /resume brings it back{RESET}")
                elif command == "/resume":
                    try:
                        pick_session(sessions, messages, prompt[len("/resume"):].strip())
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n  {DIM}Cancelled.{RESET}")
                elif command == "/model":
                    try:
                        choose_model()
                    except (EOFError, KeyboardInterrupt):
                        spinner.stop()
                        print(f"\n  {DIM}Cancelled, nothing changed.{RESET}")
                elif command == "/think":
                    level = prompt.split()[1].lower() if len(prompt.split()) > 1 else ""
                    if level in THINK_LEVELS:
                        CONFIG["think"] = level
                        save_config()
                        note = " (Anthropic models ignore this)" if PROVIDER == "anthropic" else ""
                        print(f"  {DIM}✻ Thinking set to {level}{note}{RESET}")
                    else:
                        print(f"  {DIM}Thinking is {think_level()}. Use /think off · low · medium · high · default. "
                              f"off is fastest on local models.{RESET}")
                elif command in ("/plan", "/approve", "/auto", "/yolo"):
                    set_mode("auto" if command == "/yolo" else command[1:])
                elif command == "/mode":
                    set_mode(MODE_ORDER[(MODE_ORDER.index(MODE) + 1) % len(MODE_ORDER)])
                elif command == "/compact" and messages:
                    try:
                        compact(messages, time.time())
                        sessions.save(messages)
                    except (ApiError, KeyboardInterrupt) as e:
                        print(f"  {RED}Could not compact: {why(e)}{RESET}")
                elif command == "/compact":
                    print(f"  {DIM}Nothing to compact yet.{RESET}")
                else:
                    for name, text in COMMANDS.items():
                        if name != "/quit":
                            print(f"  {ACCENT}{name:<10}{RESET} {DIM}{text}{RESET}")
                    print(f"  {DIM}ctrl+c interrupts · ctrl+d quits · ↑ recalls earlier prompts{RESET}")
                if one_shot:
                    return
                continue
            if PROVIDERS[PROVIDER]["key_env"] and not PROVIDERS[PROVIDER].get("key_optional") and not client["key"]:
                print(f"  {RED}No {PROVIDERS[PROVIDER]['label']} API key yet. Run /model to add one.{RESET}")
                if one_shot:
                    return
                continue

            if not one_shot:
                print()
            messages.append({"role": "user", "content": (PLAN_NOTE if MODE == "plan" else "") + prompt})
            try:
                run_turn(messages)
            except KeyboardInterrupt:           # interrupted while the model was writing
                spinner.stop()
                print(f"\n{DIM}  ⎿ Interrupted · tell Root what to do instead{RESET}")
            except ApiError as e:               # retries are exhausted or the request is invalid
                spinner.stop()
                print(f"\n  {RED}✗ {why(e)}{RESET}")
            except (ValueError, KeyError) as e:  # a malformed stream from a provider
                spinner.stop()
                print(f"\n  {RED}✗ Unexpected reply from {PROVIDER}: {e}{RESET}")
            sessions.save(messages)
            if one_shot:
                return
    finally:
        spinner.stop()


if __name__ == "__main__":
    main()
