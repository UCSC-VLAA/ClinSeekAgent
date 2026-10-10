"""Request shaping of the released backends (paper inference settings).

No network: requests are captured before they are sent.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path

from bedrock_backend import (
    BedrockAsyncGenerator,
    rejects_sampling_params,
    requires_adaptive_thinking,
)
from curated_invokers import OpenAICompatInvoker, claude_request_fields
from vllm_backend import VLLMOpenAIAsyncGenerator

REPO_ROOT = Path(__file__).resolve().parents[1]


def _capture_bedrock_request(model_id, *, thinking, temperature=0.0,
                             tool_choice="auto", effort="high"):
    gen = BedrockAsyncGenerator(
        model_id=model_id, region_name="us-east-1", enable_thinking=thinking,
        thinking_budget_tokens=4096, reasoning_effort=effort)
    captured = {}

    async def fake_invoke(request):
        captured.update(request)
        return {"content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "usage": {}}

    gen._invoke_model_with_retry = fake_invoke
    tools = [{"type": "function", "function": {
        "name": "ehr.finish", "description": "", "parameters": {"type": "object"}}}]
    asyncio.run(gen.chat_completion(
        messages=[{"role": "user", "content": "hi"}], tools=tools,
        tool_choice=tool_choice, temperature=temperature, max_tokens=8192))
    gen.shutdown()
    return captured


class ClaudeThinkingRules(unittest.TestCase):
    def test_model_rules(self):
        self.assertFalse(requires_adaptive_thinking("us.anthropic.claude-opus-4-6-v1"))
        self.assertTrue(requires_adaptive_thinking("claude-opus-5"))
        self.assertTrue(requires_adaptive_thinking("anthropic.claude-sonnet-5"))
        self.assertTrue(rejects_sampling_params("claude-opus-4-7"))
        self.assertFalse(rejects_sampling_params("claude-sonnet-4-6"))

    def test_opus_46_uses_fixed_budget_and_drops_temperature(self):
        req = _capture_bedrock_request("us.anthropic.claude-opus-4-6-v1", thinking=True)
        self.assertEqual(req["thinking"], {"type": "enabled", "budget_tokens": 4096})
        self.assertNotIn("temperature", req)
        self.assertNotIn("output_config", req)
        self.assertEqual(req["max_tokens"], 8192)

    def test_opus_5_uses_adaptive_thinking_with_effort(self):
        req = _capture_bedrock_request("anthropic.claude-opus-5", thinking=True)
        self.assertEqual(req["thinking"], {"type": "adaptive"})
        self.assertEqual(req["output_config"], {"effort": "high"})
        self.assertNotIn("temperature", req)

    def test_forced_tool_choice_becomes_auto_with_thinking(self):
        req = _capture_bedrock_request(
            "us.anthropic.claude-opus-4-6-v1", thinking=True, tool_choice="ehr.finish")
        self.assertEqual(req["tool_choice"], {"type": "auto"})
        req = _capture_bedrock_request(
            "us.anthropic.claude-opus-4-6-v1", thinking=False, tool_choice="ehr.finish")
        self.assertEqual(req["tool_choice"], {"type": "tool", "name": "ehr_finish"})
        self.assertEqual(req["temperature"], 0.0)

    def test_curated_claude_fields_match(self):
        fields = claude_request_fields("claude-opus-4-6", 8192, 0.0, True, 4096, "high")
        self.assertEqual(fields["thinking"]["budget_tokens"], 4096)
        self.assertNotIn("temperature", fields)
        fields = claude_request_fields("claude-opus-5", 8192, 0.0, True, 4096, "high")
        self.assertEqual(fields["output_config"], {"effort": "high"})
        fields = claude_request_fields("claude-opus-4-6", 8192, 0.0, False, 4096, "high")
        self.assertEqual(fields["temperature"], 0.0)


class OpenAICompatibleShaping(unittest.TestCase):
    def test_vllm_generator_openai_reasoning_model(self):
        gen = VLLMOpenAIAsyncGenerator(model_name="gpt-5.6-sol", enable_thinking=True,
                                       reasoning_effort="high")
        self.assertEqual(gen._max_token_kwargs("gpt-5.6-sol", 8192),
                         {"max_completion_tokens": 8192})
        self.assertEqual(gen._reasoning_kwargs("gpt-5.6-sol"), {"reasoning_effort": "high"})
        self.assertEqual(gen._sampling_kwargs("gpt-5.6-sol", 0.0), {})
        self.assertIsNone(gen._build_extra_body())
        gen.shutdown()

    def test_vllm_generator_local_model(self):
        gen = VLLMOpenAIAsyncGenerator(model_name="ClinSeek-35B-A3B", enable_thinking=True,
                                       reasoning_effort="high")
        self.assertEqual(gen._max_token_kwargs("ClinSeek-35B-A3B", 8192), {"max_tokens": 8192})
        self.assertEqual(gen._reasoning_kwargs("ClinSeek-35B-A3B"), {})
        self.assertEqual(gen._sampling_kwargs("ClinSeek-35B-A3B", 0.0), {"temperature": 0.0})
        self.assertEqual(gen._build_extra_body(),
                         {"chat_template_kwargs": {"enable_thinking": True}})
        gen.shutdown()

    def test_curated_openai_invoker_matches_agentic_shaping(self):
        inv = OpenAICompatInvoker(base_url="http://127.0.0.1:1", api_key=None,
                                  max_tokens=8192, temperature=0.0, max_retries=0)
        self.assertEqual(inv._request_kwargs("ClinSeek-35B-A3B"), {
            "max_tokens": 8192, "temperature": 0.0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}})
        self.assertEqual(inv._request_kwargs("gpt-5.6-sol"), {
            "max_completion_tokens": 8192, "reasoning_effort": "high"})


class CuratedRunnersFailClosed(unittest.TestCase):
    def test_text_row_without_question_is_rejected(self):
        import run_text_curated

        class Never:
            def invoke(self, *a, **k):  # pragma: no cover - must not be reached
                raise AssertionError("invoked without a prompt")

        with self.assertRaises(ValueError):
            run_text_curated.process_row({"qid": "q1"}, "local", "m", Never(), 1)

    def test_record_context_requires_prompt_template(self):
        import record_context

        with self.assertRaises(ValueError):
            record_context.render_question({"qid": "q1"}, "/nonexistent", "recent")
        with self.assertRaises(ValueError):
            record_context.render_input_text({"qid": "q1", "question": "x"},
                                             "/nonexistent", "recent")


class RunnerCLI(unittest.TestCase):
    def test_backends_and_browser_flags(self):
        for runner in ("run_text.py", "run_multimodal.py"):
            out = subprocess.run(
                [sys.executable, str(REPO_ROOT / "clinseekagent" / runner), "--help"],
                capture_output=True, text=True, cwd=REPO_ROOT,
                env={"PYTHONPATH": f"{REPO_ROOT / 'clinseekagent'}:{REPO_ROOT / 'src'}",
                     "PATH": "/usr/bin:/bin"})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("{bedrock,vertex,vllm}", out.stdout)
            self.assertIn("--enable_browser", out.stdout)
            self.assertNotIn("--api_waf", out.stdout)


if __name__ == "__main__":
    unittest.main()
