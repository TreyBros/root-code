"""Tools: few and powerful. Every tool returns text, including its errors.

Add a tool by decorating a function with @tool. Its keyword arguments are the tool's parameters:

    @tool("word_count", "Count", "Count the words in a file.", {"path": {"type": "string"}}, ["path"])
    def word_count(path):
        with open(path) as f:
            return f"{len(f.read().split())} words"

asks=True means approve mode asks before it runs; edits=True means it changes files, so plan mode
refuses it. A tool with neither runs freely in every mode, so only mark a tool that way if it only reads.
"""

import difflib
import glob as globlib
import json
import os
import re
import subprocess

from .modes import read_only, set_mode
from .providers import LOCAL
from .settings import MAX_TOOL_CHARS, MAX_TOOL_CHARS_LOCAL, state
from .ui import (ACCENT, BOLD, DIM, GREEN, GREEN_BG, GREY, RED, RED_BG, RESET, STRIKE, YELLOW, ask,
                 spinner, tree, width)


class Tool:
    def __init__(self, name, title, description, parameters, run, asks, edits, enabled):
        self.name, self.title, self.description, self.parameters = name, title, description, parameters
        self.run, self.asks, self.edits, self.enabled = run, asks, edits, enabled

    def spec(self):
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


TOOLS = {}


def schema(props, required):
    return {"type": "object", "properties": props, "required": list(required)}


def tool(name, title, description, properties, required=(), asks=False, edits=False, enabled=None):
    """Register a function as a tool the model can call. `enabled` hides it when it returns False."""
    def register(fn):
        TOOLS[name] = Tool(name, title, description, schema(properties, required), fn, asks, edits, enabled)
        return fn
    return register


def specs():
    """The tools to offer the model right now."""
    return [t.spec() for t in TOOLS.values() if t.enabled is None or t.enabled()]


def title(name):
    return TOOLS[name].title if name in TOOLS else name


def clip(text):
    """Keep tool output small: the head and the tail matter, the middle rarely does."""
    limit = MAX_TOOL_CHARS_LOCAL if state.provider in LOCAL else MAX_TOOL_CHARS
    if len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half] + f"\n\n... [{len(text) - limit} characters cut; "
            "narrow the command or read a specific range to see more] ...\n\n" + text[-half:])

# ----------------------------------------------------------------------------
# The built-in tools
# ----------------------------------------------------------------------------

@tool("bash", "Bash",
      "Run a shell command in the working directory and return its combined output and exit code. "
      "Use it to search (rg, grep, find), run tests and builds, and use git. Commands are killed after "
      "the timeout, so start servers and other long-running jobs in the background "
      "(for example with nohup ... &) and check on them later.",
      {"command": {"type": "string"},
       "timeout": {"type": "integer", "description": "Seconds before the command is killed. Default 120."}},
      ["command"], asks=True)
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


@tool("read", "Read", "Read a text file with line numbers. Use offset and limit for large files.",
      {"path": {"type": "string"},
       "offset": {"type": "integer", "description": "First line to read, starting at 1."},
       "limit": {"type": "integer", "description": "Number of lines to read. Default 2000."}},
      ["path"])
def tool_read(path, offset=1, limit=2000):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start = max(offset, 1) - 1
    chunk = lines[start:start + limit]
    body = "".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(chunk))
    rest = len(lines) - (start + len(chunk))
    return (body or "[file is empty]") + (f"\n[{rest} more lines; read again with offset]" if rest > 0 else "")


@tool("write", "Write", "Create a file or replace its whole content. Prefer edit for changes to existing files.",
      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"], asks=True, edits=True)
def tool_write(path, content):
    folder = os.path.dirname(os.path.abspath(path))
    inside = os.path.abspath(path).startswith(os.getcwd() + os.sep)
    if not os.path.isdir(folder) and not inside:
        return f"Error: the folder {folder} does not exist. Check the path; prefer paths relative to the working directory."
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {content.count(chr(10)) + 1} lines to {path}"


@tool("edit", "Update",
      "Replace one exact string in a file. old must match the file exactly, including whitespace, "
      "and must occur once; include surrounding lines to make it unique, or set replace_all.",
      {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
       "replace_all": {"type": "boolean"}}, ["path", "old", "new"], asks=True, edits=True)
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


@tool("glob", "Search", "List files matching a glob pattern such as src/**/*.py, newest first.",
      {"pattern": {"type": "string"}}, ["pattern"])
def tool_glob(pattern):
    paths = [p for p in globlib.glob(pattern, recursive=True) if os.path.isfile(p)]
    paths.sort(key=os.path.getmtime, reverse=True)
    return "\n".join(paths[:500]) or "No files matched."


@tool("todo", "Plan", "Record or update your plan. Send the full list every time. Statuses: pending, in_progress, done.",
      {"items": {"type": "array", "items": schema({"task": {"type": "string"}, "status": {"type": "string"}},
                                                  ["task", "status"])}}, ["items"])
def tool_todo(items):
    state.todos[:] = items
    return "Plan saved."

# ----------------------------------------------------------------------------
# Showing tool calls, and running them
# ----------------------------------------------------------------------------

def show_call(name, args):
    label = args.get("command") or args.get("path") or args.get("pattern") or args.get("name") or ""
    label = str(label).strip().split("\n")[0]
    room = max(width() - len(title(name)) - 8, 20)
    label = label if len(label) <= room else label[:room - 1] + "…"
    print(f"\n{ACCENT}⏺{RESET} {BOLD}{title(name)}{RESET}" + (f"({label})" if label else ""))


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
        if tag == "equal":
            rows = [(start + i1 + k, " ", line, "") for k, line in enumerate(a[i1:i2])]
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
        for i, item in enumerate(state.todos):
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
        tree([result.split("\n")[0][:width() - 8]])


def allowed(name, args):
    if state.mode == "auto" or (name == "bash" and read_only(str(args.get("command", "")))):
        return True
    try:
        answer = ask(f"  {YELLOW}?{RESET} Allow {BOLD}{title(name)}{RESET}? "
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
    found = TOOLS.get(name)
    if not found or (found.enabled and not found.enabled()):
        result = f"Error: there is no tool named {name}. Available: {', '.join(t['name'] for t in specs())}."
    elif state.mode == "plan" and found.edits:
        result = ("Error: plan mode is on, so files can't be changed yet. Finish exploring and reply with "
                  "your plan; the user approves it before you build.")
    else:
        show_preview(name, args)
        if found.asks and not allowed(name, args):
            result = "The user declined this action. Ask what they would like instead, or try another approach."
        else:
            if name == "bash":
                spinner.start("Running", t0)
            try:
                result = clip(str(found.run(**args)))
            except Exception as e:      # bad arguments, missing file, permissions...
                result = f"Error: {type(e).__name__}: {e}"
            finally:
                spinner.stop()
    show_result(name, result)
    return result
