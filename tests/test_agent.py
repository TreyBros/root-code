"""The agent loop end to end, against a fake model: it calls tools, gets results, and finishes."""

import json
from unittest import mock

from rootcode import agent
from rootcode.agent import looks_unfinished, run_turn, system_prompt
from rootcode.settings import state

from .support import RootTest, openai_stream


class Loop(RootTest):
    def test_tool_call_then_answer(self):
        server = self.use_server()
        state.mode = "auto"
        server.add(200, openai_stream("", [("c1", "write", json.dumps({"path": "hi.txt", "content": "hello"}))]))
        server.add(200, openai_stream("Wrote hi.txt."))
        messages = [{"role": "user", "content": "make hi.txt"}]
        run_turn(messages)
        self.assertEqual(self.read("hi.txt"), "hello")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": "c1", "content": "Wrote 1 lines to hi.txt"})
        second = server.requests[1]["body"]["messages"]          # the result went back to the model
        self.assertEqual(second[-1]["role"], "tool")
        self.assertIn("Worked for", self.out.getvalue())

    def test_nudges_a_model_that_announces_a_step_without_taking_it(self):
        server = self.use_server()
        server.add(200, openai_stream("Let me check the tests."))
        server.add(200, openai_stream("All done: nothing needed changing."))
        messages = [{"role": "user", "content": "check"}]
        run_turn(messages)
        self.assertEqual(len(server.requests), 2)
        self.assertIn("did not call a tool", messages[2]["content"])

    def test_a_cut_off_reply_is_continued(self):
        server = self.use_server()
        server.add(200, openai_stream("Part one", finish="length"))
        server.add(200, openai_stream("Part two. Done."))
        messages = [{"role": "user", "content": "go"}]
        run_turn(messages)
        self.assertIn("hit the output limit", messages[2]["content"])

    def test_plan_mode_asks_to_build_the_plan(self):
        server = self.use_server()
        state.mode = "plan"
        server.add(200, openai_stream("1. Change a.py. 2. Run the tests."))
        server.add(200, openai_stream("Built and verified."))
        messages = [{"role": "user", "content": "plan it"}]
        with mock.patch.object(agent, "ask", return_value="1"):
            run_turn(messages)
        self.assertEqual(state.mode, "auto")
        self.assertIn("approved the plan", messages[2]["content"])

    def test_the_log_records_each_call_but_not_in_incognito(self):
        server = self.use_server()
        logged = []
        with mock.patch.object(agent, "log_call", side_effect=logged.append):
            server.add(200, openai_stream("Hi."))
            run_turn([{"role": "user", "content": "hi"}])
        self.assertEqual(logged[0]["model"], "test-model")
        state.incognito = True
        with mock.patch("builtins.open") as opened:
            agent.log_call({"x": 1})
        opened.assert_not_called()


class Prompting(RootTest):
    def test_unfinished_replies(self):
        for text in ["", "Let me look at the file.", "Now I'll run the tests.", "Here is what I'll change:"]:
            self.assertTrue(looks_unfinished(text), text)
        for text in ["Done. Tests pass.", "Should I also update the docs?", "Fixed it. Let me know if more is needed."]:
            self.assertFalse(looks_unfinished(text), text)

    def test_project_notes_are_included(self):
        self.write("AGENTS.md", "Always use tabs.")
        self.assertIn("Always use tabs.", system_prompt())
        self.write("ROOT.md", "Root notes win.")
        self.assertIn("Root notes win.", system_prompt())
        self.assertNotIn("Always use tabs.", system_prompt())


if __name__ == "__main__":
    import unittest
    unittest.main()
