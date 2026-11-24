"""Skills: folders with a SKILL.md, in the same format Claude Code and other agents use.

    .root/skills/release-notes/SKILL.md
        ---
        name: release-notes
        description: Write release notes from the git log. Use when asked for a changelog or release notes.
        ---
        1. Run git log since the last tag ...

Only each skill's name and description go in the system prompt. The model loads the full instructions
with the skill tool when a task matches, which keeps the context small for local models.
"""

import os
import re

from .settings import HOME_DIR
from .tools import tool

PROJECT_DIRS = (".root/skills", ".claude/skills", ".agents/skills")
USER_DIRS = (os.path.join(HOME_DIR, "skills"), "~/.claude/skills", "~/.agents/skills")
MAX_LISTED = 50                         # skills described in the system prompt


def frontmatter(text):
    """The `key: value` header between --- lines, and the text after it. Handles the simple YAML that
    skills and commands use: plain or quoted values, and values continued on indented lines."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, rest = text[3:end], text[end + 4:]
    body = rest.split("\n", 1)[1] if "\n" in rest else ""
    meta, key = {}, None
    for line in head.splitlines():
        m = re.match(r"([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line[:1].isspace():
            key, value = m[1], m[2].strip()
            meta[key] = "" if value in (">", "|", ">-", "|-") else value.strip("\"'")
        elif key and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, body.lstrip("\n")


class Skill:
    def __init__(self, name, description, path):
        self.name, self.description, self.path = name, description, path


_cache = {}


def folders():
    return [os.path.abspath(d) for d in PROJECT_DIRS] + [os.path.expanduser(d) for d in USER_DIRS]


def find():
    """Every skill Root can see from here. Project skills win over user skills with the same name."""
    roots = folders()
    stamp = tuple(os.path.getmtime(d) if os.path.isdir(d) else 0 for d in roots)
    if _cache.get("key") == (roots, stamp):
        return _cache["skills"]
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    meta, body = frontmatter(f.read(20000))
            except OSError:
                continue
            name = meta.get("name") or entry
            description = meta.get("description") or body.strip().split("\n")[0][:200]
            if name not in found:
                found[name] = Skill(name, description, path)
    _cache.update(key=(roots, stamp), skills=list(found.values()))
    return _cache["skills"]


def prompt_section():
    """The part of the system prompt that tells the model which skills exist."""
    skills = find()
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description[:250]}" for s in skills[:MAX_LISTED]]
    return ("Skills are instructions for specific kinds of task. When the task matches one, call the skill "
            "tool with its name before starting, and follow what it says:\n" + "\n".join(lines))


@tool("skill", "Skill", "Load the full instructions of a skill listed in the system prompt, by name.",
      {"name": {"type": "string"}}, ["name"], enabled=lambda: bool(find()))
def tool_skill(name):
    skill = next((s for s in find() if s.name == name), None)
    if not skill:
        return f"Error: there is no skill named {name}. Available: {', '.join(s.name for s in find())}."
    with open(skill.path, encoding="utf-8", errors="replace") as f:
        _, body = frontmatter(f.read())
    folder = os.path.dirname(skill.path)
    extras = sorted(os.path.relpath(os.path.join(d, n), folder) for d, _, names in os.walk(folder)
                    for n in names if n != "SKILL.md")[:50]
    files = ("\n\nOther files in this skill (read them when the instructions point to them):\n"
             + "\n".join(f"- {os.path.join(folder, e)}" for e in extras)) if extras else ""
    return f"Skill: {skill.name}\nFolder: {folder}\n\n{body.strip()}{files}"
