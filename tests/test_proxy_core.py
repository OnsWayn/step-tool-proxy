"""Unit tests for step-tool-proxy core transformations.

Tests coverage:
1. No model name mapping (raw model preserved).
2. Streaming calls guarantee stream=True even if omitted in client body.
3. buffer_to_sse usage preservation (finish_reason chunk + stream_options.include_usage empty chunk).
4. Tool delta sanitization & reasoning stripping.
5. rewrite_json aliasing.
6. Responses API standard reasoning event naming (response.reasoning_summary_*).
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from step_tool_proxy.config import Config
from step_tool_proxy.proxy import (
    UpstreamTarget,
    buffer_to_sse,
    rewrite_json,
    sanitize_tool_delta,
    strip_reasoning_from_messages,
    stream_chat_completions,
    stream_responses,
)
from step_tool_proxy.responses_api import ChatToResponsesStream


class TestProxyCore(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_buffer_to_sse_usage_default(self):
        """buffer_to_sse attaches upstream usage to the finish chunk by default."""
        resp = {
            "id": "chatcmpl-test1",
            "created": 1234567890,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello world!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
        }
        sse_bytes = buffer_to_sse(resp, model="step-5", include_usage=False)
        lines = [line for line in sse_bytes.decode("utf-8").split("\n\n") if line.strip()]

        chunks = []
        for line in lines:
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload != "[DONE]":
                    chunks.append(json.loads(payload))

        # Check model name is kept raw
        for c in chunks:
            self.assertEqual(c["model"], "step-5")

        # The finish chunk (empty delta with finish_reason) should carry usage
        finish_chunks = [c for c in chunks if c["choices"] and c["choices"][0].get("finish_reason") == "stop"]
        self.assertTrue(len(finish_chunks) > 0)
        self.assertEqual(finish_chunks[0].get("usage"), resp["usage"])

        # Without include_usage, choices: [] chunk should NOT be present
        empty_choices_chunks = [c for c in chunks if c.get("choices") == []]
        self.assertEqual(len(empty_choices_chunks), 0)

    def test_buffer_to_sse_usage_with_include_usage(self):
        """When include_usage=True, an extra chunk with choices: [] and usage is emitted before [DONE]."""
        resp = {
            "id": "chatcmpl-test2",
            "created": 1234567890,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Done with tools."},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
            },
        }
        sse_bytes = buffer_to_sse(resp, model="step-5", include_usage=True)
        lines = [line for line in sse_bytes.decode("utf-8").split("\n\n") if line.strip()]

        chunks = []
        for line in lines:
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload != "[DONE]":
                    chunks.append(json.loads(payload))

        empty_choices_chunks = [c for c in chunks if c.get("choices") == []]
        self.assertEqual(len(empty_choices_chunks), 1)
        self.assertEqual(empty_choices_chunks[0].get("usage"), resp["usage"])
        self.assertEqual(empty_choices_chunks[0]["model"], "step-5")

    def test_buffer_to_sse_reasoning_aliases(self):
        """buffer_to_sse mirrors reasoning under both reasoning_content and reasoning."""
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning": "Let me think...",
                        "content": "Result",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        sse_bytes = buffer_to_sse(resp, model="step-5")
        chunks = [
            json.loads(line[6:].strip())
            for line in sse_bytes.decode("utf-8").split("\n\n")
            if line.startswith("data: ") and line[6:].strip() != "[DONE]"
        ]
        reasoning_chunks = [
            c for c in chunks
            if c.get("choices") and "reasoning_content" in c["choices"][0].get("delta", {})
        ]
        self.assertTrue(len(reasoning_chunks) > 0)
        delta = reasoning_chunks[0]["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "Let me think...")
        self.assertEqual(delta["reasoning"], "Let me think...")

    def test_sanitize_tool_delta(self):
        """sanitize_tool_delta drops empty id/type/name so clients won't overwrite values."""
        # Only index -> should return None
        self.assertIsNone(sanitize_tool_delta({"index": 0}))

        # Valid tool delta
        cleaned = sanitize_tool_delta({
            "index": 1,
            "id": "call_123",
            "type": "function",
            "function": {"name": "test_fn", "arguments": "{\"a\": 1}"},
        })
        self.assertIsNotNone(cleaned)
        self.assertEqual(cleaned["id"], "call_123")
        self.assertEqual(cleaned["function"]["name"], "test_fn")

    def test_strip_reasoning_from_messages(self):
        """strip_reasoning_from_messages cleans reasoning keys from assistant history."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello", "reasoning_content": "plan", "reasoning": "plan"},
            {"role": "user", "content": "Next"},
        ]
        cleaned = strip_reasoning_from_messages(msgs)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[1], {"role": "assistant", "content": "Hello"})
        self.assertNotIn("reasoning_content", cleaned[1])
        self.assertNotIn("reasoning", cleaned[1])

    def test_rewrite_json(self):
        """rewrite_json ensures both reasoning_content and reasoning exist in assistant message."""
        raw = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "42", "reasoning": "think"}}]
        }).encode("utf-8")
        rewritten = json.loads(rewrite_json(raw))
        msg = rewritten["choices"][0]["message"]
        self.assertEqual(msg["reasoning"], "think")
        self.assertEqual(msg["reasoning_content"], "think")

    @patch("step_tool_proxy.proxy.stream_stepfun_chat")
    def test_stream_chat_completions_ensures_stream_true_and_raw_model(self, mock_stream):
        """stream_chat_completions injects stream=True into body and keeps raw model."""
        mock_stream.return_value = iter([b"data: {}\n\n"])
        body = {"model": "step-5"}  # stream omitted
        target = UpstreamTarget("stepfun", "https://api.stepfun.ai/step_plan/v1", "key123")
        list(stream_chat_completions(self.cfg, body, upstream=target))

        self.assertTrue(mock_stream.called)
        called_args = mock_stream.call_args[0]
        passed_body = called_args[2]
        self.assertTrue(passed_body.get("stream"))
        self.assertEqual(passed_body.get("model"), "step-5")

    def test_responses_api_uses_standard_reasoning_events(self):
        """ChatToResponsesStream must produce OpenAI standard response.reasoning_summary_* events.
        Non-standard variants like response.reasoning_part.added crash Rust Serde in Codex CLI.
        """
        mapper = ChatToResponsesStream("step-5")
        event = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "Analyzing user query...",
                    },
                }
            ]
        }
        chunks = list(mapper.feed(event))
        event_types = []
        for chunk in chunks:
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("event: "):
                    event_types.append(line[7:].strip())

        # Assert no illegal response.reasoning_part.* events
        for et in event_types:
            self.assertFalse(et.startswith("response.reasoning_part"), f"Forbidden event type found: {et}")

        # Assert standard response.reasoning_summary_* events are produced
        self.assertIn("response.reasoning_summary_part.added", event_types)
        self.assertIn("response.reasoning_summary_text.delta", event_types)


if __name__ == "__main__":
    unittest.main()
