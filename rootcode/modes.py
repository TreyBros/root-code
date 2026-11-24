"""Modes: approve (ask first), auto (just do it), plan (read-only, then propose). And which
shell commands only look, so they can run without asking."""

import re

from .settings import state
from .ui import BLUE, DIM, GREEN, RESET, YELLOW

ORDER = ("approve", "auto", "plan")                 # shift+tab cycles in this order
LOOK = {"approve": (GREEN, "approve mode", "asks before commands and file changes"),
        "auto":    (YELLOW, "⏵⏵ auto mode", "runs commands and edits without asking"),
        "plan":    (BLUE, "⏸ plan mode", "read-only: explores, then proposes a plan")}
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
    state.mode = mode
    if not quiet:
        colour, label, about = LOOK[mode]
        print(f"  {colour}{label}{RESET} {DIM}· {about}{RESET}")


def next_mode():
    return ORDER[(ORDER.index(state.mode) + 1) % len(ORDER)]


def strip_plan_note(text):
    return text[len(PLAN_NOTE):] if text.startswith(PLAN_NOTE) else text
