"""Both wire formats, against a fake server: streaming, tool calls, thinking settings, model lists, errors."""

import json
from unittest import mock

from rootcode import providers
from rootcode.providers import ApiError, Quiet, chat, list_models, to_anthropic, why
from rootcode.settings import CONFIG, state

from .support import FakeServer, RootTest, anthropic_stream, openai_stream


class Events(Quiet):
    def __init__(self):
        self.texts, self.thoughts, self.tools = [], [], []

    def text(self, s):
        self.texts.append(s)

    def reasoning(self, s):
        self.thoughts.append(s)

    def tool(self, name):
        self.tools.append(name)


READ = {"name": "read", "description": "Read a file.", "parameters": {"type": "object", "properties": {}}}


class OpenAIFormat(RootTest):
    def test_streams_text_reasoning_and_tool_calls(self):
        server = self.use_server()
        server.add(200, openai_stream("Looking now.", [("c1", "read", '{"path": "a.py"}')], reasoning="hmm"))
        events = Events()
        reply = chat("local", None, "m", "system", [{"role": "user", "content": "hi"}], [READ], events)
        self.assertEqual(reply.text, "Looking now.")
        self.assertEqual(reply.calls, [{"id": "c1", "name": "read", "arguments": '{"path": "a.py"}'}])
        self.assertEqual((reply.stop, reply.usage), ("tool", 120))
        self.assertEqual((events.thoughts, events.tools), (["hmm"], ["read"]))
        sent = server.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["body"]["messages"][0], {"role": "system", "content": "system"})
        self.assertEqual(sent["body"]["tools"][0]["function"]["name"], "read")

    def test_think_off_is_sent_the_way_each_provider_expects(self):
        server = self.use_server()
        server.add(200, openai_stream("ok"))
        chat("local", None, "m", "s", [{"role": "user", "content": "hi"}], None, Quiet(), think="off")
        self.assertEqual(server.requests[0]["body"]["reasoning_effort"], "none")
        server.add(200, openai_stream("ok"))
        with mock.patch.dict(providers.PROVIDERS["openrouter"], url=server.url + "/v1"):
            chat("openrouter", "k", "m", "s", [{"role": "user", "content": "hi"}], None, Quiet(), think="off")
        self.assertEqual(server.requests[1]["body"]["reasoning"], {"enabled": False})
        self.assertEqual(server.requests[1]["headers"]["authorization"], "Bearer k")

    def test_retries_a_rate_limit_then_succeeds(self):
        server = self.use_server()
        server.add(429, {"error": {"message": "slow down"}}, {"retry-after": "0"})
        server.add(200, openai_stream("done"))
        reply = chat("local", None, "m", "s", [{"role": "user", "content": "hi"}], None, Quiet())
        self.assertEqual((reply.text, len(server.requests)), ("done", 2))

    def test_a_rejected_key_explains_itself(self):
        server = self.use_server()
        server.add(401, {"error": {"message": "bad key"}})
        with self.assertRaises(ApiError) as caught:
            chat("local", "nope", "m", "s", [{"role": "user", "content": "hi"}], None, Quiet())
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("API key was rejected", why(caught.exception))

    def test_a_model_without_tool_support_explains_itself(self):
        error = ApiError(400, "registry.ollama.ai/x does not support tools")
        self.assertIn("can't use tools", why(error))


class AnthropicFormat(RootTest):
    def test_streams_text_and_tool_calls(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.add(200, anthropic_stream("On it.", [("t1", "glob", '{"pattern": "*.py"}')]))
        state.provider = "anthropic"
        with mock.patch.dict("os.environ", ROOTCODE_BASE_URL=server.url + "/v1"):
            reply = chat("anthropic", "key", "claude", "system", [{"role": "user", "content": "hi"}], [READ], Quiet())
        self.assertEqual(reply.text, "On it.")
        self.assertEqual(reply.calls, [{"id": "t1", "name": "glob", "arguments": '{"pattern": "*.py"}'}])
        self.assertEqual((reply.stop, reply.usage), ("tool", 87))
        sent = server.requests[0]
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "key")
        self.assertEqual(sent["body"]["tools"][0]["input_schema"], READ["parameters"])

    def test_history_converts_to_content_blocks(self):
        history = [{"role": "user", "content": "fix it"},
                   {"role": "assistant", "content": "Reading.", "tool_calls": [
                       {"id": "a", "type": "function", "function": {"name": "read", "arguments": '{"path": "x"}'}},
                       {"id": "b", "type": "function", "function": {"name": "read", "arguments": "not json"}}]},
                   {"role": "tool", "tool_call_id": "a", "content": "one"},
                   {"role": "tool", "tool_call_id": "b", "content": "two"}]
        out = to_anthropic(history)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual(out[1]["content"][1], {"type": "tool_use", "id": "a", "name": "read", "input": {"path": "x"}})
        self.assertEqual(out[1]["content"][2]["input"], {})
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["a", "b"])   # both results in one turn
        self.assertEqual(out[2]["content"][-1]["cache_control"], {"type": "ephemeral"})


class ModelLists(RootTest):
    def serve(self, provider, body):
        server = FakeServer()
        self.addCleanup(server.close)
        server.add(200, body)
        return server, mock.patch.dict(providers.PROVIDERS[provider], url=server.url + "/v1")

    def test_openrouter_lists_only_models_that_can_call_tools(self):
        server, patch = self.serve("openrouter", {"data": [
            {"id": "b/chat", "supported_parameters": ["tools"]}, {"id": "a/old", "supported_parameters": []}]})
        with patch:
            self.assertEqual(list_models("openrouter", "k"), ["b/chat"])

    def test_openai_drops_models_that_cannot_chat(self):
        server, patch = self.serve("openai", {"data": [{"id": "gpt-x"}, {"id": "text-embedding-3"}, {"id": "whisper-1"}]})
        with patch:
            self.assertEqual(list_models("openai", "k"), ["gpt-x"])

    def test_anthropic(self):
        server, patch = self.serve("anthropic", {"data": [{"id": "claude-a"}]})
        with patch:
            self.assertEqual(list_models("anthropic", "k"), ["claude-a"])
        self.assertEqual(server.requests[0]["headers"]["x-api-key"], "k")

    def test_ollama_skips_embedding_models(self):
        server = FakeServer()
        self.addCleanup(server.close)
        server.add(200, {"models": [{"name": "qwen3.6"}, {"name": "nomic-embed-text"}]})
        with mock.patch.dict("os.environ", OLLAMA_HOST=server.url):
            self.assertEqual(list_models("ollama", None), ["qwen3.6"])


class Setup(RootTest):
    def test_flags_beat_environment_beat_saved_config(self):
        CONFIG.update(provider="ollama", model="saved")
        with mock.patch("sys.argv", ["rootcode"]):
            providers.setup()
        self.assertEqual((state.provider, state.model), ("ollama", "saved"))
        with mock.patch("sys.argv", ["rootcode", "--openrouter", "--model=x/y"]), \
                mock.patch.dict("os.environ", OPENROUTER_API_KEY="sk"):
            providers.setup()
        self.assertEqual((state.provider, state.model, state.key), ("openrouter", "x/y", "sk"))


if __name__ == "__main__":
    import unittest
    unittest.main()
