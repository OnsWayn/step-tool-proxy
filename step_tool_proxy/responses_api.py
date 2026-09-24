"""OpenAI Responses API <-> Chat Completions conversion.

The proxy exposes ``/v1/responses`` to downstream clients. Requests are
rewritten into chat.completions form so the same upstream handling (StepFun
Plan or an Anthropic Messages node) applies, and responses — JSON bodies and
SSE streams — are rewritten back into Responses API shapes.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .anthropic_api import iter_sse_events


def _parts_text(content: Any) -> str:
    """Flatten Responses API content parts into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return ""


def _convert_part_to_chat(part: Any) -> dict | None:
    """Convert a Responses API content part or top-level item to an OpenAI Chat Completions part."""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    ptype = part.get("type")
    if ptype in ("text", "input_text", "output_text"):
        return {"type": "text", "text": part.get("text", "")}
    if ptype in ("image_url", "input_image", "image"):
        iu = part.get("image_url") or part.get("image") or part.get("url")
        url = None
        detail = None
        if isinstance(iu, dict):
            url = iu.get("url")
            detail = iu.get("detail") or part.get("detail")
        elif isinstance(iu, str):
            url = iu
            detail = part.get("detail")
        elif isinstance(part.get("data"), str):
            mt = part.get("media_type") or "image/png"
            url = f"data:{mt};base64,{part['data'].strip()}"
            detail = part.get("detail")
        elif isinstance(part.get("source"), dict):
            src = part["source"]
            if src.get("type") == "base64":
                mt = src.get("media_type") or "image/png"
                url = f"data:{mt};base64,{src.get('data', '').strip()}"
            elif src.get("type") == "url":
                url = src.get("url")
        if url:
            entry: dict[str, Any] = {"url": url}
            if detail:
                entry["detail"] = detail
            return {"type": "image_url", "image_url": entry}
        return None
    if ptype in ("video_url", "input_video", "video"):
        vu = part.get("video_url") or part.get("video") or part.get("url")
        url = vu.get("url") if isinstance(vu, dict) else (vu if isinstance(vu, str) else None)
        if url:
            return {"type": "video_url", "video_url": {"url": url}}
        return None
    if ptype in ("input_audio", "audio"):
        ia = part.get("input_audio") or part.get("audio")
        if isinstance(ia, dict):
            return {"type": "input_audio", "input_audio": ia}
        elif isinstance(part.get("data"), str):
            return {
                "type": "input_audio",
                "input_audio": {
                    "data": part["data"],
                    "format": part.get("format", "wav"),
                },
            }
        return None
    if ptype in ("file", "input_file", "document"):
        fu = part.get("file_url") or part.get("url")
        if fu:
            return {"type": "file", "file_url": fu if isinstance(fu, dict) else {"url": fu}}
        return None
    return None


def _responses_content_to_chat(content: Any) -> Any:
    """Convert Responses API message content (str or list of parts) into chat.completions content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        converted: list[dict] = []
        has_multimodal = False
        for p in content:
            c = _convert_part_to_chat(p)
            if c:
                converted.append(c)
                if c.get("type") != "text":
                    has_multimodal = True
        if not converted:
            return ""
        if has_multimodal:
            return converted
        if len(converted) == 1:
            return converted[0].get("text", "")
        return "".join(c.get("text", "") for c in converted)
    return ""


def _chat_tool_choice(choice: Any) -> Any:
    if choice == "none":
        return "none"
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def responses_request_to_chat(body: dict) -> dict:
    """Rewrite a Responses API request body into a chat.completions body."""
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        text = _parts_text(instructions)
        if text.strip():
            messages.append({"role": "system", "content": text})

    inp = body.get("input")
    items: list[Any]
    if isinstance(inp, str):
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": inp}]}] if inp else []
    elif isinstance(inp, list):
        items = inp
    elif isinstance(body.get("messages"), list):
        items = body["messages"]
    else:
        items = []

    for item in items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in (None, "message"):
            role = item.get("role") or "user"
            content = item.get("content")
            if role in ("system", "developer"):
                text = _parts_text(content) if isinstance(content, list) else (content or "")
                if text.strip():
                    messages.append({"role": "system", "content": text})
            else:
                chat_content = _responses_content_to_chat(content)
                if chat_content != "" and chat_content != []:
                    msg_entry: dict[str, Any] = {"role": role, "content": chat_content}
                    if item.get("tool_calls"):
                        msg_entry["tool_calls"] = item["tool_calls"]
                    messages.append(msg_entry)
                elif item.get("tool_calls"):
                    messages.append({"role": role, "content": None, "tool_calls": item["tool_calls"]})
        elif itype in (
            "input_text",
            "text",
            "input_image",
            "image_url",
            "image",
            "input_video",
            "video_url",
            "input_audio",
            "audio",
            "file",
            "input_file",
        ):
            part = _convert_part_to_chat(item)
            if part:
                if messages and messages[-1].get("role") == "user":
                    last_content = messages[-1].get("content")
                    if isinstance(last_content, list):
                        messages[-1]["content"].append(part)
                    elif isinstance(last_content, str):
                        messages[-1]["content"] = [{"type": "text", "text": last_content}, part]
                    else:
                        messages[-1]["content"] = [part]
                else:
                    messages.append({"role": "user", "content": [part]})
        elif itype == "function_call":
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = item.get("name") or fn.get("name") or ""
            arguments = item.get("arguments") or fn.get("arguments") or "{}"
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}"
            tc = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(tc)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        elif itype == "function_call_output":
            out = item.get("output")
            if isinstance(out, list):
                out = "".join(p.get("text", "") for p in out if isinstance(p, dict) and isinstance(p.get("text"), str))
            if not isinstance(out, str):
                out = "" if out is None else json.dumps(out, ensure_ascii=False)
            messages.append(
                {"role": "tool", "tool_call_id": item.get("call_id") or "", "content": out}
            )
        # reasoning items and custom item types carry no chat.completions
        # equivalent and are dropped.

    chat: dict[str, Any] = {
        "model": body.get("model") or "",
        "messages": messages,
    }
    max_output_tokens = body.get("max_output_tokens") or body.get("max_tokens")
    if isinstance(max_output_tokens, int):
        chat["max_tokens"] = max_output_tokens
    if body.get("temperature") is not None:
        chat["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        chat["top_p"] = body["top_p"]
    if body.get("stream"):
        chat["stream"] = True
        stream_options = body.get("stream_options")
        if isinstance(stream_options, dict) and stream_options.get("include_usage"):
            chat["stream_options"] = {"include_usage": True}
    if body.get("reasoning_effort") is not None:
        chat["reasoning_effort"] = body["reasoning_effort"]
    if body.get("reasoning") is not None:
        chat["reasoning"] = body["reasoning"]
    if body.get("thinking") is not None:
        chat["thinking"] = body["thinking"]

    tools: list[dict] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict) or t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = t.get("name") or fn.get("name") or ""
        if not name:
            continue
        entry: dict[str, Any] = {"name": name}
        description = t.get("description") or fn.get("description")
        if description:
            entry["description"] = description
        entry["parameters"] = t.get("parameters") or fn.get("parameters") or {"type": "object", "properties": {}}
        tools.append({"type": "function", "function": entry})
    if tools:
        chat["tools"] = tools
        chat["tool_choice"] = _chat_tool_choice(body.get("tool_choice"))
    return chat


def _responses_usage(chat_usage: Any) -> dict:
    chat_usage = chat_usage or {}

    def _int(key: str) -> int:
        try:
            return int(chat_usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    it, ot = _int("prompt_tokens"), _int("completion_tokens")
    details = chat_usage.get("prompt_tokens_details") or {}
    cached = 0
    try:
        cached = int(details.get("cached_tokens") or chat_usage.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        cached = 0
    out_details = chat_usage.get("completion_tokens_details") or {}
    reasoning_tokens = 0
    try:
        reasoning_tokens = int(
            out_details.get("reasoning_tokens")
            or chat_usage.get("reasoning_tokens")
            or 0
        )
    except (TypeError, ValueError):
        reasoning_tokens = 0
    return {
        "input_tokens": it,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": ot,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": it + ot,
    }


def chat_response_to_responses(chat: dict) -> dict:
    """Rewrite a chat.completion JSON into a Responses API response JSON."""
    choices = chat.get("choices") or [{}]
    msg = (choices[0] if choices else {}).get("message") or {}
    output: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "type": "reasoning",
                "id": "rs_" + uuid.uuid4().hex[:24],
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    text = msg.get("content")
    content_parts: list[dict] = []
    if isinstance(text, str) and text:
        content_parts.append({"type": "output_text", "text": text, "annotations": []})
    elif isinstance(text, list):
        for p in text:
            if isinstance(p, str):
                content_parts.append({"type": "output_text", "text": p, "annotations": []})
            elif isinstance(p, dict):
                ptype = p.get("type")
                if ptype in ("text", "output_text") and isinstance(p.get("text"), str):
                    content_parts.append({"type": "output_text", "text": p["text"], "annotations": p.get("annotations", [])})
                else:
                    content_parts.append(p)
    if content_parts:
        output.append(
            {
                "type": "message",
                "id": "msg_" + uuid.uuid4().hex[:24],
                "status": "completed",
                "role": "assistant",
                "content": content_parts,
            }
        )
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": "fc_" + uuid.uuid4().hex[:24],
                "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
                "status": "completed",
            }
        )
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": chat.get("created") or int(time.time()),
        "status": "completed",
        "model": chat.get("model") or "",
        "output": output,
        "usage": _responses_usage(chat.get("usage")),
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


class ChatToResponsesStream:
    """Feed chat.completion chunk SSE events; emit Responses API events.

    ``feed`` converts one upstream chunk so the proxy can forward each event
    downstream as soon as it arrives; ``close`` emits the terminal events.
    """

    def __init__(self, model: str, include_usage: bool = False) -> None:
        self.model = model
        self.include_usage = include_usage
        self.resp_id = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.msg_item_id = "msg_" + uuid.uuid4().hex[:24]
        self.reasoning_item_id = "rs_" + uuid.uuid4().hex[:24]
        self.seq = 0
        self.lines: list[str] = []
        self.text_chunks: list[str] = []
        self.text_index: int | None = None
        self.text_open = False
        self.reasoning_chunks: list[str] = []
        self.reasoning_index: int | None = None
        self.reasoning_open = False
        self.reasoning_part_open = False
        self.tool_items: dict[int, dict] = {}
        self.next_index = 0
        self.usage: dict | None = None
        self.started = False

    def _emit(self, etype: str, payload: dict) -> bytes:
        obj = {"type": etype, "sequence_number": self.seq}
        obj.update(payload)
        self.seq += 1
        return (f"event: {etype}\n" + "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

    def _start(self) -> bytes:
        if self.started:
            return b""
        self.started = True
        skeleton = {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created,
            "status": "in_progress",
            "model": self.model,
            "output": [],
            "error": None,
            "usage": None,
            "incomplete_details": None,
        }
        return self._emit("response.created", {"response": dict(skeleton)}) + self._emit(
            "response.in_progress", {"response": dict(skeleton)}
        )

    def _open_reasoning(self) -> list[bytes]:
        if self.reasoning_open:
            return []
        self.reasoning_open = True
        self.reasoning_index = self.next_index
        self.next_index += 1
        return [
            self._emit(
                "response.output_item.added",
                {
                    "output_index": self.reasoning_index,
                    "item": {"id": self.reasoning_item_id, "type": "reasoning", "summary": []},
                },
            )
        ]

    def _open_reasoning_part(self) -> list[bytes]:
        if self.reasoning_part_open:
            return []
        self.reasoning_part_open = True
        return [
            self._emit(
                "response.reasoning_summary_part.added",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            )
        ]

    def _close_reasoning(self) -> list[bytes]:
        if not self.reasoning_open:
            return []
        self.reasoning_open = False
        joined = "".join(self.reasoning_chunks)
        out = [
            self._emit(
                "response.reasoning_summary_text.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "text": joined,
                },
            ),
            self._emit(
                "response.reasoning_summary_part.done",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": self.reasoning_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": joined},
                },
            ),
            self._emit(
                "response.output_item.done",
                {
                    "output_index": self.reasoning_index,
                    "item": {
                        "id": self.reasoning_item_id,
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": joined}],
                    },
                },
            ),
        ]
        return out

    def _open_text(self) -> list[bytes]:
        if self.text_open:
            return []
        self.text_open = True
        self.text_index = self.next_index
        self.next_index += 1
        return [
            self._emit(
                "response.output_item.added",
                {
                    "output_index": self.text_index,
                    "item": {
                        "id": self.msg_item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            ),
            self._emit(
                "response.content_part.added",
                {
                    "item_id": self.msg_item_id,
                    "output_index": self.text_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _close_text(self) -> list[bytes]:
        if not self.text_open:
            return []
        self.text_open = False
        joined = "".join(self.text_chunks)
        return [
            self._emit(
                "response.output_text.done",
                {"item_id": self.msg_item_id, "output_index": self.text_index, "content_index": 0, "text": joined},
            ),
            self._emit(
                "response.content_part.done",
                {
                    "item_id": self.msg_item_id,
                    "output_index": self.text_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": joined, "annotations": []},
                },
            ),
            self._emit(
                "response.output_item.done",
                {
                    "output_index": self.text_index,
                    "item": {
                        "id": self.msg_item_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": joined, "annotations": []}],
                    },
                },
            ),
        ]

    def feed(self, event: dict) -> list[bytes]:
        out: list[bytes] = [self._start()]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.extend(self._open_reasoning())
                out.extend(self._open_reasoning_part())
                self.reasoning_chunks.append(reasoning)
                out.append(
                    self._emit(
                        "response.reasoning_summary_text.delta",
                        {
                            "item_id": self.reasoning_item_id,
                            "output_index": self.reasoning_index,
                            "summary_index": 0,
                            "delta": reasoning,
                        },
                    )
                )
            content = delta.get("content")
            if isinstance(content, str) and content:
                if self.reasoning_open:
                    out.extend(self._close_reasoning())
                out.extend(self._open_text())
                self.text_chunks.append(content)
                out.append(
                    self._emit(
                        "response.output_text.delta",
                        {
                            "item_id": self.msg_item_id,
                            "output_index": self.text_index,
                            "content_index": 0,
                            "delta": content,
                        },
                    )
                )
            for tcd in delta.get("tool_calls") or []:
                if not isinstance(tcd, dict):
                    continue
                if self.reasoning_open:
                    out.extend(self._close_reasoning())
                index = tcd.get("index", 0)
                item = self.tool_items.get(index)
                fn = tcd.get("function") or {}
                if item is None:
                    item = {
                        "item_id": "fc_" + uuid.uuid4().hex[:24],
                        "call_id": "",
                        "name": "",
                        "args": "",
                        "started": False,
                        "output_index": None,
                    }
                    self.tool_items[index] = item
                if tcd.get("id"):
                    item["call_id"] = tcd["id"]
                if fn.get("name"):
                    item["name"] = fn["name"]
                if not item["started"]:
                    item["started"] = True
                    item["output_index"] = self.next_index
                    self.next_index += 1
                    out.append(
                        self._emit(
                            "response.output_item.added",
                            {
                                "output_index": item["output_index"],
                                "item": {
                                    "id": item["item_id"],
                                    "type": "function_call",
                                    "status": "in_progress",
                                    "call_id": item["call_id"],
                                    "name": item["name"],
                                    "arguments": "",
                                },
                            },
                        )
                    )
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    item["args"] += args
                    out.append(
                        self._emit(
                            "response.function_call_arguments.delta",
                            {
                                "item_id": item["item_id"],
                                "output_index": item["output_index"],
                                "delta": args,
                            },
                        )
                    )
        if event.get("usage"):
            self.usage = event["usage"]
        return out

    def close(self) -> bytes:
        out: list[bytes] = [self._start()]
        out.extend(self._close_reasoning())
        out.extend(self._close_text())
        for item in sorted(self.tool_items.values(), key=lambda i: i["output_index"]):
            out.append(
                self._emit(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item["item_id"],
                        "output_index": item["output_index"],
                        "arguments": item["args"],
                        "name": item["name"],
                    },
                )
            )
            out.append(
                self._emit(
                    "response.output_item.done",
                    {
                        "output_index": item["output_index"],
                        "item": {
                            "id": item["item_id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": item["call_id"],
                            "name": item["name"],
                            "arguments": item["args"],
                        },
                    },
                )
            )

        final_items: list[tuple[int, dict]] = []
        if self.reasoning_index is not None:
            final_items.append(
                (
                    self.reasoning_index,
                    {
                        "type": "reasoning",
                        "id": self.reasoning_item_id,
                        "summary": [{"type": "summary_text", "text": "".join(self.reasoning_chunks)}],
                    },
                )
            )
        if self.text_index is not None:
            joined = "".join(self.text_chunks)
            final_items.append(
                (
                    self.text_index,
                    {
                        "type": "message",
                        "id": self.msg_item_id,
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": joined, "annotations": []}],
                    },
                )
            )
        for item in self.tool_items.values():
            final_items.append(
                (
                    item["output_index"],
                    {
                        "type": "function_call",
                        "id": item["item_id"],
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": item["args"],
                        "status": "completed",
                    },
                )
            )
        final_items.sort(key=lambda pair: pair[0])
        final_output = [item for _, item in final_items]
        completed = {
            "id": self.resp_id,
            "object": "response",
            "created_at": self.created,
            "status": "completed",
            "model": self.model,
            "output": final_output,
            "error": None,
            "usage": _responses_usage(self.usage),
            "incomplete_details": None,
        }
        out.append(self._emit("response.completed", {"response": completed}))
        return b"".join(out)


def chat_sse_to_responses_sse(raw: bytes, model: str, include_usage: bool = False) -> bytes:
    """Rewrite a chat.completion chunk SSE stream into Responses API events."""
    stream = ChatToResponsesStream(model, include_usage=include_usage)
    out: list[bytes] = []
    for event in iter_sse_events(raw):
        out.extend(stream.feed(event))
    out.append(stream.close())
    return b"".join(out)
