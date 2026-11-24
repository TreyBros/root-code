"""
Root: a lightweight terminal coding agent. Python 3.9+, no dependencies.

    rootcode                       interactive; asks before running commands or changing files
    rootcode "fix the failing tests"   one task, then exit
    rootcode /model                pick provider, enter key, pick model
    rootcode --plan                start in plan mode (explore read-only, propose a plan, you approve it)
    rootcode --auto                start in auto mode (no permission prompts)
    rootcode --continue  (or -c)   pick up the latest session in this folder
    rootcode --resume              choose a past session to continue
    rootcode --incognito           nothing is saved: no session, no prompt history, no log
    rootcode --ollama --model=qwen3.6          flags pick provider and model for one run

The design: a loop the model exits by not calling a tool, a handful of sharp tools,
errors fed back as text, output kept small, and a summary when the context fills up.

Where things live:
    settings.py   paths, saved config, flags, and the shared state of a run
    ui.py         colours, spinner, markdown
    providers.py  the two wire formats (Anthropic Messages, OpenAI Chat Completions)
    modes.py      approve / auto / plan, and which shell commands only read
    tools.py      the tools the model can call: add one with @tool
    skills.py     SKILL.md folders and markdown slash commands
    agent.py      the loop: call the model, run its tools, repeat
    commands.py   the /commands: add one with @command
    connect.py    /model: provider, key, model, and a check that it answers
    sessions.py   saved conversations in SQLite
    screen.py     the banner and status bar
    editor.py     the input line and the / menu
    cli.py        startup and the main loop
"""

VERSION = "1.1"
