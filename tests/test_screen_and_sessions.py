"""The status bar always fits, markdown wraps cleanly, the / menu suggests, and sessions save and load."""

import os
from unittest import mock

from rootcode.editor import Prompt
from rootcode.screen import rule_text
from rootcode.sessions import Sessions, first_prompt
from rootcode.settings import HISTORY_PATH, state
from rootcode.ui import Markdown

from .support import RootTest


class StatusBar(RootTest):
    def test_fits_every_width_even_with_a_huge_model_name(self):
        for model in ["qwen3.6", "hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED-GGUF:Q4_K_M", "x" * 150]:
            for incognito in (False, True):
                state.model, state.incognito, state.usage = model, incognito, 30000
                for columns in (30, 50, 80, 120, 200):
                    with mock.patch.dict(os.environ, COLUMNS=str(columns)):
                        bar = rule_text("· asks before commands")
                    self.assertLessEqual(len(bar), min(columns, 120) - 1, (model, columns))
                    self.assertNotIn("\n", bar)

    def test_shows_mode_model_context_and_incognito(self):
        state.model, state.usage, state.incognito = "qwen3.6", 24000, True
        bar = rule_text()
        for part in ("approve mode", "◐ incognito", "qwen3.6", "ollama", "50% context"):
            self.assertIn(part, bar)


class Markdowns(RootTest):
    def test_wraps_prose_without_breaking_code_spans(self):
        with mock.patch.dict(os.environ, COLUMNS="40"):
            md = Markdown()
            md.feed("- a list item that is long enough to wrap, with `some code here` inside it\n")
            md.feed("```py\nprint('a very long line of code that must not be wrapped at all')\n```\n")
            md.flush()
        lines = self.out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("⏺ • a list"))
        self.assertTrue(any("`" not in line and "some code here" in line for line in lines))
        self.assertTrue(all(len(line) <= 40 for line in lines if "│" not in line))
        self.assertIn("  │ print('a very long line of code that must not be wrapped at all')", lines)


class Menu(RootTest):
    def suggestions(self, typed):
        line = Prompt()
        line.buf = typed
        return [name for name, _ in line.menu()]

    def test_suggests_commands_then_arguments(self):
        self.assertEqual(self.suggestions("/th"), ["/think"])
        self.assertEqual(self.suggestions("/think o"), ["/think off"])
        self.assertIn("/incognito", self.suggestions("/"))
        self.assertNotIn("/quit", self.suggestions("/"))           # hidden until typed
        self.assertIn("/quit", self.suggestions("/q"))
        self.assertIn("/compact", self.suggestions("/pac"))       # matches inside names too
        self.assertEqual(self.suggestions("hello"), [])


class SavedSessions(RootTest):
    def sessions(self):
        opened = Sessions()
        self.addCleanup(opened.db.close)
        return opened

    def test_save_and_load_round_trip(self):
        state.provider, state.model = "ollama", "m"
        messages = [{"role": "user", "content": "fix the login bug\nmore detail"},
                    {"role": "assistant", "content": "Fixed."}]
        first = self.sessions()
        first.save(messages)
        second, loaded = self.sessions(), []
        self.assertEqual(second.recent()[0][2], "fix the login bug")
        second.load(second.latest_here(), loaded)
        self.assertEqual(loaded, messages)
        self.assertIn("Resumed · 1 prompt", self.out.getvalue())

    def test_incognito_saves_nothing(self):
        state.incognito = True
        sessions = self.sessions()
        sessions.save([{"role": "user", "content": "secret"}])
        self.assertIsNone(sessions.id)
        self.assertFalse(any("secret" in row[2] for row in sessions.recent()))

    def test_title_skips_the_plan_mode_note(self):
        from rootcode.modes import PLAN_NOTE
        self.assertEqual(first_prompt([{"role": "user", "content": PLAN_NOTE + "add search"}]), "add search")

    def test_typed_history_stays_out_of_the_file_while_incognito(self):
        line = Prompt()
        state.incognito = True
        line.remember("private thing")
        line.forget_private()
        state.incognito = False
        self.assertNotIn("private thing", line.history)
        self.assertNotIn("private thing", Prompt().history)
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as f:
                self.assertNotIn("private thing", f.read())


if __name__ == "__main__":
    import unittest
    unittest.main()
