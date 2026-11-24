"""Shared test helpers: a fake model server, stream builders, and a scratch working folder."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rootcode.settings import CONFIG, state


class FakeServer:
    """A local HTTP server that answers each request with the next scripted response and records
    what it was sent. Responses are (status, body) or (status, body, headers)."""

    def __init__(self):
        self.responses, self.requests = [], []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, body):
                server.requests.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                        "body": json.loads(body) if body else None})
                status, data, *headers = server.responses.pop(0) if server.responses else (500, b"{}")
                self.send_response(status)
                for k, v in (headers[0] if headers else {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.answer(b"")

            def do_POST(self):
                self.answer(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add(self, status, body, headers=None):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.responses.append((status, data, headers or {}))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def openai_stream(text="", calls=(), finish=None, reasoning="", usage=(100, 20)):
    """An OpenAI Chat Completions stream. calls: (id, name, arguments-json); arguments arrive in two pieces."""
    events = []
    if reasoning:
        events.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    for piece in ([text[:len(text) // 2], text[len(text) // 2:]] if text else []):
        events.append({"choices": [{"delta": {"content": piece}}]})
    for i, (call_id, name, args) in enumerate(calls):
        events.append({"choices": [{"delta": {"tool_calls": [
            {"index": i, "id": call_id, "function": {"name": name, "arguments": args[:5]}}]}}]})
        events.append({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": args[5:]}}]}}]})
    events.append({"choices": [{"delta": {}, "finish_reason": finish or ("tool_calls" if calls else "stop")}]})
    events.append({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def anthropic_stream(text="", calls=(), stop=None):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 30}}}]
    index = 0
    if text:
        events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}]
        index = 1
    for call_id, name, args in calls:
        events += [{"type": "content_block_start", "index": index,
                    "content_block": {"type": "tool_use", "id": call_id, "name": name}},
                   {"type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": args[:5]}},
                   {"type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": args[5:]}}]
        index += 1
    events.append({"type": "message_delta", "delta": {"stop_reason": stop or ("tool_use" if calls else "end_turn")},
                   "usage": {"output_tokens": 7}})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


class RootTest(unittest.TestCase):
    """Runs each test in a fresh empty folder, with Root's state reset and output captured."""

    def setUp(self):
        self.old_cwd = os.getcwd()
        self.dir = tempfile.mkdtemp(prefix="rootcode-test-")
        os.chdir(self.dir)
        state.__init__()
        self.saved_config = dict(CONFIG)
        self.out = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.out)
        self.capture.__enter__()

    def tearDown(self):
        self.capture.__exit__(None, None, None)
        os.chdir(self.old_cwd)
        CONFIG.clear()
        CONFIG.update(self.saved_config)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def use_server(self):
        """Point the 'local' provider at a fresh fake server."""
        server = FakeServer()
        self.addCleanup(server.close)
        state.provider, state.model = "local", "test-model"
        CONFIG["local_url"] = server.url + "/v1"
        return server
