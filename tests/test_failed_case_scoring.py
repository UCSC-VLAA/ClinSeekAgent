from __future__ import annotations

import json
import unittest

from clinseekagent.run_text import (
    run_one_native,
    summarize_conversation_completion,
    tool_call_batch_signature,
)
from clinseekagent.scoring.score_text import (
    extract_finish_predictions_with_source,
    f1_score,
)


class FailureScoringTests(unittest.TestCase):
    def test_tool_call_signature_ignores_ids_and_json_key_order(self):
        first = [{
            "id": "call-1",
            "function": {
                "name": "ehr_search",
                "arguments": '{"patient_id": 7, "table": "labs"}',
            },
        }]
        second = [{
            "id": "call-99",
            "function": {
                "name": "ehr.search",
                "arguments": '{"table":"labs","patient_id":7}',
            },
        }]
        self.assertEqual(
            tool_call_batch_signature(first),
            tool_call_batch_signature(second),
        )

    def test_tool_call_signature_preserves_batch_order(self):
        calls = [
            {"function": {"name": "ehr.a", "arguments": "{}"}},
            {"function": {"name": "ehr.b", "arguments": "{}"}},
        ]
        self.assertNotEqual(
            tool_call_batch_signature(calls),
            tool_call_batch_signature(list(reversed(calls))),
        )

    def test_plain_text_without_finish_is_incomplete(self):
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "the answer is yes"},
        ]
        self.assertEqual(
            (False, "no_final_answer"),
            summarize_conversation_completion(messages),
        )

    def test_native_finish_is_complete(self):
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "finish-1",
                "type": "function",
                "function": {
                    "name": "ehr.finish",
                    "arguments": json.dumps({"response": ["yes"]}),
                },
            }],
        }]
        self.assertEqual(
            (True, "finish_tool_call"),
            summarize_conversation_completion(messages),
        )

    def test_failed_result_has_empty_prediction_and_zero_score(self):
        failed = {
            "qid": "q1",
            "status": "error",
            "completed": False,
            "messages": [],
            "error": "429 rate limit",
        }
        predictions, source = extract_finish_predictions_with_source(failed)
        self.assertEqual([], predictions)
        self.assertEqual("no_prediction", source)
        self.assertEqual(
            {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0},
            f1_score(predictions, ["yes"]),
        )


class IdenticalToolCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_on_configured_identical_round(self):
        class Generator:
            calls = 0

            async def chat_completion(self, **kwargs):
                self.calls += 1
                return {
                    "choices": [{
                        "message": {
                            "content": "",
                            "tool_calls": [{
                                "id": f"call-{self.calls}",
                                "function": {
                                    "name": "ehr.get_table_names",
                                    "arguments": "{}",
                                },
                            }],
                        },
                    }],
                }

        class BrowserPool:
            enabled = False

            def init_session(self, qid):
                return None

            def cleanup(self, qid):
                return None

        class EHRPool:
            async def init_session(self, qid):
                return None

            async def call_tool(self, qid, name, arguments):
                return "[]"

            async def cleanup(self, qid):
                return None

        generator = Generator()
        messages, stop_reason = await run_one_native(
            question="test",
            qid="qid",
            generator=generator,
            browser_pool=BrowserPool(),
            ehr_pool=EHRPool(),
            max_rounds=200,
            max_consecutive_identical_tool_rounds=20,
        )
        self.assertEqual(20, generator.calls)
        self.assertEqual("consecutive_identical_tool_rounds", stop_reason)
        self.assertEqual(20, sum(m["role"] == "assistant" for m in messages))
        self.assertEqual(20, sum(m["role"] == "tool" for m in messages))


if __name__ == "__main__":
    unittest.main()
