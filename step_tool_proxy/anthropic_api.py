"""Chat Completions <-> Anthropic Messages API conversion.

Used by upstream nodes of type "anthropic" (Claude Code / Anthropic SDK
endpoints): the proxy accepts OpenAI-shaped requests from downstream clients,
rewrites them into the Anthropic Messages API, and converts responses — whole
JSON bodies and SSE streams — back into the OpenAI chat.completions shapes.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192

# Anthropic stop_reason -> OpenAI finish_reason
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "tool_calls",
    "refusal": "content_filter",
}


def anthropic_endpoint(base: str, path: str) -> str:
    """Join a base URL with an Anthropic API path (``/v1/messages`` etc.).

    Accepts bases with or without a trailing ``/v1`` so relay URLs such as
    ``https://host/api`` and ``https://host/v1`` both work.
    """
    root = (base or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if root.endswith("/v1"):
        return root + path[len("/v1") :]
    return root + path


def stop_reason_to_finish(reason: Any) -> str:
    return _STOP_REASON_MAP.get(reason, "stop") if isinstance(reason, str) else "stop"


def _text_from_content(content: Any, joiner: str = "") -> str:
    """Flatten OpenAI message content (string or part list) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return joiner.join(parts)
    return ""


def _anthropic_blocks_from_content(content: Any) -> list[dict]:
    """Convert OpenAI user content into Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict] = []
    for p in content:
        if isinstance(p, str):
            blocks.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype in ("text", "input_text", "output_text") and isinstance(p.get("text"), str):
            blocks.append({"type": "text", "text": p["text"]})
        elif ptype == "image" and isinstance(p.get("source"), dict):
            blocks.append({"type": "image", "source": p["source"]})
        elif ptype == "image_url":
            url = None
            iu = p.get("image_url")
            if isinstance(iu, dict):
                url = iu.get("url")
            elif isinstance(iu, str):
                url = iu
            if not url:
                continue
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                media_type, b64 = url[5:].split(";base64,", 1)
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type or "image/png",
                            "data": b64,
                        },
                    }
                )
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _append_message(messages: list[dict], role: str, *blocks: dict) -> None:
    """Append blocks, merging consecutive same-role messages (Anthropic requires
    user/assistant alternation, while clients may send several in a row)."""
    clean = [b for b in blocks if b]
    if not clean:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(clean)
    else:
        messages.append({"role": role, "content": list(clean)})


def _resolve_max_tokens(body: dict, default_max_tokens: int) -> int:
    for key in ("max_tokens", "max_completion_tokens"):
        val = body.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return default_max_tokens if default_max_tokens > 0 else DEFAULT_MAX_TOKENS


def _anthropic_tool_choice(choice: Any) -> dict:
    if isinstance(choice, dict):
        if choice.get("type") == "function" and isinstance(choice.get("function"), dict):
            name = choice["function"].get("name")
            if name:
                return {"type": "tool", "name": name}
        elif choice.get("type") == "tool" and choice.get("name"):
            return {"type": "tool", "name": choice["name"]}
    elif choice == "required":
        return {"type": "any"}
    return {"type": "auto"}


def chat_request_to_anthropic(
    body: dict, default_max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict:
    """Rewrite a chat.completions request body into the Anthropic Messages API.

    Reasoning fields are intentionally dropped from history: Anthropic rejects
    thinking blocks without their original signatures, and the proxy never
    asks the upstream to return them.
    """
    system_parts: list[str] = []
    messages: list[dict] = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            text = _text_from_content(m.get("content")).strip()
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            output = _text_from_content(m.get("content"))
            _append_message(
                messages,
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": output,
                },
            )
            continue
        if role == "assistant":
            blocks: list[dict] = []
            text = _text_from_content(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {"value": args}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                        "name": fn.get("name") or "tool",
                        "input": args,
                    }
                )
            if not blocks:
                # If assistant message has no content and no tool_calls, fallback to reasoning text if any, or a single space
                reasoning = m.get("reasoning_content") or m.get("reasoning")
                if isinstance(reasoning, str) and reasoning.strip():
                    blocks.append({"type": "text", "text": reasoning.strip()})
                else:
                    blocks.append({"type": "text", "text": " "})
            _append_message(messages, "assistant", *blocks)
            continue
        # user (and anything unexpected)
        _append_message(messages, "user", *_anthropic_blocks_from_content(m.get("content")))

    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    elif messages[0].get("role") != "user":
        # Anthropic requires the first message to be from the user
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "Hello"}]})

    out: dict[str, Any] = {
        "model": body.get("model") or "",
        "max_tokens": _resolve_max_tokens(body, default_max_tokens),
        "messages": messages,
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if body.get("thinking"):
        out["thinking"] = body["thinking"]
    elif body.get("reasoning_effort"):
        effort = str(body["reasoning_effort"]).lower()
        budget = 2048
        if effort == "low":
            budget = 1024
        elif effort == "high":
            budget = 4096
        out["thinking"] = {"type": "enabled", "budget_tokens": budget}

    tools: list[dict] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        tools.append(
            {
                "name": name,
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    if tools:
        out["tools"] = tools
        out["tool_choice"] = _anthropic_tool_choice(body.get("tool_choice"))

    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [s for s in stop if isinstance(s, str) and s]
    return out


def _usage_to_openai(usage: Any) -> dict:
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        it = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        it = 0
    try:
        cache_read = int(
            usage.get("cache_read_input_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
    except (TypeError, ValueError):
        cache_read = 0
    try:
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    except (TypeError, ValueError):
        cache_create = 0
    try:
        ot = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        ot = 0

    total_prompt = it + cache_read + cache_create
    res: dict[str, Any] = {
        "prompt_tokens": total_prompt,
        "completion_tokens": ot,
        "total_tokens": total_prompt + ot,
    }
    if cache_read > 0 or cache_create > 0:
        res["prompt_tokens_details"] = {"cached_tokens": cache_read}
    return res


def anthropic_response_to_chat(resp: dict, model: str) -> dict:
    """Rewrite an Anthropic message JSON into a chat.completion JSON."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in resp.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif btype == "thinking" and block.get("thinking"):
            reasoning_parts.append(str(block["thinking"]))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
    }
    if reasoning_parts:
        # Mirror under both names so @ai-sdk/xai keeps the thinking block.
        message["reasoning_content"] = message["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-" + str(resp.get("id") or uuid.uuid4().hex).replace("msg_", "")[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": stop_reason_to_finish(resp.get("stop_reason")),
            }
        ],
        "usage": _usage_to_openai(resp.get("usage")),
    }


class SSEDecoder:
    """Turn an incoming line stream into SSE event payloads.

    Feed raw ``bytes`` lines (``for line in response:``) as they arrive; each
    blank line terminates one event. ``flush`` drains a trailing event that
    was not blank-line terminated.
    """

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed_line(self, line: bytes) -> list[dict]:
        events: list[dict] = []
        if line.endswith(b"\n"):
            line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
        if line == b"":
            if self._data:
                payload = "\n".join(self._data)
                self._data = []
                obj = self._parse(payload)
                if obj is not None:
                    events.append(obj)
            return events
        if line.startswith(b":"):
            return events
        if line.startswith(b"data:"):
            self._data.append(line[5:].decode("utf-8", "replace").lstrip())
        return events

    def flush(self) -> list[dict]:
        events: list[dict] = []
        if self._data:
            payload = "\n".join(self._data)
            self._data = []
            obj = self._parse(payload)
            if obj is not None:
                events.append(obj)
        return events

    @staticmethod
    def _parse(payload: str) -> dict | None:
        if payload.strip() == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None


def iter_sse_events(raw: bytes) -> Iterator[dict]:
    """Yield parsed JSON payloads from an SSE byte stream."""
    decoder = SSEDecoder()
    for line in raw.splitlines(keepends=True):
        for event in decoder.feed_line(line):
            yield event
    for event in decoder.flush():
        yield event


def _chat_chunk(
    chat_id: str, created: int, model: str, delta: dict, finish_reason: str | None = None, usage: dict | None = None
) -> dict:
    obj = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


class AnthropicToChatStream:
    """Feed Anthropic SSE events one by one; emit chat.completion chunk SSE.

    ``feed`` returns the bytes produced by one upstream event so the proxy can
    forward each chunk downstream the moment it arrives.
    """

    def __init__(self, model: str, include_usage: bool = False) -> None:
        self.model = model
        self.include_usage = include_usage
        self.chat_id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.role_emitted = False
        self.finish_reason = "stop"
        self.usage_in = 0
        self.usage_out = 0
        self.cached_tokens = 0
        self.tool_index: dict[int, int] = {}
        self.error_seen: dict | None = None

    def _emit(self, delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> bytes:
        chunk = _chat_chunk(self.chat_id, self.created, self.model, delta, finish_reason, usage)
        return b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"

    def _update_usage(self, usage_raw: Any) -> None:
        if isinstance(usage_raw, dict):
            parsed = _usage_to_openai(usage_raw)
            if parsed.get("prompt_tokens", 0) > 0 or self.usage_in == 0:
                self.usage_in = max(self.usage_in, parsed.get("prompt_tokens", 0))
            if parsed.get("completion_tokens", 0) > 0:
                self.usage_out = max(self.usage_out, parsed.get("completion_tokens", 0))
            details = parsed.get("prompt_tokens_details") or {}
            if details.get("cached_tokens", 0) > 0:
                self.cached_tokens = max(self.cached_tokens, details["cached_tokens"])

    def feed(self, event: dict) -> list[bytes]:
        out: list[bytes] = []
        etype = event.get("type")
        if etype == "error":
            self.error_seen = event.get("error") or {}
            return out

        # Always check for usage on any incoming event
        u = event.get("usage") or (event.get("message") or {}).get("usage")
        if u:
            self._update_usage(u)

        if etype == "message_start":
            if not self.role_emitted:
                out.append(self._emit({"role": "assistant"}))
                self.role_emitted = True
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            index = event.get("index", 0)
            if block.get("type") == "tool_use":
                t_index = len(self.tool_index)
                self.tool_index[index] = t_index
                out.append(
                    self._emit(
                        {
                            "tool_calls": [
                                {
                                    "index": t_index,
                                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                                    "type": "function",
                                    "function": {"name": block.get("name") or "", "arguments": ""},
                                }
                            ]
                        }
                    )
                )
        elif etype == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta" and isinstance(delta.get("text"), str):
                if delta["text"]:
                    out.append(self._emit({"content": delta["text"]}))
            elif dtype == "thinking_delta" and delta.get("thinking"):
                out.append(
                    self._emit(
                        {
                            "reasoning_content": str(delta["thinking"]),
                            "reasoning": str(delta["thinking"]),
                        }
                    )
                )
            elif dtype == "input_json_delta":
                partial = delta.get("partial_json")
                if partial is not None and index in self.tool_index:
                    out.append(
                        self._emit(
                            {"tool_calls": [{"index": self.tool_index[index], "function": {"arguments": partial}}]}
                        )
                    )
        elif etype == "message_delta":
            if (event.get("delta") or {}).get("stop_reason"):
                self.finish_reason = stop_reason_to_finish((event["delta"])["stop_reason"])
        # ping / content_block_stop / message_stop carry nothing to forward
        return out

    def close(self) -> bytes:
        """Final chunk(s) + terminator, emitted once the upstream stream ends."""
        out: list[bytes] = []
        if not self.role_emitted:
            out.append(self._emit({"role": "assistant"}))
        if self.error_seen is not None:
            out.append(self._emit({}, "stop"))
            out.append(
                b"data: "
                + json.dumps(
                    {
                        "error": {
                            "message": str(self.error_seen.get("message") or "upstream error"),
                            "type": str(self.error_seen.get("type") or "anthropic_error"),
                        }
                    },
                    ensure_ascii=False,
                ).encode()
                + b"\n\n"
            )
        else:
            usage_dict: dict[str, Any] = {
                "prompt_tokens": self.usage_in,
                "completion_tokens": self.usage_out,
                "total_tokens": self.usage_in + self.usage_out,
            }
            if self.cached_tokens > 0:
                usage_dict["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}

            # Emit final finish_reason chunk with usage included
            out.append(self._emit({}, self.finish_reason, usage=usage_dict))

            # If client requested stream_options.include_usage (OpenAI official spec), emit choices: [] chunk
            if self.include_usage:
                empty_chunk = {
                    "id": self.chat_id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.model,
                    "choices": [],
                    "usage": usage_dict,
                }
                out.append(b"data: " + json.dumps(empty_chunk, ensure_ascii=False).encode() + b"\n\n")

        out.append(b"data: [DONE]\n\n")
        return b"".join(out)


def anthropic_sse_to_chat_sse(raw: bytes, model: str, include_usage: bool = False) -> bytes:
    """Rewrite an Anthropic SSE stream into chat.completion chunk SSE."""
    stream = AnthropicToChatStream(model, include_usage=include_usage)
    out: list[bytes] = []
    for event in iter_sse_events(raw):
        out.extend(stream.feed(event))
    out.append(stream.close())
    return b"".join(out)


def anthropic_models_to_openai(resp: dict) -> dict:
    """Rewrite an Anthropic /v1/models response into the OpenAI list shape."""
    data: list[dict] = []
    for m in resp.get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        data.append(
            {
                "id": mid,
                "object": "model",
                "created": m.get("created") or m.get("created_at") or 0,
                "owned_by": m.get("owned_by") or "stepfun",
            }
        )
    return {"object": "list", "data": data}
