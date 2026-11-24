"""The /commands. Each one is a function registered with @command; the / menu and /help list them all.

A command gets the conversation context and whatever was typed after its name. It returns None when
it's done, or a string to send to the model as the next prompt (that's how /init works).

You don't need Python to add one: a markdown file in .root/commands/ (this project) or
~/.rootcode/commands/ (everywhere) becomes a command that sends the file as a prompt. `$ARGUMENTS` in
the file is replaced by what you type after the command. .claude/commands/ folders work too.
"""

import os
import sys
import time

from . import skills
from .agent import compact as summarize
from .connect import choose_model
from .modes import LOOK, next_mode, set_mode
from .providers import LOCAL, PROVIDERS, THINK_LEVELS, ApiError, think_level, why
from .screen import banner
from .sessions import pick_session
from .settings import CONFIG, HOME_DIR, save_config, state
from .ui import ACCENT, DIM, PURPLE, RED, RESET, TTY, spinner, tidy_path

COMMAND_DIRS = (".root/commands", ".claude/commands", os.path.join(HOME_DIR, "commands"), "~/.claude/commands")
INIT_PROMPT = ("Look through this project and write a ROOT.md file at its root, for future coding sessions: what the "
               "project is, how to install, build, run and test it, where the important code lives, and conventions "
               "to follow. Keep it under 80 lines and only include what you verified. If ROOT.md exists, improve it.")


class Quit(Exception):
    pass


class Context:
    """What a command can act on: the conversation, saved sessions and the input line."""

    def __init__(self, messages, sessions, line):
        self.messages, self.sessions, self.line = messages, sessions, line

    def new_conversation(self):
        self.messages.clear()
        state.todos.clear()
        state.usage, self.sessions.id = 0, None


class Command:
    def __init__(self, name, about, run, args=None, hidden=False):
        self.name, self.about, self.run, self.args, self.hidden = name, about, run, args or {}, hidden


COMMANDS = {}


def command(name, about, args=None, hidden=False):
    """Register a /command. `args` maps suggested arguments to what they do, for the / menu.
    A hidden command works but only shows in the menu once its first letters are typed."""
    def register(fn):
        COMMANDS[name] = Command(name, about, fn, args, hidden)
        return fn
    return register


def run(ctx, text):
    """Run the /command in text. Returns a prompt for the model, or None."""
    name, _, arg = text.partition(" ")
    found = COMMANDS.get(name)
    if not found and "/" in name[1:]:
        return text                     # a path, like /src/app.py: it's a prompt, not a command
    if not found:
        print(f"  {RED}No command {name}.{RESET}")
        return show_help(ctx, "")
    return found.run(ctx, arg.strip())

# ----------------------------------------------------------------------------
# The built-in commands
# ----------------------------------------------------------------------------

@command("/help", "show this help")
def show_help(ctx, arg):
    shown = [c for c in COMMANDS.values() if not c.hidden]
    pad = max(10, max(len(c.name) for c in shown))
    for c in shown:
        print(f"  {ACCENT}{c.name:<{pad}}{RESET} {DIM}{c.about}{RESET}")
    print(f"  {DIM}shift+tab switches modes, even while Root works · ctrl+c interrupts · ctrl+d quits{RESET}")


@command("/model", "switch provider, API key or model")
def model(ctx, arg):
    try:
        choose_model()
    except (EOFError, KeyboardInterrupt):
        spinner.stop()
        print(f"\n  {DIM}Cancelled, nothing changed.{RESET}")


@command("/mode", "cycle approve → auto → plan (or press shift+tab)")
def mode(ctx, arg):
    set_mode(next_mode())


@command("/plan", "plan mode: explore read-only, then propose a plan")
def plan(ctx, arg):
    set_mode("plan")


@command("/approve", "approve mode: ask before commands and file changes")
def approve(ctx, arg):
    set_mode("approve")


@command("/auto", "auto mode: run commands and edits without asking")
def auto(ctx, arg):
    set_mode("auto")


@command("/yolo", "auto mode", hidden=True)
def yolo(ctx, arg):
    set_mode("auto")


@command("/think", "how hard the model thinks: off, low, medium, high, default",
         args={"off": "fastest: no thinking (best for local models)", "low": "a little thinking",
               "medium": "balanced", "high": "think hardest; slowest",
               "default": "whatever the model does by default"})
def think(ctx, arg):
    level = arg.split()[0].lower() if arg else ""
    if level in THINK_LEVELS:
        CONFIG["think"] = level
        save_config()
        note = " (Anthropic models ignore this)" if state.provider == "anthropic" else ""
        print(f"  {DIM}✻ Thinking set to {level}{note}{RESET}")
    else:
        print(f"  {DIM}Thinking is {think_level()}. Use /think off · low · medium · high · default. "
              f"off is fastest on local models.{RESET}")


@command("/resume", "continue a saved session (add words to search titles)")
def resume(ctx, arg):
    try:
        pick_session(ctx.sessions, ctx.messages, arg)
    except (EOFError, KeyboardInterrupt):
        print(f"\n  {DIM}Cancelled.{RESET}")


@command("/clear", "start a new conversation (the current one stays saved)")
def clear(ctx, arg):
    ctx.new_conversation()
    print(f"  {DIM}✻ New conversation · " + ("still incognito, nothing saved" if state.incognito
          else "the last one is saved, /resume brings it back") + RESET)


@command("/incognito", "private conversation: nothing saved; leaving it wipes the screen")
def incognito(ctx, arg):
    """Like a private browser window: a fresh conversation that never touches the disk. Leaving it
    forgets the conversation and its prompts, and wipes the screen and scrollback."""
    turn_on = {"on": True, "off": False}.get(arg.lower(), not state.incognito)
    if turn_on == state.incognito:
        print(f"  {DIM}Incognito is already {'on' if state.incognito else 'off'}.{RESET}")
        return
    ctx.sessions.save(ctx.messages)     # a normal conversation is kept before going private
    ctx.new_conversation()
    if turn_on:
        state.incognito = True
        print(f"\n  {PURPLE}◐ Incognito{RESET} {DIM}· a new conversation that is never saved{RESET}")
        print(f"  {DIM}  no session, no prompt history, no log · your last conversation is saved{RESET}")
        if state.provider not in LOCAL:
            print(f"  {DIM}  prompts still go to {PROVIDERS[state.provider]['label']}; pick Ollama in /model for fully"
                  f" offline{RESET}")
        print(f"  {DIM}  /incognito again to leave: it wipes this conversation and the screen{RESET}")
    else:
        state.incognito = False
        ctx.line.forget_private()
        if TTY:
            sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")   # clear the screen and the scrollback
            banner()
        print(f"\n  {DIM}✻ Left incognito · that conversation is gone. Back to a new, saved one.{RESET}")


@command("/compact", "summarize the conversation to free context")
def compact(ctx, arg):
    if not ctx.messages:
        print(f"  {DIM}Nothing to compact yet.{RESET}")
        return
    try:
        summarize(ctx.messages, time.time())
        ctx.sessions.save(ctx.messages)
    except (ApiError, KeyboardInterrupt) as e:
        print(f"  {RED}Could not compact: {why(e)}{RESET}")


@command("/init", "write ROOT.md: notes on this project for future sessions")
def init(ctx, arg):
    return INIT_PROMPT


@command("/skills", "list the skills Root can use here")
def list_skills(ctx, arg):
    found = skills.find()
    if not found:
        print(f"  {DIM}No skills yet. A skill is a folder with a SKILL.md, in .root/skills/ (this project) or "
              f"~/.rootcode/skills/ (everywhere). .claude/skills/ folders work too.{RESET}")
        return
    pad = max(len(s.name) for s in found)
    for s in found:
        where = tidy_path(os.path.dirname(os.path.dirname(s.path)))
        print(f"  {ACCENT}{s.name:<{pad}}{RESET} {DIM}{s.description[:80]} · {where}{RESET}")
    print(f"  {DIM}The model loads a skill when a task matches it; you can also just ask it to use one.{RESET}")


@command("/exit", "quit (also /quit, ctrl+d)")
def leave(ctx, arg):
    raise Quit


@command("/quit", "quit", hidden=True)
def quit_(ctx, arg):
    raise Quit

# ----------------------------------------------------------------------------
# Commands written as markdown files
# ----------------------------------------------------------------------------

def markdown_command(path):
    def run_file(ctx, arg):
        with open(path, encoding="utf-8", errors="replace") as f:
            _, body = skills.frontmatter(f.read())
        body = body.strip()
        if "$ARGUMENTS" in body:
            return body.replace("$ARGUMENTS", arg)
        return body + (f"\n\n{arg}" if arg else "")
    return run_file


def load_markdown_commands():
    """Register every commands/*.md file Root can see. Built-in commands and earlier folders win."""
    for folder in COMMAND_DIRS:
        folder = os.path.expanduser(folder)
        if not os.path.isdir(folder):
            continue
        for entry in sorted(os.listdir(folder)):
            name = "/" + entry[:-3]
            if not entry.endswith(".md") or name in COMMANDS or " " in name:
                continue
            path = os.path.join(folder, entry)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    meta, body = skills.frontmatter(f.read(4000))
            except OSError:
                continue
            about = meta.get("description") or (body.strip().split("\n")[0].lstrip("# ")[:70] or "custom command")
            COMMANDS[name] = Command(name, about, markdown_command(path), hidden=False)
