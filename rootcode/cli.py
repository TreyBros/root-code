"""Startup and the main loop: read a prompt, run a /command or a turn, save, repeat."""

import os
import sys

from . import commands, providers
from .agent import run_turn, trim_log
from .connect import choose_model
from .editor import Keys, Prompt
from .modes import PLAN_NOTE
from .providers import PROVIDERS, ApiError, list_models, why
from .screen import banner
from .sessions import Sessions, pick_session
from .settings import CONFIG_PATH, flag, state
from .ui import ACCENT, DIM, RED, RESET, spinner


def read_flags():
    state.incognito = "--incognito" in sys.argv
    state.mode = ("plan" if "--plan" in sys.argv
                  else "auto" if "--auto" in sys.argv or "--yolo" in sys.argv else "approve")
    providers.setup()
    if not state.model and state.provider == "ollama":     # no model chosen yet: use the first one installed
        try:
            state.model = (list_models("ollama", None) or [""])[0]
        except Exception:
            pass


def first_run(task):
    return (not os.path.exists(CONFIG_PATH) and not task and not flag("provider")
            and not any(f"--{p}" in sys.argv for p in PROVIDERS) and not os.environ.get("ROOTCODE_PROVIDER"))


def turn(messages, prompt):
    """Send one prompt and let the model work until it's done or interrupted."""
    messages.append({"role": "user", "content": (PLAN_NOTE if state.mode == "plan" else "") + prompt})
    state.keys.start()
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
        print(f"\n  {RED}✗ Unexpected reply from {state.provider}: {e}{RESET}")
    finally:
        state.keys.stop()


def main():
    read_flags()
    commands.load_markdown_commands()
    task = " ".join(a for a in sys.argv[1:] if not a.startswith("--") and a != "-c")
    messages, sessions, line = [], Sessions(), Prompt()
    ctx = commands.Context(messages, sessions, line)
    state.keys = Keys(line)
    trim_log()
    banner()
    if first_run(task) and sys.stdin.isatty():
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
                    prompt, state.keys.typed = line.read(state.keys.typed), ""
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
            if not prompt:
                continue
            if prompt.startswith("/"):
                prompt = commands.run(ctx, prompt)
                if not prompt:
                    if one_shot:
                        return
                    continue
            spec = PROVIDERS[state.provider]
            if spec["key_env"] and not spec.get("key_optional") and not state.key:
                print(f"  {RED}No {spec['label']} API key yet. Run /model to add one.{RESET}")
                if one_shot:
                    return
                continue
            if not one_shot:
                print()
            turn(messages, prompt)
            sessions.save(messages)
            if one_shot:
                return
    except commands.Quit:
        return
    finally:
        spinner.stop()
        if state.keys:
            state.keys.stop()
