"""Paths, the saved config, command-line flags, and the state shared across one run."""

import json
import os
import sys

from . import VERSION

HOME_DIR = os.path.expanduser("~/.rootcode")
CONFIG_PATH = os.path.join(HOME_DIR, "config.json")
HISTORY_PATH = os.path.join(HOME_DIR, "history.jsonl")
DB_PATH = os.path.join(HOME_DIR, "sessions.db")
LOG_PATH = os.path.join(HOME_DIR, "log.jsonl")
UA = f"root-code/{VERSION}"
MAX_OUTPUT_TOKENS = 16000
MAX_TOOL_CHARS = 30000                  # anything longer is cut to its head and tail
MAX_TOOL_CHARS_LOCAL = 12000            # local models read slowly: keep tool output tighter


class State:
    """What changes while Root runs. One instance, `state`, shared by every module."""

    def __init__(self):
        self.provider = "ollama"
        self.model = ""
        self.key = None                 # the API key in use
        self.mode = "approve"           # approve, auto or plan
        self.incognito = False          # nothing written to disk: no session, no prompt history, no log
        self.usage = 0                  # tokens in context after the last model call
        self.todos = []                 # the model's plan, from the todo tool
        self.keys = None                # the key listener that runs while the model works


state = State()


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CONFIG = load_config()


def save_config():
    os.makedirs(HOME_DIR, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def flag(name):
    """The value of --name=value on the command line, or None."""
    for arg in sys.argv[1:]:
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None
