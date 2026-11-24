"""The tools themselves, the registry, and the rules about when a tool may run."""

import json
import os
from unittest import mock

from rootcode import tools
from rootcode.settings import state
from rootcode.tools import TOOLS, clip, run_tool, specs, tool

from .support import RootTest


def call(name, **args):
    return run_tool(name, json.dumps(args), 0)


class Files(RootTest):
    def test_read_numbers_lines_and_pages(self):
        self.write("a.txt", "".join(f"line {i}\n" for i in range(1, 11)))
        result = call("read", path="a.txt", offset=3, limit=2)
        self.assertIn("     3\tline 3", result)
        self.assertNotIn("line 5", result)
        self.assertIn("[6 more lines; read again with offset]", result)

    def test_edit_replaces_one_exact_match(self):
        state.mode = "auto"
        self.write("a.py", "x = 1\ny = 1\n")
        self.assertIn("occurs 2 times", call("edit", path="a.py", old="1", new="2"))
        self.assertIn("was not found", call("edit", path="a.py", old="z", new="2"))
        self.assertIn("Updated a.py", call("edit", path="a.py", old="y = 1", new="y = 2"))
        self.assertEqual(self.read("a.py"), "x = 1\ny = 2\n")
        call("edit", path="a.py", old="= ", new=":= ", replace_all=True)
        self.assertEqual(self.read("a.py"), "x := 1\ny := 2\n")

    def test_write_makes_folders_inside_the_project_only(self):
        state.mode = "auto"
        self.assertIn("Wrote 2 lines", call("write", path="src/new/a.py", content="a\nb"))
        self.assertEqual(self.read("src/new/a.py"), "a\nb")
        self.assertIn("does not exist", call("write", path="/no/such/place/a.py", content="x"))

    def test_glob(self):
        self.write("src/a.py", "")
        self.write("src/deep/b.py", "")
        self.assertEqual(sorted(call("glob", pattern="src/**/*.py").split("\n")), ["src/a.py", "src/deep/b.py"])
        self.assertEqual(call("glob", pattern="*.rs"), "No files matched.")

    def test_bash_reports_output_and_exit_code(self):
        state.mode = "auto"
        self.assertEqual(call("bash", command="echo hi; exit 3"), "hi\n[exit code 3]")
        self.assertIn("killed after 1s", call("bash", command="sleep 5", timeout=1))

    def test_long_output_keeps_head_and_tail_tighter_for_local_models(self):
        text = "a" * 20000 + "b" * 20000
        state.provider = "anthropic"
        self.assertEqual(clip("short"), "short")
        cloud = clip(text)
        state.provider = "ollama"
        local = clip(text)
        self.assertTrue(cloud.startswith("a") and cloud.endswith("b") and "characters cut" in cloud)
        self.assertLess(len(local), len(cloud))


class Rules(RootTest):
    def test_plan_mode_refuses_to_change_files(self):
        state.mode = "plan"
        self.assertIn("plan mode is on", call("write", path="a.txt", content="x"))
        self.assertFalse(os.path.exists("a.txt"))

    def test_approve_mode_asks_and_respects_no(self):
        state.mode = "approve"
        with mock.patch.object(tools, "ask", return_value="n") as asked:
            self.assertIn("declined", call("write", path="a.txt", content="x"))
        asked.assert_called_once()
        self.assertFalse(os.path.exists("a.txt"))

    def test_approve_mode_answer_a_switches_to_auto(self):
        state.mode = "approve"
        with mock.patch.object(tools, "ask", return_value="a"):
            call("write", path="a.txt", content="x")
        self.assertEqual((state.mode, self.read("a.txt")), ("auto", "x"))

    def test_read_only_commands_run_without_asking(self):
        state.mode = "approve"
        with mock.patch.object(tools, "ask") as asked:
            self.assertIn("[exit code 0]", call("bash", command="pwd"))
        asked.assert_not_called()

    def test_mistakes_come_back_as_text(self):
        self.assertIn("not valid JSON", run_tool("read", "{oops", 0))
        self.assertIn("no tool named fly", call("fly"))
        self.assertIn("Error: FileNotFoundError", call("read", path="missing.txt"))
        self.assertIn("Error: TypeError", call("read", wrong="x"))


class Registry(RootTest):
    def test_builtin_tools_in_order(self):
        names = [t["name"] for t in specs()]
        self.assertEqual(names[:6], ["bash", "read", "write", "edit", "glob", "todo"])

    def test_a_new_tool_is_one_decorated_function(self):
        @tool("shout", "Shout", "Upper-case some text.", {"text": {"type": "string"}}, ["text"])
        def shout(text):
            return text.upper()
        self.addCleanup(TOOLS.pop, "shout")
        self.assertIn("shout", [t["name"] for t in specs()])
        self.assertEqual(call("shout", text="hi"), "HI")

    def test_a_disabled_tool_is_hidden_and_refused(self):
        tool("hidden", "Hidden", "x", {}, enabled=lambda: False)(lambda: "ran")
        self.addCleanup(TOOLS.pop, "hidden")
        self.assertNotIn("hidden", [t["name"] for t in specs()])
        self.assertIn("no tool named hidden", call("hidden"))


if __name__ == "__main__":
    import unittest
    unittest.main()
