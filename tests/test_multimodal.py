"""Unit and regression tests for multimodal (vision, video, audio, document) support.

Covers:
1. Responses API request -> chat.completions request with images (URLs, data URIs, base64).
2. Responses API request with video and audio.
3. Top-level multimodal input items in Responses API input array.
4. Image-only messages preservation (not dropped).
5. Responses API response serialization for list and string content.
6. Chat completions -> Anthropic Messages API multimodal conversion (image, pdf document, tool_result with image).
7. Media type normalization (image/jpg -> image/jpeg).
8. StepFun messages normalization (input_image -> image_url, Anthropic source -> image_url, video_url, input_audio).
9. Full pipeline roundtrip tests with mock upstreams.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from step_tool_proxy.anthropic_api import (
    _anthropic_blocks_from_content,
    _normalize_media_type,
    chat_request_to_anthropic,
)
from step_tool_proxy.config import Config
from step_tool_proxy.proxy import (
    UpstreamTarget,
    _call_anthropic,
    _call_stepfun,
    handle_models,
    normalize_content_for_stepfun,
    normalize_messages_for_stepfun,
)
from step_tool_proxy.responses_api import (
    chat_response_to_responses,
    responses_request_to_chat,
)


class TestMultimodalSupport(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_normalize_media_type(self):
        self.assertEqual(_normalize_media_type("image/jpg"), "image/jpeg")
        self.assertEqual(_normalize_media_type("image/jpeg"), "image/jpeg")
        self.assertEqual(_normalize_media_type("image/png; charset=utf-8"), "image/png")
        self.assertEqual(_normalize_media_type("image/webp"), "image/webp")
        self.assertEqual(_normalize_media_type("application/pdf"), "application/pdf")
        self.assertEqual(_normalize_media_type(""), "image/png")

    def test_responses_request_to_chat_preserves_vision(self):
        """Responses API input with input_image must be converted to chat.completions image_url."""
        body = {
            "model": "step-5-preview",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is in this picture?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/photo.jpg",
                            "detail": "high",
                        },
                    ],
                }
            ],
        }
        chat = responses_request_to_chat(body)
        self.assertEqual(len(chat["messages"]), 1)
        user_msg = chat["messages"][0]
        self.assertEqual(user_msg["role"], "user")
        self.assertIsInstance(user_msg["content"], list)
        self.assertEqual(len(user_msg["content"]), 2)
        self.assertEqual(user_msg["content"][0], {"type": "text", "text": "What is in this picture?"})
        self.assertEqual(
            user_msg["content"][1],
            {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg", "detail": "high"}},
        )

    def test_responses_request_to_chat_image_only_not_dropped(self):
        """An image-only message in Responses API input must not be dropped."""
        body = {
            "model": "step-1.5v-mini",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": {
                                "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
                            },
                        }
                    ],
                }
            ],
        }
        chat = responses_request_to_chat(body)
        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["role"], "user")
        self.assertIsInstance(chat["messages"][0]["content"], list)
        self.assertEqual(chat["messages"][0]["content"][0]["type"], "image_url")
        self.assertTrue(chat["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_responses_request_to_chat_top_level_items(self):
        """Top-level multimodal items in input array are collected into a user message."""
        body = {
            "model": "step-5-preview",
            "input": [
                {"type": "input_text", "text": "Look at this:"},
                {"type": "input_image", "image_url": "https://example.com/cat.png"},
                {"type": "input_video", "video_url": "https://example.com/cat.mp4"},
            ],
        }
        chat = responses_request_to_chat(body)
        self.assertEqual(len(chat["messages"]), 1)
        content = chat["messages"][0]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "https://example.com/cat.png")
        self.assertEqual(content[2]["type"], "video_url")
        self.assertEqual(content[2]["video_url"]["url"], "https://example.com/cat.mp4")

    def test_responses_request_to_chat_video_and_audio(self):
        """Responses API requests with video and audio are correctly mapped."""
        body = {
            "model": "stepaudio-2.5-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Transcribe this audio:"},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "dGVzdGF1ZGlv", "format": "wav"},
                        },
                    ],
                }
            ],
        }
        chat = responses_request_to_chat(body)
        self.assertEqual(len(chat["messages"]), 1)
        content = chat["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "input_audio")
        self.assertEqual(content[1]["input_audio"]["format"], "wav")

    def test_chat_response_to_responses_with_list_content(self):
        """Responses API output builder handles message content that is a list of parts."""
        chat_resp = {
            "id": "chatcmpl-test",
            "created": 1000000000,
            "model": "step-5-preview",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I see a cute orange cat in the photo."}
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        resp = chat_response_to_responses(chat_resp)
        self.assertEqual(len(resp["output"]), 1)
        msg_item = resp["output"][0]
        self.assertEqual(msg_item["type"], "message")
        self.assertEqual(msg_item["content"][0]["type"], "output_text")
        self.assertEqual(msg_item["content"][0]["text"], "I see a cute orange cat in the photo.")

    def test_anthropic_blocks_from_content_image_url_and_data_uri(self):
        """Conversion of OpenAI image_url to Anthropic content blocks."""
        content = [
            {"type": "text", "text": "Analyze image"},
            {"type": "image_url", "image_url": "https://example.com/test.jpg"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpg;base64,/9j/4AAQSkZJRg==",
                },
            },
        ]
        blocks = _anthropic_blocks_from_content(content)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0], {"type": "text", "text": "Analyze image"})
        self.assertEqual(blocks[1], {"type": "image", "source": {"type": "url", "url": "https://example.com/test.jpg"}})
        # image/jpg normalized to image/jpeg
        self.assertEqual(
            blocks[2],
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/4AAQSkZJRg=="}},
        )

    def test_anthropic_blocks_from_content_pdf_document(self):
        """PDF data URIs are mapped to Anthropic document blocks instead of image blocks."""
        content = [
            {
                "type": "image_url",
                "image_url": "data:application/pdf;base64,JVBERi0xLjUK...",
            }
        ]
        blocks = _anthropic_blocks_from_content(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "document")
        self.assertEqual(blocks[0]["source"]["media_type"], "application/pdf")

    def test_anthropic_blocks_from_content_video_and_audio(self):
        """Video and audio parts are converted to descriptive text blocks for Anthropic."""
        content = [
            {"type": "video_url", "video_url": {"url": "https://example.com/sample.mp4"}},
            {"type": "input_audio", "input_audio": {"data": "abc", "format": "mp3"}},
        ]
        blocks = _anthropic_blocks_from_content(content)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], {"type": "text", "text": "[Video: https://example.com/sample.mp4]"})
        self.assertEqual(blocks[1], {"type": "text", "text": "[Audio input]"})

    def test_chat_request_to_anthropic_tool_result_multimodal(self):
        """Tool result messages carrying multimodal parts preserve images in Anthropic tool_result blocks."""
        body = {
            "model": "claude-3-7-sonnet-20250219",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_abc123",
                    "content": [
                        {"type": "text", "text": "Screenshot of page:"},
                        {
                            "type": "image_url",
                            "image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==",
                        },
                    ],
                }
            ],
        }
        anth = chat_request_to_anthropic(body)
        user_msg = anth["messages"][0]
        # Should be a tool_result block with content as blocks
        self.assertEqual(user_msg["role"], "user")
        block = user_msg["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "call_abc123")
        self.assertIsInstance(block["content"], list)
        self.assertEqual(len(block["content"]), 2)
        self.assertEqual(block["content"][1]["type"], "image")

    def test_normalize_messages_for_stepfun(self):
        """normalize_messages_for_stepfun transforms various input formats to StepFun standards."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Hello"},
                    {"type": "input_image", "image_url": "https://example.com/img.jpg", "detail": "high"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                    {"type": "input_video", "video_url": "https://example.com/video.mp4"},
                    {"type": "input_audio", "audio": {"data": "dGVzdA==", "format": "wav"}},
                ],
            }
        ]
        normalized = normalize_messages_for_stepfun(msgs)
        self.assertEqual(len(normalized), 1)
        parts = normalized[0]["content"]
        self.assertEqual(len(parts), 5)
        # text
        self.assertEqual(parts[0], {"type": "text", "text": "Hello"})
        # input_image -> image_url with detail
        self.assertEqual(
            parts[1],
            {"type": "image_url", "image_url": {"url": "https://example.com/img.jpg", "detail": "high"}},
        )
        # Anthropic base64 source -> image_url data URI
        self.assertEqual(
            parts[2],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        )
        # video_url
        self.assertEqual(parts[3], {"type": "video_url", "video_url": {"url": "https://example.com/video.mp4"}})
        # input_audio
        self.assertEqual(parts[4]["type"], "input_audio")

    @patch("step_tool_proxy.proxy.UpstreamClient.post")
    def test_stepfun_pipeline_sends_normalized_multimodal(self, mock_post):
        """When calling StepFun, messages are normalized and sent upstream."""
        mock_post.return_value = (
            200,
            json.dumps({
                "id": "chatcmpl-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "This is a cat."},
                        "finish_reason": "stop",
                    }
                ],
            }).encode("utf-8"),
            "application/json",
        )
        body = {
            "model": "step-5-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Identify this animal"},
                        {"type": "input_image", "image_url": "https://example.com/cat.jpg"},
                    ],
                }
            ],
        }
        target = UpstreamTarget("stepfun", "https://api.stepfun.ai/v1", "test-key")
        status, data, ct = _call_stepfun(self.cfg, target, body, json.dumps(body).encode(), lambda m: None)

        self.assertEqual(status, 200)
        self.assertTrue(mock_post.called)
        posted_body = json.loads(mock_post.call_args[0][1])
        user_parts = posted_body["messages"][0]["content"]
        self.assertEqual(user_parts[0]["type"], "text")
        self.assertEqual(user_parts[1]["type"], "image_url")
        self.assertEqual(user_parts[1]["image_url"]["url"], "https://example.com/cat.jpg")

    def test_fallback_models_includes_multimodal(self):
        """handle_models fallback models should include StepFun multimodal models."""
        target = UpstreamTarget("anthropic", "https://unreachable.invalid", "key")
        with patch("step_tool_proxy.proxy.AnthropicUpstreamClient.get", return_value=(500, b"", "")):
            status, data, ct = handle_models(self.cfg, upstream=target)
            self.assertEqual(status, 200)
            models = json.loads(data)["data"]
            model_ids = [m["id"] for m in models]
            self.assertIn("step-5-preview", model_ids)
            self.assertIn("step-3.7-flash", model_ids)
            self.assertIn("step-1v-8k", model_ids)
            self.assertIn("step-1.5v-mini", model_ids)
            self.assertIn("stepaudio-2.5-chat", model_ids)


if __name__ == "__main__":
    unittest.main()
