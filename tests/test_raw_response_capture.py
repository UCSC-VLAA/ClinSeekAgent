from __future__ import annotations

import unittest
from types import SimpleNamespace

from clinseekagent.vllm_backend import VLLMOpenAIAsyncGenerator


class FakeResponse:
    """Enough of an OpenAI response object for the converter.

    `thinking_blocks` sits only in the raw payload, never on the typed
    message — which is exactly what the capture switch is for.
    """

    def __init__(self):
        self.choices = [SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="done", tool_calls=None),
        )]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=3,
                                     total_tokens=13, prompt_tokens_details=None)

    def model_dump(self, mode=None):
        return {"choices": [{"message": {
            "content": "done",
            "thinking_blocks": [{"type": "thinking", "thinking": "why not",
                                 "signature": "sig"}],
        }}]}


class RawResponseCaptureTests(unittest.TestCase):
    def test_raw_payload_is_dropped_unless_asked_for(self):
        converted = VLLMOpenAIAsyncGenerator._convert_response_to_openai(
            FakeResponse(), model_name="claude-opus-4-6")
        self.assertNotIn("raw_response", converted["choices"][0]["message"])

    def test_raw_payload_carries_the_thinking_blocks(self):
        converted = VLLMOpenAIAsyncGenerator._convert_response_to_openai(
            FakeResponse(), model_name="claude-opus-4-6",
            capture_raw=True)
        raw = converted["choices"][0]["message"]["raw_response"]
        self.assertEqual(
            "why not",
            raw["choices"][0]["message"]["thinking_blocks"][0]["thinking"],
        )


if __name__ == "__main__":
    unittest.main()
