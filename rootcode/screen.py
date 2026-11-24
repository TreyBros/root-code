"""The banner at startup, and the status bar above the input line."""

import os

from . import VERSION
from .modes import LOOK
from .providers import PROVIDERS, compact_at, think_level
from .settings import state
from .ui import (ACCENT, BOLD, DIM, GREY, LOGO, LOGO_SHADES, PURPLE, RESET, TTY, fg, tidy_path,
                 width)


def banner():
    print()
    if TTY and width() >= 42:
        for row, shade in zip(LOGO, LOGO_SHADES):
            print(f"  {fg(shade)}{row}{RESET}")
    else:
        print(f"  {BOLD}{ACCENT}ROOT{RESET}")
    print(f"  {DIM}lightweight coding agent · v{VERSION}{RESET}\n")
    print(f"  {GREY}model{RESET}  {BOLD}{state.model or '(none)'}{RESET} {DIM}via {PROVIDERS[state.provider]['label']}{RESET}")
    print(f"  {GREY}cwd{RESET}    {tidy_path(os.getcwd())}")
    colour, label, about = LOOK[state.mode]
    print(f"  {GREY}mode{RESET}   {colour}{label}{RESET} {DIM}· {about}{RESET}")
    if state.incognito:
        print(f"  {PURPLE}◐ incognito{RESET} {DIM}· nothing from this conversation is saved{RESET}")
    print(f"\n  {DIM}shift+tab switches modes · /resume past sessions · /incognito saves nothing · /help{RESET}")


def rule_text(note=""):
    """The status bar: mode, a note, model, provider, context used, thinking level. Always fits the width."""
    pct = min(99, int(100 * state.usage / max(compact_at(), 1)))
    colour, label, _ = LOOK[state.mode]
    w = min(width(), 120) - 1           # one column spare: a full-width line can wrap in some terminals
    extra = f" · {state.provider}" + (f" · {pct}% context" if pct >= 1 else "") \
        + (f" · think {think_level()}" if think_level() != "default" else "")
    model = state.model.split("/")[-1] if len(state.model) > 24 else state.model   # hf.co/org/name:tag → name:tag
    room = max(w - len(label) - len(extra) - 12, 8)
    model = model if len(model) <= room else model[:room - 1] + "…"
    info = f" {model}{extra} "
    if len(info) + len(label) + 10 > w:  # a very narrow terminal: just the mode and the model
        info = f" {model[:max(w - len(label) - 12, 1)]} "
    tag = " ◐ incognito" if state.incognito and len(info) + len(label) + 24 <= w else ""
    note = f" {note[:max(w - len(info) - len(label) - len(tag) - 12, 0)]} " if note else ""
    left = max(w - len(info) - len(label) - len(tag) - len(note) - 6, 1)
    return (f"{DIM}──{RESET} {colour}{label}{RESET}{PURPLE}{tag}{RESET}{GREY}{note}{RESET} "
            f"{DIM}{'─' * left}{info}──{RESET}")
