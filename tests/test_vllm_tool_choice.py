import unittest

from clinseekagent.vllm_backend import VLLMOpenAIAsyncGenerator


class ToolChoiceSanitizationTests(unittest.TestCase):
    def test_public_clinseek_35b_uses_native_api_tools(self):
        self.assertFalse(
            VLLMOpenAIAsyncGenerator._is_clinseek_sft_model(
                "UCSC-VLAA/ClinSeek-35B-A3B"
            )
        )

    def test_older_clinseek_checkpoint_keeps_legacy_format(self):
        self.assertTrue(
            VLLMOpenAIAsyncGenerator._is_clinseek_sft_model("ClinSeek-7B-SFT")
        )

    def test_specific_tool_choice_is_openai_structured_and_api_safe(self):
        self.assertEqual(
            VLLMOpenAIAsyncGenerator._sanitize_tool_choice_for_api(
                "ehr.finish"
            ),
            {
                "type": "function",
                "function": {"name": "ehr_finish"},
            },
        )

    def test_any_tool_choice_maps_to_openai_required(self):
        self.assertEqual(
            VLLMOpenAIAsyncGenerator._sanitize_tool_choice_for_api("any"),
            "required",
        )

    def test_structured_tool_choice_name_is_sanitized_without_mutation(self):
        original = {
            "type": "function",
            "function": {"name": "browser.search"},
        }
        sanitized = VLLMOpenAIAsyncGenerator._sanitize_tool_choice_for_api(
            original
        )
        self.assertEqual(
            sanitized["function"]["name"],
            "browser_search",
        )
        self.assertEqual(
            original["function"]["name"],
            "browser.search",
        )


if __name__ == "__main__":
    unittest.main()
