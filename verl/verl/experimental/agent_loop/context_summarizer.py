"""Context summarizer using Claude Haiku via AWS Bedrock.

When the RL agent's conversation history exceeds a token threshold, instead of
discarding all context (the default context_reset behavior), this module calls
Haiku to produce a concise research summary. The summary is then used as the
reset message, preserving key findings while freeing token budget.

Usage in tool_agent_loop.py:
    summarizer = ContextSummarizer.from_config(multi_turn_config)
    summary = await summarizer.summarize(messages)
"""

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

# Shared thread pool for blocking boto3 calls inside async code
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

SUMMARIZE_SYSTEM_PROMPT = (
    "You are a research assistant summarizer. Given a multi-turn research conversation "
    "between an AI agent and its tools, produce a concise summary of KEY FINDINGS ONLY.\n\n"
    "Rules:\n"
    "- List facts, entities, dates, and numbers the agent discovered.\n"
    "- Note which search queries were tried and what they returned.\n"
    "- Note any dead ends or contradictions found.\n"
    "- Do NOT include reasoning chains, thinking, or tool call syntax.\n"
    "- Keep the summary under 500 words.\n"
    "- Use bullet points for clarity."
)


class ContextSummarizer:
    """Summarizes multi-turn conversations using Claude Haiku via Bedrock."""

    def __init__(
        self,
        model_id: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name: str = "us-east-1",
        max_summary_tokens: int = 1024,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
    ):
        self.model_id = model_id
        self.region_name = region_name
        self.max_summary_tokens = max_summary_tokens

        import boto3

        client_kwargs = {"service_name": "bedrock-runtime", "region_name": region_name}
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        self._client = boto3.client(**client_kwargs)
        logger.info(
            f"ContextSummarizer initialized: model={model_id}, region={region_name}"
        )

    @classmethod
    def from_config(cls, multi_turn_config) -> Optional["ContextSummarizer"]:
        """Create a ContextSummarizer from verl multi_turn config, or None if disabled."""
        enabled = getattr(multi_turn_config, "context_reset_summarizer_enabled", False)
        if not enabled:
            return None

        model_id = getattr(
            multi_turn_config,
            "context_reset_summarizer_model",
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        region = getattr(
            multi_turn_config, "context_reset_summarizer_region", "us-east-1"
        )
        max_tokens = getattr(
            multi_turn_config, "context_reset_summarizer_max_tokens", 1024
        )

        # Credentials: check config, then env, then base64-encoded env
        aws_key = getattr(multi_turn_config, "context_reset_summarizer_aws_key", None)
        aws_secret = getattr(
            multi_turn_config, "context_reset_summarizer_aws_secret", None
        )

        if not aws_key:
            aws_key = os.environ.get("CONTEXT_SUMMARIZER_AWS_ACCESS_KEY_ID")
        if not aws_secret:
            aws_secret = os.environ.get("CONTEXT_SUMMARIZER_AWS_SECRET_ACCESS_KEY")

        # Support base64-encoded combined key: "base64(key_id:secret_key)"
        b64_key = os.environ.get("CONTEXT_SUMMARIZER_BEDROCK_KEY")
        if b64_key and not (aws_key and aws_secret):
            try:
                decoded = base64.b64decode(b64_key).decode("utf-8")
                if ":" in decoded:
                    aws_key, aws_secret = decoded.split(":", 1)
            except Exception as e:
                logger.warning(f"Failed to decode CONTEXT_SUMMARIZER_BEDROCK_KEY: {e}")

        return cls(
            model_id=model_id,
            region_name=region,
            max_summary_tokens=max_tokens,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )

    def _format_conversation_for_summary(self, messages: list[dict]) -> str:
        """Convert conversation messages to a readable text block for summarization."""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content or role == "system":
                continue

            # Truncate very long tool responses to keep the summarization input manageable
            if role == "tool" or (role == "user" and "<tool_response>" in content):
                if len(content) > 2000:
                    content = content[:1000] + "\n...(truncated)...\n" + content[-1000:]

            # Strip thinking blocks for cleaner summary input
            if "<think>" in content:
                import re

                content = re.sub(
                    r"<think>.*?</think>", "[thinking omitted]", content, flags=re.DOTALL
                )

            parts.append(f"[{role}]: {content}")

        return "\n\n".join(parts)

    def _call_bedrock_sync(self, conversation_text: str) -> str:
        """Synchronous Bedrock API call (run in executor for async)."""
        user_prompt = (
            f"Summarize the key findings from this research conversation:\n\n"
            f"---\n{conversation_text}\n---\n\n"
            f"Provide a concise bullet-point summary of what was discovered."
        )

        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_summary_tokens,
            "temperature": 0.0,
            "system": SUMMARIZE_SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": user_prompt}]}
            ],
        }

        response = self._client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(request_body),
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    async def summarize(self, messages: list[dict]) -> Optional[str]:
        """Summarize conversation messages asynchronously.

        Returns the summary string, or None if summarization fails.
        """
        try:
            conversation_text = self._format_conversation_for_summary(messages)
            if not conversation_text.strip():
                return None

            # Limit input to ~12k chars to keep Haiku fast and cheap
            if len(conversation_text) > 12000:
                conversation_text = (
                    conversation_text[:6000]
                    + "\n\n...(middle truncated)...\n\n"
                    + conversation_text[-6000:]
                )

            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                _EXECUTOR, self._call_bedrock_sync, conversation_text
            )
            logger.info(
                f"Context summarization complete: {len(summary)} chars "
                f"from {len(messages)} messages"
            )
            return summary

        except Exception as e:
            logger.warning(f"Context summarization failed, falling back to static reset: {e}")
            return None
