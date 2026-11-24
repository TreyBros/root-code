"""Skills (SKILL.md folders) and the /commands, built-in and markdown."""

import json
import os
from unittest import mock

from rootcode import commands, skills
from rootcode.agent import system_prompt
from rootcode.commands import COMMANDS, Context, Quit, load_markdown_commands, run
from rootcode.settings import CONFIG, state
from rootcode.tools import run_tool, specs

from .support import RootTest

SKILL = """---
name: release-notes
description: >
  Write release notes from the git log.
  Use when asked for a changelog.
---
# Release notes

1. Run git log since the last tag.
"""


class Skills(RootTest):
    def test_frontmatter(self):
        meta, body = skills.frontmatter(SKILL)
        self.assertEqual(meta, {"name": "release-notes",
                                "description": "Write release notes from the git log. Use when asked for a changelog."})
        self.assertTrue(body.startswith("# Release notes"))
        self.assertEqual(skills.frontmatter("no header"), ({}, "no header"))
        self.assertEqual(skills.frontmatter('---\nname: "q"\n---\nx')[0], {"name": "q"})

    def test_no_skills_means_no_skill_tool_and_nothing_in_the_prompt(self):
        self.assertNotIn("skill", [t["name"] for t in specs()])
        self.assertNotIn("Skills are", system_prompt())

    def test_project_skill_is_listed_then_loaded_on_demand(self):
        self.write(".root/skills/release-notes/SKILL.md", SKILL)
        self.write(".root/skills/release-notes/template.md", "## Changes")
        self.assertIn("- release-notes: Write release notes", system_prompt())
        self.assertNotIn("Run git log", system_prompt())           # only the description, until it's needed
        self.assertIn("skill", [t["name"] for t in specs()])
        result = run_tool("skill", json.dumps({"name": "release-notes"}), 0)
        self.assertIn("1. Run git log since the last tag.", result)
        self.assertIn("template.md", result)
        self.assertIn("no skill named nope", run_tool("skill", '{"name": "nope"}', 0))

    def test_project_skills_win_and_claude_folders_work(self):
        self.write(".claude/skills/a/SKILL.md", "---\nname: same\ndescription: from .claude\n---\n")
        self.write(".root/skills/b/SKILL.md", "---\nname: same\ndescription: from .root\n---\n")
        self.write(os.path.expanduser("~/.claude/skills/c/SKILL.md"), "---\ndescription: user skill\n---\n")
        self.addCleanup(os.remove, os.path.expanduser("~/.claude/skills/c/SKILL.md"))
        found = {s.name: s.description for s in skills.find()}
        self.assertEqual(found, {"same": "from .root", "c": "user skill"})


class Commands(RootTest):
    def setUp(self):
        super().setUp()
        self.messages = [{"role": "user", "content": "hi"}]
        self.sessions = mock.Mock(id=7)
        self.ctx = Context(self.messages, self.sessions, mock.Mock())

    def test_init_sends_a_prompt_and_exit_quits(self):
        self.assertIn("ROOT.md", run(self.ctx, "/init"))
        self.assertRaises(Quit, run, self.ctx, "/exit")
        self.assertRaises(Quit, run, self.ctx, "/quit")

    def test_unknown_command_shows_help_but_a_path_is_a_prompt(self):
        self.assertIsNone(run(self.ctx, "/nope"))
        self.assertIn("No command /nope", self.out.getvalue())
        self.assertEqual(run(self.ctx, "/src/app.py crashes"), "/src/app.py crashes")

    def test_modes_and_thinking(self):
        run(self.ctx, "/plan")
        self.assertEqual(state.mode, "plan")
        run(self.ctx, "/mode")
        self.assertEqual(state.mode, "approve")
        with mock.patch.object(commands, "save_config"):
            run(self.ctx, "/think off")
        self.assertEqual(CONFIG["think"], "off")

    def test_clear_starts_over(self):
        state.todos[:] = [{"task": "x", "status": "pending"}]
        run(self.ctx, "/clear")
        self.assertEqual((self.messages, state.todos, self.sessions.id), ([], [], None))

    def test_incognito_saves_first_then_wipes_on_the_way_out(self):
        run(self.ctx, "/incognito")
        self.sessions.save.assert_called_once()
        self.assertTrue(state.incognito)
        self.assertEqual(self.messages, [])
        self.messages.append({"role": "user", "content": "secret"})
        run(self.ctx, "/incognito off")
        self.assertFalse(state.incognito)
        self.assertEqual(self.messages, [])
        self.ctx.line.forget_private.assert_called_once()

    def test_markdown_files_become_commands(self):
        self.write(".root/commands/review.md", "---\ndescription: review my changes\n---\nReview $ARGUMENTS carefully.")
        self.write(".root/commands/tests.md", "Run the tests and fix failures.")
        self.write(".root/commands/help.md", "should not replace /help")
        self.addCleanup(lambda: [COMMANDS.pop(n, None) for n in ("/review", "/tests")])
        load_markdown_commands()
        self.assertEqual(COMMANDS["/review"].about, "review my changes")
        self.assertEqual(run(self.ctx, "/review the auth module"), "Review the auth module carefully.")
        self.assertEqual(run(self.ctx, "/tests in src"), "Run the tests and fix failures.\n\nin src")
        self.assertEqual(COMMANDS["/help"].about, "show this help")


if __name__ == "__main__":
    import unittest
    unittest.main()
