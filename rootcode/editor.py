"""Root's input line, the / menu, and the key listener that runs while the model works.

Root reads keys itself in raw mode instead of using readline: macOS's libedit lags a key behind on
key bindings and ignores some arrow sequences, and redrawing everything on each key is what lets
shift+tab change the mode in place.
"""

import json
import os
import sys
import threading
import time

from .commands import COMMANDS
from .modes import LOOK, next_mode, set_mode
from .screen import rule_text
from .settings import HISTORY_PATH, HOME_DIR, state
from .ui import ACCENT, BOLD, DIM, GREY, RESET, TTY, spinner, width

try:
    import select
    import termios
    import tty
except ImportError:                     # Windows: plain input() instead of Root's own input line
    termios = None


def interactive():
    return bool(termios and TTY and sys.stdin.isatty())


class Prompt:
    """The status bar and the text below it, redrawn together on every key. Handles history, arrows,
    word moves, multi-line paste, the / menu, shift+tab for modes and ctrl+c to clear."""

    def __init__(self):
        self.history, self.drawn, self.buf, self.pos, self.note, self.private = [], 0, "", 0, "", []
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                self.history = [json.loads(line) for line in f][-500:]
        except (OSError, ValueError):
            pass

    def remember(self, text):
        if not text or (self.history and self.history[-1] == text):
            return
        self.history.append(text)
        if state.incognito:                 # ↑ still works this session; nothing reaches the file
            self.private.append(text)
            return
        try:
            os.makedirs(HOME_DIR, exist_ok=True)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(text) + "\n")
        except OSError:
            pass

    def forget_private(self):
        """Drop what was typed while incognito."""
        self.history = [h for h in self.history if h not in self.private]
        self.private.clear()

    def read(self, typed=""):
        if not interactive():
            print("\n" + rule_text())
            lines = [input(f"{BOLD}{LOOK[state.mode][0]}❯{RESET} ")]
            while lines[-1].endswith("\\"):
                lines[-1] = lines[-1][:-1]
                lines.append(input(f"{DIM}…{RESET} "))
            return "\n".join(lines).strip()
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self.buf, self.pos, self.note, self.drawn, self.spot, self.draft = typed, len(typed), "", 0, len(self.history), ""
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
        if getattr(self, "final", False) or getattr(self, "hidden", False) \
                or not self.buf.startswith("/") or "\n" in self.buf:
            return []
        head, space, rest = self.buf.partition(" ")
        if not space:
            names = [c.name for c in COMMANDS.values() if not c.hidden or (len(head) > 1 and c.name.startswith(head))]
            starts = [c for c in names if c.startswith(head)]
            inside = [c for c in names if len(head) > 1 and head[1:] in c[1:] and c not in starts]
            return [(c, COMMANDS[c].about) for c in starts + inside]
        if head in COMMANDS and COMMANDS[head].args and " " not in rest:
            return [(f"{head} {arg}", about) for arg, about in COMMANDS[head].args.items() if arg.startswith(rest)]
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
            set_mode(next_mode(), quiet=True)
            self.note = "· " + LOOK[state.mode][2]
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
        out += rule_text(self.note) + "\r\n" + f"{BOLD}{LOOK[state.mode][0]}❯{RESET} " + shown.replace("\n", "\r\n  ")
        out += f"{GREY}{ghost}{RESET}" if ghost else ""
        end_row, _ = place(self.buf + ghost)
        row, col = place(self.buf[:self.pos])
        below = end_row - row
        top = max(0, self.sel - 7)                       # keep the highlighted line in view
        pad = max([10] + [len(n.split(" ", 1)[-1]) for n, _ in items[top:top + 8]])
        for i, (name, about) in enumerate(items[top:top + 8], top):
            label = name.split(" ", 1)[1] if " " in name else name
            mark = f"{ACCENT}›{RESET} {BOLD}{ACCENT}" if i == self.sel else f"  {GREY}"
            out += f"\r\n  {mark}{label:<{pad}}{RESET} {DIM}{about[:max(w - pad - 8, 0)]}{RESET}"
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


class Keys:
    """While the model works: shift+tab switches modes right away, and anything else typed is kept for
    the next prompt instead of being echoed over the output. ctrl+c still interrupts."""

    def __init__(self, prompt):
        self.prompt, self.on, self.thread, self.saved, self.typed = prompt, False, None, None, ""

    def start(self):
        if self.on or not interactive():
            return
        fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
        quiet[3] &= ~(termios.ICANON | termios.ECHO)    # one key at a time, no echo; ctrl+c still signals
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        self.on = True
        self.thread = threading.Thread(target=self.listen, args=(fd,), daemon=True)
        self.thread.start()

    def listen(self, fd):
        while self.on:
            if not select.select([fd], [], [], 0.1)[0] or not self.on:
                continue
            key = self.prompt.key(fd)
            if key == "esc [Z":
                set_mode(next_mode(), quiet=True)
                colour, label, about = LOOK[state.mode]
                spinner.flash = (f"→ {label} · {about}", colour, time.time() + 4)
            elif isinstance(key, tuple):
                self.typed += key[1]
            elif key in ("\x7f", "\x08"):
                self.typed = self.typed[:-1]
            elif isinstance(key, str) and len(key) == 1 and key >= " ":
                self.typed += key

    def stop(self):
        if not self.on:
            return
        self.on = False
        self.thread.join()
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)
