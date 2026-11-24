"""The loop: call the model, run the tools it asks for, send back the results, repeat until it answers."""

import json
import os
import random
import re
import sys
import time

from . import skills
from .modes import PLAN_NOTE, set_mode
from .providers import chat, compact_at, think_level
from .settings import HOME_DIR, LOG_PATH, state
from .tools import run_tool, specs, title
from .ui import ACCENT, BLUE, BOLD, DIM, RESET, VERBS, Markdown, ask, human, spinner

PROJECT_NOTES = ("ROOT.md", "AGENTS.md", "CLAUDE.md")     # first one found wins


def system_prompt():
    """Describe the environment and what "done" means. Nothing else."""
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
    section = skills.prompt_section()
    if section:
        parts.append(section)
    for name in PROJECT_NOTES:
        if os.path.isfile(name):
            with open(name, encoding="utf-8", errors="replace") as f:
                parts.append(f"Project instructions from {name}:\n{f.read()[:20000]}")
            break
    return "\n\n".join(parts)


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
        spinner.start(f"{self.verb} · {title(name)}", self.t0)

    def progress(self, s):
        self.mark()
        spinner.chars += len(s)

    def done(self):
        spinner.stop()
        self.md.flush()


def log_call(entry):
    """One line per model call in ~/.rootcode/log.jsonl, for working out why something was slow."""
    if state.incognito:
        return
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
    system = system_prompt()
    estimate = (len(system) + len(json.dumps(messages))) // 4
    live = Live(verb, t0, quiet, reading=estimate)
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "provider": state.provider, "model": state.model,
             "think": think_level(), "context_est": estimate}
    try:
        reply = chat(state.provider, state.key, state.model, system, messages, specs() if tools else None,
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
    state.usage = reply.usage or estimate
    return reply


def compact(messages, t0):
    """Replace a long history with a summary written for an agent that will resume the work."""
    request = messages + [{"role": "user", "content":
        "Stop and write a handoff summary for another agent that will resume this work with no other context. "
        "Include: the user's request in their words, what has been done, what remains, decisions made and why, "
        "and exact file paths, commands, error messages and identifiers that matter. Do not call tools."}]
    summary = call_model(request, "Compacting conversation", t0, tools=False, quiet=True).text
    plan = "\n".join(f"- [{i.get('status')}] {i.get('task')}" for i in state.todos)
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
        answer = ask(f"  {BLUE}❯{RESET} {DIM}[1-3, or type what to change]{RESET} ").strip()
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
            if state.mode == "plan" and reply.text.strip() and approve_plan(messages):
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
        if state.usage > compact_at():
            compact(messages, t0)
    secs = time.time() - t0
    took = f"{secs:.0f}s" if secs < 60 else f"{int(secs // 60)}m {int(secs % 60)}s"
    print(f"\n{ACCENT}✻{RESET} {DIM}Worked for {took} · {human(state.usage)} tokens in context{RESET}")
