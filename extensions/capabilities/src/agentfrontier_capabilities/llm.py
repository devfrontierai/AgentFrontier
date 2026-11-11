"""The LLM provider protocol of the capability framework: a provider-neutral chat model with tool calling, and the
Gemini, Anthropic, OpenAI, Azure OpenAI and OpenAI-compatible clients that map it to and from each wire format, so
no capability sees provider specifics. Also plain and structured (JSON) completions, a token estimate, a call
budget (per day and per task) and a scripted client for tests. Standard library only (urllib). Moved here from
Avero, which ported it from the Java ``llm`` package.

Token counts are normalised: ``input_tokens`` counts every prompt token (cached or not), ``output_tokens`` is the
visible output, ``reasoning_tokens`` the hidden thinking billed as output, ``cached_input_tokens`` the part of the
input served from the provider's prompt cache.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

USER, ASSISTANT, TOOL = "user", "assistant", "tool"


@dataclass
class Text:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    provider_meta: dict = field(default_factory=dict)  # opaque data echoed back (Gemini thought signatures)


@dataclass
class ToolResult:
    id: str
    name: str
    result: Any


@dataclass
class Message:
    role: str
    parts: list

    @staticmethod
    def user(text: str) -> "Message":
        return Message(USER, [Text(text)])

    @staticmethod
    def assistant(text: str) -> "Message":
        return Message(ASSISTANT, [Text(text)])

    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, Text))

    def tool_calls(self) -> list[ToolCall]:
        return [p for p in self.parts if isinstance(p, ToolCall)]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema of the arguments


@dataclass
class Request:
    system: str | None
    messages: list[Message]
    tools: list[Tool]
    temperature: float | None = None  # None: the provider's default (recent Claude models accept no other)
    max_tokens: int = 8192


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


@dataclass
class Response:
    message: Message
    usage: Usage
    finish_reason: str | None


class LlmError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Client(Protocol):
    provider: str
    model: str

    def chat(self, request: Request) -> Response: ...


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def post_json(url: str, headers: dict[str, str], body: Any, timeout: float = 120.0) -> dict:
    """JSON over HTTP for the provider clients. Error bodies are surfaced, keys never are."""
    req = urllib.request.Request(url, data=_dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - configured provider endpoint
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            n = json.loads(e.read() or b"{}")
        except ValueError:
            n = {}
        err = n.get("error") if isinstance(n, dict) else None
        msg = err.get("message") if isinstance(err, dict) else err if isinstance(err, str) else _dumps(n)
        msg = str(msg or e.reason)
        raise LlmError(e.code, "LLM provider returned " + str(e.code) + ": " + msg[:400]) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LlmError(0, "LLM provider unreachable: " + str(e)) from None


# ------------------------------------------------------------------------------------------------ Gemini


class GeminiClient:
    """Google Gemini ``generateContent`` with function calling. Gemini 3 attaches a ``thoughtSignature`` to
    function-call parts and requires it back on the next turn; thought-summary parts are dropped."""

    provider = "gemini"

    def __init__(self, base_url: str | None, model: str, api_key: str):
        self.base_url = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, req: Request) -> Response:
        return self.from_body(post_json(self.base_url + "/v1beta/models/" + self.model + ":generateContent",
                                        {"x-goog-api-key": self.api_key}, self.to_body(req)))

    def to_body(self, req: Request) -> dict:
        body: dict = {}
        if req.system is not None:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        contents = []
        for m in req.messages:
            parts = []
            for p in m.parts:
                if isinstance(p, Text):
                    parts.append({"text": p.text})
                elif isinstance(p, ToolCall):
                    fc: dict = {"name": p.name, "args": p.args}
                    if p.id and not p.id.startswith("gen_"):
                        fc["id"] = p.id
                    part: dict = {"functionCall": fc}
                    if p.provider_meta.get("thoughtSignature"):
                        part["thoughtSignature"] = p.provider_meta["thoughtSignature"]
                    parts.append(part)
                elif isinstance(p, ToolResult):
                    fr: dict = {"name": p.name,
                                "response": p.result if isinstance(p.result, dict) else {"result": p.result}}
                    if p.id and not p.id.startswith("gen_"):
                        fr["id"] = p.id
                    parts.append({"functionResponse": json.loads(_dumps(fr))})
            contents.append({"role": "model" if m.role == ASSISTANT else "user", "parts": parts})
        body["contents"] = contents
        if req.tools:
            body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description,
                                                        "parameters": t.parameters} for t in req.tools]}]
        body["generationConfig"] = {"maxOutputTokens": req.max_tokens}
        if req.temperature is not None:
            body["generationConfig"]["temperature"] = req.temperature
        return body

    def from_body(self, r: dict) -> Response:
        cands = r.get("candidates") or []
        if not cands:
            block = (r.get("promptFeedback") or {}).get("blockReason") or "no candidates returned"
            raise LlmError(200, "Gemini returned no answer: " + str(block))
        cand = cands[0]
        parts: list = []
        text = []
        for i, p in enumerate((cand.get("content") or {}).get("parts") or []):
            if p.get("thought"):
                continue
            if "functionCall" in p:
                fc = p["functionCall"]
                meta = {"thoughtSignature": p["thoughtSignature"]} if p.get("thoughtSignature") else {}
                parts.append(ToolCall(fc.get("id") or "gen_" + str(i), fc.get("name", ""), fc.get("args") or {}, meta))
            elif "text" in p:
                text.append(p["text"])
        if text:
            parts.insert(0, Text("".join(text)))
        u = r.get("usageMetadata") or {}
        # promptTokenCount includes cached tokens; candidatesTokenCount excludes thinking tokens
        return Response(Message(ASSISTANT, parts),
                        Usage(int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)),
                              int(u.get("cachedContentTokenCount", 0)), int(u.get("thoughtsTokenCount", 0))),
                        cand.get("finishReason"))


# ------------------------------------------------------------------------------------------------ Anthropic


class AnthropicClient:
    """Anthropic Messages API with tool use."""

    provider = "anthropic"

    def __init__(self, base_url: str | None, model: str, api_key: str):
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, req: Request) -> Response:
        return self.from_body(post_json(self.base_url + "/v1/messages",
                                        {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                                        self.to_body(req)))

    def to_body(self, req: Request) -> dict:
        body: dict = {"model": self.model, "max_tokens": req.max_tokens}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.system is not None:
            body["system"] = req.system
        messages = []
        for m in req.messages:
            content = []
            for p in m.parts:
                if isinstance(p, Text):
                    content.append({"type": "text", "text": p.text})
                elif isinstance(p, ToolCall):
                    content.append({"type": "tool_use", "id": p.id, "name": p.name, "input": p.args})
                elif isinstance(p, ToolResult):
                    content.append({"type": "tool_result", "tool_use_id": p.id, "content": _dumps(p.result)})
            messages.append({"role": "assistant" if m.role == ASSISTANT else "user", "content": content})
        body["messages"] = messages
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                             for t in req.tools]
        return body

    def from_body(self, r: dict) -> Response:
        parts: list = []
        text = []
        for b in r.get("content") or []:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                parts.append(ToolCall(b.get("id", ""), b.get("name", ""), b.get("input") or {}))
        if text:
            parts.insert(0, Text("".join(text)))
        u = r.get("usage") or {}
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        # input_tokens excludes cache reads and writes; thinking is already inside output_tokens
        return Response(Message(ASSISTANT, parts),
                        Usage(int(u.get("input_tokens") or 0) + cache_read + int(u.get("cache_creation_input_tokens")
                                                                                 or 0),
                              int(u.get("output_tokens") or 0), cache_read, 0),
                        r.get("stop_reason"))


# ------------------------------------------------------------------------------------------------ OpenAI


class OpenAiClient:
    """OpenAI Chat Completions with tools; also any OpenAI-compatible endpoint (Ollama, vLLM, Azure OpenAI, ...)."""

    def __init__(self, provider: str, base_url: str | None, model: str, api_key: str | None):
        self.provider = provider
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model
        self.api_key = api_key

    def chat(self, req: Request) -> Response:
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        return self.from_body(post_json(self.base_url + "/chat/completions", headers, self.to_body(req)))

    def to_body(self, req: Request) -> dict:
        body: dict = {"model": self.model, "max_tokens": req.max_tokens}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        messages: list = []
        if req.system is not None:
            messages.append({"role": "system", "content": req.system})
        for m in req.messages:
            if m.role == TOOL or (m.parts and all(isinstance(p, ToolResult) for p in m.parts)):
                for p in m.parts:
                    if isinstance(p, ToolResult):
                        messages.append({"role": "tool", "tool_call_id": p.id, "content": _dumps(p.result)})
                continue
            msg: dict = {"role": "assistant" if m.role == ASSISTANT else "user", "content": m.text() or None}
            calls = m.tool_calls()
            if calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": _dumps(c.args)}} for c in calls]
            messages.append(msg)
        body["messages"] = messages
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.parameters}} for t in req.tools]
        return body

    def from_body(self, r: dict) -> Response:
        choice = (r.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        parts: list = []
        if msg.get("content"):
            parts.append(Text(msg["content"]))
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            parts.append(ToolCall(tc.get("id", ""), (tc.get("function") or {}).get("name", ""), args))
        u = r.get("usage") or {}
        # completion_tokens includes reasoning tokens; split them out so they are not counted twice
        reasoning = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
        return Response(Message(ASSISTANT, parts),
                        Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0) - reasoning,
                              int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0), reasoning),
                        choice.get("finish_reason"))


class AzureOpenAiClient(OpenAiClient):
    """Azure OpenAI: the Chat Completions body of OpenAI, sent to a deployment (``model`` is the deployment name)
    with an ``api-key`` header and an ``api-version``."""

    API_VERSION = "2024-10-21"

    def __init__(self, base_url: str | None, model: str, api_key: str | None, api_version: str | None = None):
        if not base_url:
            raise ValueError("Azure OpenAI needs the resource endpoint as the base URL")
        super().__init__("azure_openai", base_url, model, api_key)
        self.api_version = api_version or self.API_VERSION

    def chat(self, req: Request) -> Response:
        url = (self.base_url + "/openai/deployments/" + self.model + "/chat/completions?api-version="
               + self.api_version)
        body = self.to_body(req)
        body.pop("model", None)
        return self.from_body(post_json(url, {"api-key": self.api_key or ""}, body))


DEFAULT_MODELS = {"gemini": "gemini-3.5-flash", "anthropic": "claude-opus-5-5"}
PROVIDERS = ("gemini", "anthropic", "openai", "azure_openai", "openai_compatible")


def build_client(provider: str, model: str | None, base_url: str | None, api_key: str | None) -> Client:
    if provider == "gemini":
        return GeminiClient(base_url, model or DEFAULT_MODELS["gemini"], api_key or "")
    if provider == "anthropic":
        return AnthropicClient(base_url, model or DEFAULT_MODELS["anthropic"], api_key or "")
    if provider in ("openai", "openai_compatible", "azure_openai"):
        if not model:
            raise ValueError("a model is required for " + provider)
        if provider == "azure_openai":
            return AzureOpenAiClient(base_url, model, api_key)
        return OpenAiClient(provider, base_url, model, api_key)
    raise ValueError("unsupported LLM provider '" + str(provider) + "' (use one of " + ", ".join(PROVIDERS) + ")")


# ------------------------------------------------------------------------------------------------ helpers

LlmProvider = Client  # the provider protocol of the capability framework


def count_tokens(text: str) -> int:
    """An estimate (about four characters per token) for budgeting before a call; providers report the real
    counts afterwards."""
    return max(1, (len(text or "") + 3) // 4)


def complete(client: Client, prompt: str, system: str | None = None, max_tokens: int = 4096,
             budget: "Budget | None" = None) -> tuple[str, Usage]:
    """One plain completion: the text and its usage."""
    req = Request(system, [Message.user(prompt)], [], None, max_tokens)
    r = budget.call(client, req) if budget is not None else client.chat(req)
    return r.message.text(), r.usage


def _json_text(text: str) -> Any:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if start > 0:
        t = t[start:]
    return json.loads(t)


def structured_output(client: Client, prompt: str, schema: dict, system: str | None = None,
                      max_tokens: int = 4096, budget: "Budget | None" = None, retries: int = 1) -> tuple[Any, Usage]:
    """A JSON value that has the schema's required properties. The schema goes in the prompt; an answer that is
    not JSON, or misses a required property, is retried once with the problem stated."""
    ask = (prompt + "\n\nAnswer with JSON only, no prose, matching this JSON Schema:\n" + _dumps(schema))
    total = Usage()
    problem = None
    for _ in range(retries + 1):
        text, u = complete(client, ask if problem is None else ask + "\n\nYour last answer was rejected: " + problem,
                           system, max_tokens, budget)
        total = Usage(total.input_tokens + u.input_tokens, total.output_tokens + u.output_tokens,
                      total.cached_input_tokens + u.cached_input_tokens, total.reasoning_tokens + u.reasoning_tokens)
        try:
            v = _json_text(text)
        except ValueError as e:
            problem = "not valid JSON (" + str(e) + ")"
            continue
        missing = [k for k in schema.get("required") or [] if not isinstance(v, dict) or k not in v]
        if missing:
            problem = "missing required properties: " + ", ".join(missing)
            continue
        return v, total
    raise LlmError(200, "the model did not return valid JSON: " + str(problem))


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Caps on model calls: per day and per task, with token totals. In memory; a product that needs the count to
    survive restarts persists :meth:`to_json` and restores it with :meth:`restore`."""

    def __init__(self, daily_calls: int = 0, task_calls: int = 0):
        self.daily_calls, self.task_calls = daily_calls, task_calls
        self._lock = threading.Lock()
        self.day = dt.date.today().isoformat()
        self.calls = self.input_tokens = self.output_tokens = 0
        self.tasks: dict[str, int] = {}

    def _roll(self) -> None:
        today = dt.date.today().isoformat()
        if today != self.day:
            self.day, self.calls, self.input_tokens, self.output_tokens, self.tasks = today, 0, 0, 0, {}

    def acquire(self, task: str | None = None) -> None:
        with self._lock:
            self._roll()
            if self.daily_calls and self.calls >= self.daily_calls:
                raise BudgetExceeded("daily limit of " + str(self.daily_calls) + " model calls reached")
            if task is not None and self.task_calls and self.tasks.get(task, 0) >= self.task_calls:
                raise BudgetExceeded("limit of " + str(self.task_calls) + " model calls for this task reached")
            self.calls += 1
            if task is not None:
                self.tasks[task] = self.tasks.get(task, 0) + 1

    def record(self, usage: Usage) -> None:
        with self._lock:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens + usage.reasoning_tokens

    def call(self, client: Client, req: Request, task: str | None = None) -> Response:
        self.acquire(task)
        r = client.chat(req)
        self.record(r.usage)
        return r

    def to_json(self) -> dict:
        return {"day": self.day, "calls": self.calls, "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens, "dailyCalls": self.daily_calls, "taskCalls": self.task_calls}

    def restore(self, d: dict) -> None:
        if d.get("day") == dt.date.today().isoformat():
            self.calls = int(d.get("calls", 0))
            self.input_tokens, self.output_tokens = int(d.get("inputTokens", 0)), int(d.get("outputTokens", 0))


class ScriptedClient:
    """A model that answers from a script, for tests and offline runs: each item is a text answer or a
    :class:`Response`."""

    provider, model = "scripted", "script-1"

    def __init__(self, steps: list):
        self.steps = list(steps)
        self.requests: list[Request] = []

    def chat(self, request: Request) -> Response:
        self.requests.append(request)
        s = self.steps.pop(0)
        if isinstance(s, Response):
            return s
        asked = request.messages[-1].text() if request.messages else ""
        return Response(Message.assistant(str(s)), Usage(count_tokens(asked), count_tokens(str(s))), "stop")
