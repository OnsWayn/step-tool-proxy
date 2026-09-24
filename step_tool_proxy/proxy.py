"""Upstream proxy: OpenAI-compatible downstream, per-node upstream formats.

Downstream clients talk chat.completions (``/v1/chat/completions``) or the
Responses API (``/v1/responses``). Each client token is bound to one upstream
node — either a StepFun Plan node (OpenAI-compatible) or a Claude Code /
Anthropic SDK node (Anthropic Messages API) — and the request/response bodies
are converted accordingly.
"""

from __future__ import annotations

import json
import time
import uuid
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import __version__
from .anthropic_api import (
    ANTHROPIC_VERSION,
    AnthropicToChatStream,
    SSEDecoder,
    anthropic_endpoint,
    anthropic_models_to_openai,
    anthropic_response_to_chat,
    anthropic_sse_to_chat_sse,
    chat_request_to_anthropic,
)
from .responses_api import (
    ChatToResponsesStream,
    chat_response_to_responses,
    chat_sse_to_responses_sse,
    responses_request_to_chat,
)
from .store import UPSTREAM_TYPES, normalize_upstream_type

if TYPE_CHECKING:
    from .config import Config


class UpstreamError(Exception):
    """Upstream refused the call before streaming started (status + body)."""

    def __init__(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        super().__init__(f"upstream error {status}")
        self.status = status
        self.body = body
        self.content_type = content_type

# StepFun only emits `reasoning_content` when the request sets
# reasoning_format="deepseek-style", but clients (@ai-sdk/xai, Grok Build)
# only read `reasoning_content`. Force both fields so nothing is swallowed.
REASONING_KEYS = ("reasoning_content", "reasoning")


def reasoning_from(obj: dict) -> str:
    """First non-empty reasoning text from `reasoning_content` or `reasoning`."""
    for key in REASONING_KEYS:
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def with_reasoning_aliases(obj: dict) -> dict:
    """Ensure `reasoning_content` and `reasoning` carry the same text."""
    text = reasoning_from(obj)
    if not text:
        return obj
    return {**obj, "reasoning_content": text, "reasoning": text}


def sanitize_tool_delta(tc: dict) -> dict | None:
    """Drop empty id/type/name so clients won't overwrite earlier values."""
    out: dict[str, Any] = {"index": tc.get("index", 0)}
    tid = tc.get("id")
    ttype = tc.get("type")
    fn = dict(tc.get("function") or {})
    if tid:
        out["id"] = tid
    if ttype:
        out["type"] = ttype
    clean_fn: dict[str, Any] = {}
    if fn.get("name"):
        clean_fn["name"] = fn["name"]
    if "arguments" in fn and fn["arguments"] is not None:
        if fn["arguments"] != "" or clean_fn.get("name") or out.get("id"):
            clean_fn["arguments"] = fn["arguments"]
    if clean_fn:
        out["function"] = clean_fn
    if set(out.keys()) == {"index"}:
        return None
    return out


def rewrite_sse(raw: bytes) -> bytes:
    """Sanitize empty tool_call fields from SSE deltas; mirror reasoning fields."""
    out_lines: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if not line.startswith(b"data: "):
            out_lines.append(line)
            continue
        payload = line[6:].strip()
        if payload == b"[DONE]":
            out_lines.append(line)
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            out_lines.append(line)
            continue
        choices = obj.get("choices")
        if not choices:
            out_lines.append(line)
            continue
        ch0 = choices[0]
        delta = with_reasoning_aliases(ch0.get("delta") or {})
        tcs = delta.get("tool_calls")
        if tcs:
            new_tcs = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                s = sanitize_tool_delta(tc)
                if s:
                    new_tcs.append(s)
            if new_tcs:
                delta = dict(delta)
                delta["tool_calls"] = new_tcs
                ch0 = dict(ch0)
                ch0["delta"] = delta
                obj = dict(obj)
                obj["choices"] = [ch0] + list(choices[1:])
            else:
                delta = dict(delta)
                delta.pop("tool_calls", None)
                ch0 = dict(ch0)
                ch0["delta"] = delta
                obj = dict(obj)
                obj["choices"] = [ch0] + list(choices[1:])
        elif delta:
            ch0 = dict(ch0)
            ch0["delta"] = delta
            obj = dict(obj)
            obj["choices"] = [ch0] + list(choices[1:])
        out_lines.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n")
    text = b"".join(out_lines)
    return text if text.endswith(b"\n") else text + b"\n"


def rewrite_json(raw: bytes) -> bytes:
    """Mirror reasoning fields in a non-stream chat completion JSON body."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    choices = obj.get("choices")
    if not choices:
        return raw
    ch0 = choices[0]
    msg = ch0.get("message")
    if not isinstance(msg, dict):
        return raw
    aliased = with_reasoning_aliases(msg)
    if aliased is msg:
        return raw
    ch0 = dict(ch0)
    ch0["message"] = aliased
    obj = dict(obj)
    obj["choices"] = [ch0] + list(choices[1:])
    return json.dumps(obj, ensure_ascii=False).encode()


def buffer_to_sse(resp_json: dict, model: str, include_usage: bool = False) -> bytes:
    """Convert a non-stream chat completion JSON into complete SSE events."""
    ch = (resp_json.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    usage = resp_json.get("usage")
    base = {
        "id": resp_json.get("id") or "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion.chunk",
        "created": resp_json.get("created") or int(time.time()),
        "model": model,
    }
    chunks: list[str] = []

    def emit(delta: dict, finish: str | None = None, chunk_usage: dict | None = None) -> None:
        o = dict(base)
        o["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
        if chunk_usage is not None:
            o["usage"] = chunk_usage
        chunks.append("data: " + json.dumps(o, ensure_ascii=False) + "\n\n")

    emit({"role": "assistant"})
    reasoning = reasoning_from(msg)
    if reasoning:
        # Mirror under both field names; @ai-sdk/xai reads only reasoning_content.
        emit({"reasoning_content": reasoning, "reasoning": reasoning})
    if msg.get("content"):
        emit({"content": msg["content"]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        emit(
            {
                "tool_calls": [
                    {
                        "index": i,
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "",
                        },
                    }
                ]
            }
        )
    finish_reason = ch.get("finish_reason") or ("tool_calls" if msg.get("tool_calls") else "stop")
    emit({}, finish_reason, chunk_usage=usage)

    if usage and include_usage:
        empty_chunk = dict(base)
        empty_chunk["choices"] = []
        empty_chunk["usage"] = usage
        chunks.append("data: " + json.dumps(empty_chunk, ensure_ascii=False) + "\n\n")

    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


class UpstreamClient:
    def __init__(self, cfg: "Config", api_key: str = "", base_url: str = "") -> None:
        self.cfg = cfg
        self.api_key = api_key
        # Per-upstream override wins; otherwise the protocol's global default.
        self.base: str = (base_url or cfg.upstream).rstrip("/")

    def _url(self, path: str) -> str:
        return self.base + path

    def _auth_headers(self, extra: dict | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if extra:
            for k in ("Accept", "User-Agent"):
                if k in extra and extra[k]:
                    h[k] = extra[k]
        return h

    def get(self, path: str, timeout: int = 60) -> tuple[int, bytes, str]:
        req = urllib.request.Request(self._url(path), headers=self._auth_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")

    def post(
        self, path: str, payload: bytes, timeout: int = 600
    ) -> tuple[int, bytes, str]:
        req = urllib.request.Request(
            self._url(path), data=payload, headers=self._auth_headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")


class AnthropicUpstreamClient(UpstreamClient):
    """Client for Anthropic Messages API nodes (Claude Code / Anthropic SDK)."""

    def _url(self, path: str) -> str:
        return anthropic_endpoint(self.base, path)

    def _auth_headers(self, extra: dict | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": f"step-tool-proxy/{__version__}",
        }
        if extra:
            for k in ("Accept", "User-Agent"):
                if k in extra and extra[k]:
                    h[k] = extra[k]
        return h


class UpstreamTarget:
    """Resolved upstream for one request: node kind + base URL + key."""

    def __init__(self, kind: str = "stepfun", base: str = "", api_key: str = "") -> None:
        self.kind = normalize_upstream_type(kind)
        self.base = (base or "").rstrip("/")
        self.api_key = api_key or ""


def resolve_target(
    cfg: "Config", kind: str = "stepfun", base: str = "", api_key: str = ""
) -> UpstreamTarget:
    """Pick the effective base URL for an upstream node.

    ``base`` is the per-upstream override (empty when the WebUI left it blank);
    the global default for the node kind applies otherwise.
    """
    kind = normalize_upstream_type(kind)
    if kind not in UPSTREAM_TYPES:
        kind = "stepfun"
    fallback = cfg.anthropic_upstream if kind == "anthropic" else cfg.upstream
    return UpstreamTarget(kind, base or fallback, api_key)


def _anthropic_client(target: UpstreamTarget, cfg: "Config") -> AnthropicUpstreamClient:
    return AnthropicUpstreamClient(cfg, target.api_key, base_url=target.base)


def strip_reasoning_from_messages(messages: Any) -> Any:
    """Drop reasoning fields from assistant messages.

    StepFun rejects `reasoning_content` on assistant messages that also carry
    `tool_calls`, so it is removed from every assistant turn unless
    FORWARD_REASONING_HISTORY is on.
    """
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    changed = False
    for msg in messages:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            and ("reasoning" in msg or "reasoning_content" in msg)
        ):
            msg = {
                k: v
                for k, v in msg.items()
                if k not in ("reasoning", "reasoning_content")
            }
            changed = True
        out.append(msg)
    return out if changed else messages


def normalize_content_for_stepfun(content: Any) -> Any:
    """Normalize multimodal message content for StepFun OpenAI-compatible API."""
    if not isinstance(content, list):
        return content
    out: list[dict] = []
    changed = False
    for p in content:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
            changed = True
            continue
        if not isinstance(p, dict):
            out.append(p)
            continue
        ptype = p.get("type")
        if ptype in ("text", "input_text"):
            text = p.get("text", "")
            if ptype != "text":
                changed = True
            out.append({"type": "text", "text": text})
        elif ptype in ("image_url", "input_image", "image"):
            changed = True
            url = None
            detail = p.get("detail")
            if isinstance(p.get("source"), dict):
                src = p["source"]
                stype = src.get("type")
                if stype == "base64":
                    mt = src.get("media_type") or "image/png"
                    data = (src.get("data") or "").strip()
                    url = f"data:{mt};base64,{data}"
                elif stype == "url":
                    url = src.get("url")
            else:
                iu = p.get("image_url") or p.get("image") or p.get("url")
                if isinstance(iu, dict):
                    url = iu.get("url")
                    detail = iu.get("detail") or detail
                elif isinstance(iu, str):
                    url = iu
                elif isinstance(p.get("data"), str):
                    mt = p.get("media_type") or "image/png"
                    url = f"data:{mt};base64,{p['data'].strip()}"
            if url:
                entry: dict[str, Any] = {"url": url}
                if detail:
                    entry["detail"] = detail
                out.append({"type": "image_url", "image_url": entry})
            else:
                out.append(p)
        elif ptype in ("video_url", "input_video", "video"):
            vu = p.get("video_url") or p.get("video") or p.get("url")
            url = vu.get("url") if isinstance(vu, dict) else (vu if isinstance(vu, str) else None)
            if url:
                changed = True
                out.append({"type": "video_url", "video_url": {"url": url}})
            else:
                out.append(p)
        elif ptype in ("input_audio", "audio"):
            ia = p.get("input_audio") or p.get("audio")
            changed = True
            if isinstance(ia, dict):
                out.append({"type": "input_audio", "input_audio": ia})
            elif isinstance(p.get("data"), str):
                out.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": p["data"],
                            "format": p.get("format", "wav"),
                        },
                    }
                )
            else:
                out.append(p)
        else:
            out.append(p)
    return out if changed else content


def normalize_messages_for_stepfun(messages: Any) -> Any:
    """Normalize multimodal message content for StepFun OpenAI-compatible API."""
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        new_content = normalize_content_for_stepfun(content)
        if new_content is not content:
            changed = True
            msg = dict(msg, content=new_content)
        out.append(msg)
    return out if changed else messages


def handle_chat_completions(
    cfg: "Config",
    body: dict,
    raw: bytes,
    log: Callable[[str], None] | None = None,
    api_key: str = "",
    upstream: UpstreamTarget | None = None,
) -> tuple[int, bytes, str]:
    """Handle /v1/chat/completions against the upstream node of this request.

    ``upstream`` selects the node kind (StepFun Plan or Claude Code /
    Anthropic SDK) and its base URL; it comes from the client token's bound
    upstream. Returns (status, body_bytes, content_type).
    """
    return _handle(cfg, "chat", body, raw, log, api_key, upstream)


def handle_responses(
    cfg: "Config",
    body: dict,
    raw: bytes,
    log: Callable[[str], None] | None = None,
    api_key: str = "",
    upstream: UpstreamTarget | None = None,
) -> tuple[int, bytes, str]:
    """Handle /v1/responses: convert to chat.completions, call the upstream
    node, convert the response back into Responses API shapes."""
    return _handle(cfg, "responses", body, raw, log, api_key, upstream)


def _handle(
    cfg: "Config",
    api: str,
    body: dict,
    raw: bytes,
    log: Callable[[str], None] | None,
    api_key: str,
    upstream: UpstreamTarget | None,
) -> tuple[int, bytes, str]:
    _log = log or (lambda m: None)
    target = upstream or UpstreamTarget("stepfun", cfg.upstream, api_key)

    # Downstream normalization: turn the Responses API into chat.completions
    # so both upstream node kinds share one code path.
    if api == "responses":
        chat_body = responses_request_to_chat(body)
        chat_raw = json.dumps(chat_body, ensure_ascii=False).encode()
    else:
        chat_body, chat_raw = body, raw

    if target.kind == "anthropic":
        status, data, ct = _call_anthropic(cfg, target, chat_body, chat_raw, _log)
    else:
        status, data, ct = _call_stepfun(cfg, target, chat_body, chat_raw, _log)

    if status != 200:
        return status, data, ct

    # Upstream normalization back into the downstream format.
    if api == "responses":
        if (ct or "").startswith("text/event-stream"):
            data = chat_sse_to_responses_sse(
                data, chat_body.get("model") or "", include_usage=_wants_usage(chat_body)
            )
            ct = "text/event-stream"
            _log(f"responses SSE ok ({len(data)} bytes)")
        else:
            try:
                data = json.dumps(
                    chat_response_to_responses(json.loads(data)), ensure_ascii=False
                ).encode()
            except Exception:
                return (
                    502,
                    json.dumps(
                        {"error": {"message": "invalid upstream JSON", "type": "proxy_error"}}
                    ).encode(),
                    "application/json",
                )
            _log("responses JSON ok")
    return status, data, ct


def rewrite_sse_event(obj: dict) -> bytes:
    """Alias reasoning + sanitize tool_calls in one chat.completion.chunk.

    Per-event counterpart of ``rewrite_sse`` for the incremental stream path.
    """
    choices = obj.get("choices")
    if not choices:
        return b""
    ch0 = choices[0]
    delta = with_reasoning_aliases(ch0.get("delta") or {})
    tcs = delta.get("tool_calls")
    if tcs:
        new_tcs = [s for s in (sanitize_tool_delta(tc) for tc in tcs if isinstance(tc, dict)) if s]
        if new_tcs:
            ch0 = dict(ch0, delta=dict(delta, tool_calls=new_tcs))
        else:
            trimmed = {k: v for k, v in delta.items() if k != "tool_calls"}
            ch0 = dict(ch0, delta=trimmed)
    elif delta is not ch0.get("delta"):
        ch0 = dict(ch0, delta=delta)
    obj = dict(obj, choices=[ch0] + list(choices[1:]))
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def _stream_lines(client: UpstreamClient, path: str, payload: bytes):
    """POST to the upstream and yield SSE lines as they arrive.

    Raises UpstreamError for non-200 / unreachable upstreams so the caller can
    answer the client with JSON before any stream bytes were written.
    """
    req = urllib.request.Request(
        client._url(path), data=payload, headers=client._auth_headers(), method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        ct = e.headers.get("Content-Type", "application/json") if e.headers else "application/json"
        raise UpstreamError(e.code, e.read(), ct) from None
    except urllib.error.URLError as e:
        raise UpstreamError(
            502,
            json.dumps(
                {"error": {"message": f"upstream unreachable: {e}", "type": "proxy_error"}}
            ).encode(),
        ) from None
    with resp:
        if resp.status != 200:
            raise UpstreamError(resp.status, resp.read(), "application/json")
        for line in resp:
            yield line


def stream_stepfun_chat(cfg: "Config", target: UpstreamTarget, body: dict, _log) -> Iterator[bytes]:
    """True streaming for StepFun nodes: forward each upstream SSE event as
    soon as it arrives (plain-stream and sanitize paths).

    FORCE_BUFFER stays buffered by design — the upstream call is non-stream so
    there is nothing to forward incrementally — but still goes through here as
    a single chunk.
    """
    model = body.get("model") or ""
    body = dict(body, model=model)
    client = UpstreamClient(cfg, target.api_key, base_url=target.base)
    tools = body.get("tools") or []
    has_tools = bool(tools)

    # Same history-stripping as the buffered path.
    messages = body.get("messages")
    if not cfg.forward_reasoning_history:
        messages = strip_reasoning_from_messages(messages)
    messages = normalize_messages_for_stepfun(messages)
    body = dict(body, messages=messages)
    upstream_body = dict(body, reasoning_format="deepseek-style")

    if cfg.force_buffer and has_tools:
        fixed = dict(upstream_body, stream=False)
        _log(f"FORCE_BUFFER (buffered) model={model} tools={len(tools)}")
        try:
            status, data, _ct = client.post("/chat/completions", json.dumps(fixed).encode())
        except Exception as e:  # keep parity with the buffered handler
            _log(f"FORCE_BUFFER upstream failure: {e}")
            return
        if status != 200:
            raise UpstreamError(status, data, "application/json")
        try:
            resp = json.loads(data)
        except Exception:
            raise UpstreamError(502, json.dumps({"error": "invalid upstream JSON"}).encode()) from None
        yield buffer_to_sse(resp, model, include_usage=_wants_usage(body))
        return

    _log(f"streaming (incremental) model={model} tools={len(tools)} force_buffer={cfg.force_buffer}")
    decoder = SSEDecoder()
    for line in _stream_lines(client, "/chat/completions", json.dumps(upstream_body).encode()):
        if line.strip() == b"data: [DONE]":
            yield b"data: [DONE]\n\n"
            continue
        for event in decoder.feed_line(line):
            chunk = rewrite_sse_event(event)
            if chunk:
                yield chunk
    for event in decoder.flush():
        chunk = rewrite_sse_event(event)
        if chunk:
            yield chunk


def stream_anthropic_chat(cfg: "Config", target: UpstreamTarget, body: dict, _log) -> Iterator[bytes]:
    """True streaming for Anthropic nodes: convert each Anthropic SSE event
    into chat.completion chunks as it arrives."""
    model = body.get("model") or ""
    body = dict(body, model=model)
    tools = body.get("tools") or []
    _log(f"streaming (incremental anthropic) model={model} tools={len(tools)} base={target.base}")
    # Reasoning is never echoed back: Anthropic rejects thinking blocks
    # without their original signatures, and the proxy drops them upstream.
    messages = strip_reasoning_from_messages(body.get("messages"))
    upstream_body = chat_request_to_anthropic(
        {**body, "messages": messages}, default_max_tokens=cfg.anthropic_max_tokens
    )
    upstream_body["stream"] = True
    client = _anthropic_client(target, cfg)
    mapper = AnthropicToChatStream(model, include_usage=_wants_usage(body))
    decoder = SSEDecoder()
    for line in _stream_lines(client, "/v1/messages", json.dumps(upstream_body, ensure_ascii=False).encode()):
        for event in decoder.feed_line(line):
            for chunk in mapper.feed(event):
                yield chunk
    for event in decoder.flush():
        for chunk in mapper.feed(event):
            yield chunk
    yield mapper.close()


def stream_chat_completions(
    cfg: "Config",
    body: dict,
    log: Callable[[str], None] | None = None,
    api_key: str = "",
    upstream: UpstreamTarget | None = None,
) -> Iterator[bytes]:
    """Stream /v1/chat/completions SSE incrementally (per upstream event)."""
    _log = log or (lambda m: None)
    target = upstream or UpstreamTarget("stepfun", cfg.upstream, api_key)
    body = dict(body, stream=True)
    if target.kind == "anthropic":
        yield from stream_anthropic_chat(cfg, target, body, _log)
    else:
        yield from stream_stepfun_chat(cfg, target, body, _log)


def stream_responses(
    cfg: "Config",
    body: dict,
    log: Callable[[str], None] | None = None,
    api_key: str = "",
    upstream: UpstreamTarget | None = None,
) -> Iterator[bytes]:
    """Stream /v1/responses events incrementally (per upstream event)."""
    _log = log or (lambda m: None)
    target = upstream or UpstreamTarget("stepfun", cfg.upstream, api_key)
    body = dict(body, stream=True)
    chat_body = responses_request_to_chat(body)
    chat_body["stream"] = True
    mapper = ChatToResponsesStream(chat_body.get("model") or "", include_usage=_wants_usage(chat_body))
    decoder = SSEDecoder()
    for chunk in stream_chat_completions(cfg, chat_body, log=_log, api_key=api_key, upstream=target):
        for line in chunk.splitlines(keepends=True):
            for event in decoder.feed_line(line):
                yield from mapper.feed(event)
    for event in decoder.flush():
        yield from mapper.feed(event)
    yield mapper.close()


def _call_stepfun(
    cfg: "Config",
    target: UpstreamTarget,
    body: dict,
    raw: bytes,
    _log: Callable[[str], None],
) -> tuple[int, bytes, str]:
    """StepFun Plan node (OpenAI-compatible): FORCE_BUFFER or sanitize."""
    client = UpstreamClient(cfg, target.api_key, base_url=target.base)
    tools = body.get("tools") or []
    want_stream = bool(body.get("stream"))
    has_tools = bool(tools)
    model = body.get("model") or ""
    body = dict(body, model=model)

    _log(
        f"chat/completions model={model} stream={want_stream} "
        f"tools={len(tools)} force_buffer={cfg.force_buffer} upstream={target.kind}"
    )

    if cfg.debug_dump and tools:
        dump = cfg.data_dir / f"last_request_{int(time.time())}.json"
        try:
            dump.write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _log(f"debug dump -> {dump}")
        except Exception as e:
            _log(f"debug dump failed: {e}")

    path = "/chat/completions"

    # Stream requests reuse the incremental pipeline and buffered materialize
    # it for direct callers; the HTTP layer streams instead.
    if want_stream:
        try:
            data = b"".join(stream_stepfun_chat(cfg, target, body, _log))
        except UpstreamError as e:
            return e.status, e.body, e.content_type
        return 200, data, "text/event-stream"

    # Follow-up turns: only echo assistant reasoning back when explicitly
    # enabled. Everything downstream must see the stripped body.
    original = body.get("messages")
    messages = original
    if not cfg.forward_reasoning_history:
        messages = strip_reasoning_from_messages(messages)
        if messages is not original:
            _log("stripped reasoning_content from assistant history")
    messages = normalize_messages_for_stepfun(messages)
    if messages is not original:
        body = dict(body, messages=messages)
        raw = json.dumps(body, ensure_ascii=False).encode()

    # Non-stream: alias reasoning in the JSON body
    status, data, ct = client.post(path, raw)
    if status == 200 and "application/json" in (ct or "").lower():
        data = rewrite_json(data)
    return status, data, ct or "application/json"


def _call_anthropic(
    cfg: "Config",
    target: UpstreamTarget,
    body: dict,
    raw: bytes,
    _log: Callable[[str], None],
) -> tuple[int, bytes, str]:
    """Claude Code / Anthropic SDK node: rewrite into the Messages API,
    then convert the JSON or SSE response back to chat.completions."""
    tools = body.get("tools") or []
    want_stream = bool(body.get("stream"))
    model = body.get("model") or ""
    body = dict(body, model=model)
    _log(
        f"anthropic model={model} stream={want_stream} "
        f"tools={len(tools)} base={target.base}"
    )

    # Stream requests reuse the incremental pipeline (buffered here only for
    # direct callers; the HTTP layer streams instead).
    if want_stream:
        try:
            data = b"".join(stream_anthropic_chat(cfg, target, body, _log))
        except UpstreamError as e:
            return e.status, e.body, e.content_type
        return 200, data, "text/event-stream"

    # Reasoning is never echoed back: Anthropic rejects thinking blocks
    # without their original signatures, and the proxy drops them upstream.
    messages = strip_reasoning_from_messages(body.get("messages"))
    upstream_body = chat_request_to_anthropic(
        {**body, "messages": messages}, default_max_tokens=cfg.anthropic_max_tokens
    )
    upstream_body["stream"] = False
    client = _anthropic_client(target, cfg)
    status, data, ct = client.post(
        "/v1/messages", json.dumps(upstream_body, ensure_ascii=False).encode()
    )
    if status != 200:
        return status, data, ct or "application/json"

    try:
        resp = json.loads(data)
    except Exception:
        return 502, json.dumps({"error": "invalid upstream JSON"}).encode(), "application/json"
    chat = anthropic_response_to_chat(resp, model)
    _log("anthropic JSON ok")
    return 200, json.dumps(chat, ensure_ascii=False).encode(), "application/json"


def _wants_usage(body: dict) -> bool:
    opts = body.get("stream_options")
    return bool(isinstance(opts, dict) and opts.get("include_usage"))


def handle_models(
    cfg: "Config",
    api_key: str = "",
    upstream: UpstreamTarget | None = None,
) -> tuple[int, bytes, str]:
    target = upstream or UpstreamTarget("stepfun", cfg.upstream, api_key)
    if target.kind == "anthropic":
        client = _anthropic_client(target, cfg)
        status, data, ct = client.get("/v1/models")
        if status != 200:
            status, data, ct = client.get("/models")
        if status != 200:
            fallback_models = {
                "object": "list",
                "data": [
                    {"id": "step-5-preview", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "step-3.7-flash", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "step-3.5-flash", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "step-1v-8k", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "step-1v-32k", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "step-1.5v-mini", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "stepaudio-2.5-chat", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "claude-3-7-sonnet-20250219", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "claude-3-5-sonnet-20241022", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                    {"id": "claude-3-5-haiku-20241022", "object": "model", "created": int(time.time()), "owned_by": "stepai"},
                ],
            }
            return 200, json.dumps(fallback_models).encode(), "application/json"
        try:
            models = anthropic_models_to_openai(json.loads(data))
        except Exception:
            return 502, json.dumps({"error": "invalid upstream JSON"}).encode(), "application/json"
        return 200, json.dumps(models, ensure_ascii=False).encode(), "application/json"
    client = UpstreamClient(cfg, target.api_key, base_url=target.base)
    return client.get("/models")
