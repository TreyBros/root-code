"""Look: 256-colour ANSI (off when piped or NO_COLOR is set), the spinner, and markdown rendering."""

import os
import re
import shutil
import sys
import textwrap
import threading
import time

from .settings import state

TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def sgr(code):
    return f"\033[{code}m" if TTY else ""


def fg(n):
    return sgr(f"38;5;{n}")


RESET, BOLD, DIM, STRIKE = sgr(0), sgr(1), sgr(2), sgr(9)
ACCENT, RED, GREEN, YELLOW, GREY, CODE = fg(42), fg(203), fg(114), fg(179), fg(245), fg(180)
BLUE, PURPLE = fg(75), fg(177)
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


def tree(lines, colour=GREY):
    """Lines hanging under the tool call above them."""
    for i, line in enumerate(lines):
        print(f"  {DIM}{'⎿ ' if i == 0 else '  '}{RESET} {colour}{line}{RESET}")


class Spinner:
    """An animated status line on its own thread: frame, shimmering label, timer, tokens."""
    FRAMES = "·✢✳✶✻✽✻✶✳✢"

    def __init__(self):
        self.on, self.thread, self.lock = False, None, threading.Lock()
        self.label, self.t0, self.chars, self.reading, self.preview = "", time.time(), 0, 0, ""
        self.flash = ("", "", 0.0)     # (text, colour, until): e.g. the mode just switched to

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
        text, colour, until = self.flash
        if until > time.time() and room > 12:
            return line + f" {colour}{text[:room]}{RESET}"
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
            text = re.sub(r"(`[^`]+`|\*\*[^*]+\*\*)", lambda m: m[1].replace(" ", " "), text)
            body = text.lstrip()
            hang = len(text) - len(body) + (2 if body.startswith("• ") else len(body.split(" ")[0]) + 1
                                             if re.match(r"\d+\. ", body) else 0)
            pieces = textwrap.wrap(text, max(width() - 4, 20), subsequent_indent=" " * hang,
                                   break_on_hyphens=False) or [""]
            for k, piece in enumerate(pieces):
                piece = re.sub(r"`([^`]+)`", lambda m: CODE + m[1] + RESET, piece.replace(" ", " "))
                piece = re.sub(r"\*\*([^*]+)\*\*", lambda m: BOLD + m[1] + RESET, piece)
                print(("  " if k else prefix) + piece, flush=True)
            return
        print(prefix + out, flush=True)


def ask(prompt):
    """A short answer to a question: a number, y/n, a key."""
    keys = state.keys
    listening = keys is not None and keys.on
    if listening:
        keys.stop()                     # input() needs the keyboard while the question is open
    try:
        return input(prompt)
    finally:
        if listening:
            keys.start()
