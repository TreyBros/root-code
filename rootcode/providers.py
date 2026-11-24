"""Providers: two wire formats cover all of them. Plain urllib, streamed, with retries.

To add a provider that speaks one of the two formats, add a row to PROVIDERS.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .settings import CONFIG, MAX_OUTPUT_TOKENS, UA, flag, state
from .ui import spinner

PROVIDERS = {
    "anthropic":  {"label": "Anthropic", "key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5",
                   "url": "https://api.anthropic.com/v1", "ctx": 140000},
    "openai":     {"label": "OpenAI", "key_env": "OPENAI_API_KEY", "model": "gpt-6.2-sol",
                   "url": "https://api.openai.com/v1", "ctx": 140000},
    "openrouter": {"label": "OpenRouter", "key_env": "OPENROUTER_API_KEY", "model": "anthropic/claude-sonnet-5.5",
                   "url": "https://openrouter.ai/api/v1", "ctx": 140000},
    "ollama":     {"label": "Ollama (local)", "key_env": None, "model": "",   # first installed model
                   "url": None, "ctx": 48000},
    "local":      {"label": "Local server (LM Studio, llama.cpp, vLLM)", "key_env": "ROOTCODE_LOCAL_KEY",
                   "model": "", "url": None, "ctx": 24000, "key_optional": True},
}
LOCAL = ("ollama", "local")
THINK_LEVELS = ("default", "off", "low", "medium", "high")


def api_key(provider):
    env = PROVIDERS[provider]["key_env"]
    return (env and os.environ.get(env)) or CONFIG.get("keys", {}).get(provider)


def ollama_host():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return (host if host.startswith("http") else "http://" + host).rstrip("/")


def base_url(provider):
    if os.environ.get("ROOTCODE_BASE_URL") and provider == state.provider:
        return os.environ["ROOTCODE_BASE_URL"].rstrip("/")
    if provider == "ollama":
        return ollama_host() + "/v1"
    if provider == "local":
        return CONFIG.get("local_url", "http://localhost:1234/v1").rstrip("/")
    return PROVIDERS[provider]["url"]


def auth(provider, key):
    if provider == "anthropic":
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def pick_provider():
    for name in PROVIDERS:
        if f"--{name}" in sys.argv:
            return name
    chosen = flag("provider") or os.environ.get("ROOTCODE_PROVIDER") or CONFIG.get("provider")
    if chosen:
        if chosen not in PROVIDERS:
            sys.exit(f"Unknown provider {chosen!r}. Choose one of: {', '.join(PROVIDERS)}")
        return chosen
    for name in ("anthropic", "openrouter", "openai"):
        if api_key(name):
            return name
    return "ollama"


def setup():
    """Choose the provider, model and key for this run: flags, then environment, then the saved config."""
    state.provider = pick_provider()
    state.model = (flag("model") or os.environ.get("ROOTCODE_MODEL")
                   or (CONFIG.get("model") if CONFIG.get("provider") == state.provider else None)
                   or PROVIDERS[state.provider]["model"])
    state.key = api_key(state.provider)


def compact_at():
    return int(os.environ.get("ROOTCODE_COMPACT_TOKENS", PROVIDERS[state.provider]["ctx"]))


def think_level():
    return flag("think") or os.environ.get("ROOTCODE_THINK") or CONFIG.get("think", "default")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status} {message}" if status else message)
        self.status = status


def error_text(raw):
    try:
        err = json.loads(raw).get("error", raw)
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "replace")[:300] if isinstance(raw, bytes) else str(raw)[:300]


def why(e):
    """An error, said so a person knows what to do next."""
    if getattr(e, "status", None) in (401, 403):
        return "the API key was rejected. Check it and run /model again."
    if getattr(e, "status", None) == 404:
        return "not found (check the model name or server address)."
    if "support tools" in str(e) or "tool" in str(e).lower() and getattr(e, "status", None) == 400:
        return ("this model can't use tools, and Root needs them to read, edit and run things. "
                "Pick a model with tool support. For an Ollama model imported from Hugging Face, see "
                "\"Models that can't use tools\" in the README: it's usually a missing chat template.")
    return str(e)


def http(url, headers=None, body=None, timeout=600, retries=5):
    """Open a request, retrying rate limits, overloads and dropped connections with backoff."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "content-type": "application/json", **(headers or {})})
    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            with e:
                message = error_text(e.read())
            if e.code not in (408, 409, 429, 500, 502, 503, 504, 529) or attempt == retries:
                raise ApiError(e.code, message) from None
            wait = float(e.headers.get("retry-after") or min(2 ** attempt, 30))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == retries:
                reason = getattr(e, "reason", e)
                raise ApiError(None, f"cannot reach {url.split('/v1')[0]} ({reason})") from None
            wait, message = min(2 ** attempt, 30), "connection problem"
        spinner.label = f"Retrying in {int(wait)}s ({message[:40]})"
        time.sleep(wait)


def get_json(url, headers=None):
    with http(url, headers, timeout=20, retries=1) as response:
        return json.load(response)


def sse(response):
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            yield line[5:].strip()


class Reply:
    def __init__(self):
        self.text, self.calls, self.stop, self.usage, self.reasoning_details = "", [], "end", 0, {}

    def add_call(self, call_id, name):
        call = {"id": call_id or f"call_{len(self.calls)}_{int(time.time() * 1000)}",
                "name": name or "", "arguments": ""}
        self.calls.append(call)
        return call


class Quiet:
    """Stream events that go nowhere: for calls whose output isn't shown."""
    def text(self, s): pass
    def reasoning(self, s): pass
    def tool(self, name): pass
    def progress(self, s): pass


def to_anthropic(messages):
    """The OpenAI-style history used internally, as Anthropic content blocks."""
    out = []
    for m in messages:
        if m["role"] == "tool":
            role, blocks = "user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                     "content": m["content"]}]
        elif m["role"] == "user":
            role, blocks = "user", [{"type": "text", "text": m["content"]}]
        else:
            role, blocks = "assistant", [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            if not blocks:
                continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    if out:                             # one moving cache breakpoint keeps a long loop cheap
        out[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return out


def to_openai(messages, provider):
    out = []
    for m in messages:
        m = dict(m)
        details = m.pop("reasoning_details", None)
        if details and provider == "openrouter":   # Gemini and others need their reasoning back
            m["reasoning_details"] = details
        out.append(m)
    return out


def chat(provider, key, model, system, messages, tools, on, max_tokens=MAX_OUTPUT_TOKENS, think=None):
    """One streamed model call. `on` gets text, reasoning, tool and progress events."""
    reply, url = Reply(), base_url(provider)
    if provider == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": to_anthropic(messages),
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"],
                              "input_schema": t["parameters"]} for t in tools]
        blocks = {}
        with http(url + "/messages", auth(provider, key), body) as response:
            for data in sse(response):
                event = json.loads(data)
                kind = event.get("type")
                if kind == "message_start":
                    u = event["message"].get("usage") or {}
                    reply.usage = sum(u.get(k) or 0 for k in
                                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                elif kind == "content_block_start" and event["content_block"]["type"] == "tool_use":
                    block = event["content_block"]
                    blocks[event["index"]] = reply.add_call(block["id"], block["name"])
                    on.tool(block["name"])
                elif kind == "content_block_delta":
                    delta = event["delta"]
                    if delta["type"] == "text_delta":
                        reply.text += delta["text"]
                        on.text(delta["text"])
                    elif delta["type"] == "input_json_delta":
                        blocks[event["index"]]["arguments"] += delta["partial_json"]
                        on.progress(delta["partial_json"])
                    elif delta["type"] == "thinking_delta":
                        on.reasoning(delta.get("thinking", ""))
                elif kind == "message_delta":
                    reply.stop = {"tool_use": "tool", "max_tokens": "length"}.get(
                        event["delta"].get("stop_reason"), "end")
                    reply.usage += (event.get("usage") or {}).get("output_tokens") or 0
                elif kind == "error":
                    raise ApiError(None, error_text(json.dumps(event)))
        return reply

    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "system", "content": system}] + to_openai(messages, provider)}
    body["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens
    if think and think != "default":
        if provider == "openrouter":
            body["reasoning"] = {"enabled": False} if think == "off" else {"effort": think}
        else:
            body["reasoning_effort"] = "none" if think == "off" else think
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
    slots = {}
    with http(url + "/chat/completions", auth(provider, key), body) as response:
        for data in sse(response):
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("error"):
                raise ApiError(None, error_text(json.dumps(event)))
            if event.get("usage"):
                reply.usage = (event["usage"].get("prompt_tokens") or 0) + (event["usage"].get("completion_tokens") or 0)
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning") or delta.get("reasoning_content")
                if thought:
                    on.reasoning(thought)
                for d in delta.get("reasoning_details") or []:
                    slot = reply.reasoning_details.setdefault(d.get("index", 0), {})
                    for k, v in d.items():
                        slot[k] = slot.get(k, "") + v if k in ("text", "summary", "data") and isinstance(v, str) else v
                if delta.get("content"):
                    reply.text += delta["content"]
                    on.text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    i = tc.get("index")
                    if i is None:
                        i = next((k for k, c in slots.items() if c["id"] == tc.get("id")), len(slots))
                    if i not in slots:
                        slots[i] = reply.add_call(tc.get("id"), fn.get("name"))
                        on.tool(fn.get("name") or "tool")
                    args = fn.get("arguments") or ""
                    args = json.dumps(args) if isinstance(args, dict) else args
                    slots[i]["arguments"] += args
                    on.progress(args)
                if choice.get("finish_reason"):
                    reply.stop = {"tool_calls": "tool", "length": "length"}.get(choice["finish_reason"], "end")
    return reply


def list_models(provider, key):
    """The models a provider offers, for /model."""
    if provider == "ollama":
        names = [m["name"] for m in get_json(ollama_host() + "/api/tags")["models"]]
        return [n for n in names if "embed" not in n]
    if provider == "openrouter":            # only models that can call tools
        data = get_json(base_url(provider) + "/models")["data"]
        return sorted(m["id"] for m in data if "tools" in (m.get("supported_parameters") or []))
    if provider == "anthropic":
        return [m["id"] for m in get_json(base_url(provider) + "/models?limit=1000", auth(provider, key))["data"]]
    ids = sorted(m["id"] for m in get_json(base_url(provider) + "/models", auth(provider, key))["data"])
    if provider == "openai":                # drop models that can't chat
        skip = r"embed|tts|whisper|dall-e|davinci|babbage|moderation|image|audio|realtime|transcribe|sora|search"
        ids = [i for i in ids if not re.search(skip, i)]
    return ids


def without_tools(provider, models):
    """Ollama models whose template can't do tool calls (Root needs them)."""
    if provider != "ollama":
        return set()
    missing = set()
    for name in models:
        try:
            show = http(ollama_host() + "/api/show", body={"model": name}, timeout=10, retries=0)
            with show:
                if "tools" not in (json.load(show).get("capabilities") or ["tools"]):
                    missing.add(name)
        except (ApiError, ValueError, OSError):
            pass
    return missing
