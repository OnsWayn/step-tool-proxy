"""Upstream proxy + SSE tool_call fix (FORCE_BUFFER / sanitize)."""

from __future__ import annotations

import json
import time
import uuid
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .config import Config

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


def buffer_to_sse(resp_json: dict, model: str) -> bytes:
    """Convert a non-stream chat completion JSON into complete SSE events."""
    ch = (resp_json.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    base = {
        "id": resp_json.get("id") or "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion.chunk",
        "created": resp_json.get("created") or int(time.time()),
        "model": model,
    }
    chunks: list[str] = []

    def emit(delta: dict, finish: str | None = None) -> None:
        o = dict(base)
        o["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
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
    emit(
        {},
        ch.get("finish_reason")
        or ("tool_calls" if msg.get("tool_calls") else "stop"),
    )
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


class UpstreamClient:
    def __init__(self, cfg: "Config", api_key: str = "") -> None:
        self.cfg = cfg
        self.api_key = api_key

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
        url = self.cfg.upstream + path
        req = urllib.request.Request(url, headers=self._auth_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")

    def post(
        self, path: str, payload: bytes, timeout: int = 600
    ) -> tuple[int, bytes, str]:
        url = self.cfg.upstream + path
        req = urllib.request.Request(
            url, data=payload, headers=self._auth_headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")


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


def handle_chat_completions(
    cfg: "Config",
    body: dict,
    raw: bytes,
    log: Callable[[str], None] | None = None,
    api_key: str = "",
) -> tuple[int, bytes, str]:
    """
    Process /v1/chat/completions with FORCE_BUFFER or sanitize.

    ``api_key`` selects which upstream key authenticates the call; it comes from
    the client token's bound upstream. Returns (status, body_bytes, content_type).
    """
    _log = log or (lambda m: None)
    client = UpstreamClient(cfg, api_key)
    tools = body.get("tools") or []
    want_stream = bool(body.get("stream"))
    has_tools = bool(tools)
    model = body.get("model") or "step-5-preview"

    _log(
        f"chat/completions model={model} stream={want_stream} "
        f"tools={len(tools)} force_buffer={cfg.force_buffer}"
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

    # Follow-up turns: only echo assistant reasoning back when explicitly
    # enabled. Everything downstream must see the stripped body.
    if not cfg.forward_reasoning_history:
        original = body.get("messages")
        messages = strip_reasoning_from_messages(original)
        if messages is not original:
            body = dict(body, messages=messages)
            raw = json.dumps(body, ensure_ascii=False).encode()
            _log("stripped reasoning_content from assistant history")

    # Ask StepFun for reasoning_content explicitly; without this it only
    # returns `reasoning`, which clients do not read.
    if want_stream:
        upstream_body = dict(body, reasoning_format="deepseek-style")
    else:
        upstream_body = body

    # FORCE_BUFFER: non-stream upstream, re-emit complete SSE tool_calls
    if cfg.force_buffer and want_stream and has_tools:
        fixed = dict(upstream_body, stream=False)
        status, data, _ct = client.post(path, json.dumps(fixed).encode())
        if status != 200:
            return status, data, "application/json"
        try:
            resp = json.loads(data)
        except Exception:
            return 502, json.dumps({"error": "invalid upstream JSON"}).encode(), "application/json"
        sse = buffer_to_sse(resp, model)
        _log(f"FORCE_BUFFER SSE ok ({len(sse)} bytes)")
        return 200, sse, "text/event-stream"

    # Sanitize path: stream+tools without FORCE_BUFFER
    if want_stream and has_tools and not cfg.force_buffer:
        status, data, _ct = client.post(path, json.dumps(upstream_body).encode())
        if status != 200:
            return status, data, "application/json"
        rewritten = rewrite_sse(data)
        _log(f"sanitized SSE {len(data)}->{len(rewritten)} bytes")
        return 200, rewritten, "text/event-stream"

    # Plain stream without tools: still alias reasoning, but no tool fix needed
    if want_stream:
        status, data, ct = client.post(path, json.dumps(upstream_body).encode())
        if status != 200:
            return status, data, ct or "application/json"
        if (ct or "").startswith("text/event-stream"):
            rewritten = rewrite_sse(data)
            _log(f"stream SSE {len(data)}->{len(rewritten)} bytes")
            return 200, rewritten, "text/event-stream"
        return status, data, ct or "application/json"

    # Non-stream: alias reasoning in the JSON body
    status, data, ct = client.post(path, raw)
    if status == 200 and (ct or "").startswith("application/json"):
        data = rewrite_json(data)
    return status, data, ct or "application/json"


def handle_models(cfg: "Config", api_key: str = "") -> tuple[int, bytes, str]:
    client = UpstreamClient(cfg, api_key)
    return client.get("/models")
