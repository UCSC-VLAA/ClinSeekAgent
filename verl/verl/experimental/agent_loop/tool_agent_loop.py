# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import json
import logging
import os
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import torch
from PIL import Image

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase,
    AgentLoopOutput,
    register,
)
from verl.experimental.agent_loop.context_summarizer import ContextSummarizer
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.experimental.agent_loop.utils import build_gpt_oss_tool_response_text
from verl.interactions.base import BaseInteraction
from verl.interactions.utils.interaction_registry import initialize_interactions_from_config
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class AgentState(Enum):
    PENDING = "pending"
    GENERATING = "generating"
    PROCESSING_TOOLS = "processing_tools"
    TERMINATED = "terminated"
    INTERACTING = "interacting"


class AgentData:
    """Encapsulates all state variables for the agent loop. AgentData is passed to tool calling in case that
    tool may need to access full history state. User can store any tool session data in `extra_fields`."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data: list[Image.Image],
        video_data: list[tuple[torch.Tensor, dict[str, Any]]],
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        interaction: Optional[BaseInteraction] = None,
        interaction_kwargs: Optional[dict[str, Any]] = None,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.tools_kwargs = tools_kwargs
        self.interaction = interaction
        self.interaction_kwargs = interaction_kwargs or {}

        # State variables
        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []
        self.turn_scores: list[float] = []
        self.tool_rewards: list[float] = []
        self.user_turns = 0
        self.assistant_turns = 0

        # Temporary state for tool calls
        self.tool_calls: list[FunctionCall] = []

        self.routed_experts = None

        # Context-reset state (ported from OpenResearcher researcher_v2). The vLLM
        # generation prompt is derived from _get_generation_prompt(); prompt_ids
        # always holds the full trajectory for training/reward use.
        self._context_was_reset: bool = False
        self._reset_generation_prefix: list[int] = []
        self._post_reset_offset: int = 0
        self._num_resets: int = 0

        # Extra fields for dynamic addition, e.g., tool session data
        self.extra_fields: dict[str, Any] = {}


@register("tool_agent")
class ToolAgentLoop(AgentLoopBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize tools from config file
        self.max_user_turns = self.rollout_config.multi_turn.max_user_turns
        self.max_assistant_turns = self.rollout_config.multi_turn.max_assistant_turns
        self.max_parallel_calls = self.rollout_config.multi_turn.max_parallel_calls
        self.max_tool_response_length = self.rollout_config.multi_turn.max_tool_response_length
        self.tool_response_truncate_side = self.rollout_config.multi_turn.tool_response_truncate_side
        tool_config_path = self.rollout_config.multi_turn.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {tool.name: tool for tool in tool_list}
        self.tool_schemas = [tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for tool in tool_list]
        self.tool_parser = ToolParser.get_tool_parser(self.rollout_config.multi_turn.format, self.tokenizer)
        self.tool_parser_name = self.rollout_config.multi_turn.format

        self.prompt_length = self.rollout_config.prompt_length
        self.response_length = self.rollout_config.response_length

        # Context-reset config (ported from OpenResearcher researcher_v2)
        mt = self.rollout_config.multi_turn
        self.context_reset_enabled = mt.context_reset_enabled
        self.context_reset_threshold = mt.context_reset_threshold
        self.context_reset_message = mt.context_reset_message
        # Optional: Claude Haiku summarizer that condenses the full history
        # into bullet points of findings before a reset fires. None when the
        # summarizer is disabled or boto3 is unavailable — _maybe_reset_context
        # falls back to the static reset message in that case.
        try:
            self.context_summarizer = ContextSummarizer.from_config(mt)
        except Exception as e:  # noqa: BLE001
            logger.warning("ContextSummarizer.from_config failed (%s); continuing without it", e)
            self.context_summarizer = None

        # Force-finish-tool variant (SFT-ed models that submit via ehr.finish
        # tool call). When enabled, _maybe_force_answer injects a Qwen3-XML
        # tool-call prefix instead of the text <answer> prefix. Defaults to
        # False so the base/non-SFT <answer>-prefix path remains the default.
        self.force_finish_tool_enabled = getattr(mt, "force_finish_tool_enabled", False)
        self.force_finish_tool_name = getattr(mt, "force_finish_tool_name", "ehr.finish")
        # SFT-ed Qwen3.5 models were trained to emit `response=...` as the
        # finish-tool argument; default the forced path to match.
        self.force_finish_tool_param = getattr(mt, "force_finish_tool_param", "response")

        # Context reset body selection:
        #   "summarizer"    → Claude-Haiku summary note (or static fallback) replaces
        #                    the middle rounds; keeps rollouts submitting (v5h default).
        #   "sliding_window" → keep system + user + last N×2 messages verbatim (v5m).
        # Shared bounds: `context_reset_max_count` caps total resets per rollout.
        self.context_reset_mode = getattr(mt, "context_reset_mode", "summarizer")
        self.context_reset_keep_last_rounds = getattr(mt, "context_reset_keep_last_rounds", 4)
        # Kept for backward-compat, no longer enforced inside _do_context_reset.
        # Resets fire as many times as the trajectory needs — only the hard
        # response_length cap + force-answer soft cap bound things.
        self.context_reset_max_count = getattr(mt, "context_reset_max_count", 10)
        # Soft cap: when response_mask crosses this length, the next turn-limit
        # event force-injects a finish-tool prefix so the model spends its
        # remaining budget on the answer rather than further exploration.
        # 0 disables (fall back to response_length-exhaustion trigger only).
        self.force_answer_token_threshold = int(getattr(mt, "force_answer_token_threshold", 0) or 0)

        # Initialize interactions from config file
        self.interaction_config_file = self.rollout_config.multi_turn.interaction_config_path
        if self.interaction_config_file:
            self.interaction_map: dict[str, BaseInteraction] = self._initialize_interactions(
                self.interaction_config_file
            )

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])

        # extract images and videos from messages
        multi_modal_data = await self.process_vision_info(messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")

        metrics = {}
        request_id = uuid4().hex
        tools_kwargs = kwargs.get("tools_kwargs", {})

        # Initialize interaction if needed
        interaction = None
        interaction_kwargs = {}
        if self.interaction_config_file:
            interaction_kwargs = kwargs["extra_info"]["interaction_kwargs"]
            if "name" not in interaction_kwargs:
                raise ValueError("'name' key is required in interaction_kwargs")
            interaction_name = interaction_kwargs["name"]
            if interaction_name not in self.interaction_map:
                raise ValueError(
                    f"Interaction '{interaction_name}' not found in interaction_map. Available interactions: "
                    f"{list(self.interaction_map.keys())}"
                )
            interaction = self.interaction_map[interaction_name]
            await interaction.start_interaction(request_id, **interaction_kwargs)
        # Create AgentData instance to encapsulate all state
        agent_data = AgentData(
            messages=messages,
            image_data=images,
            video_data=videos,
            metrics=metrics,
            request_id=request_id,
            tools_kwargs=tools_kwargs,
            interaction=interaction,
            interaction_kwargs=interaction_kwargs,
        )

        # State machine loop
        state = AgentState.PENDING
        while state != AgentState.TERMINATED:
            if state == AgentState.PENDING:
                state = await self._handle_pending_state(agent_data, sampling_params)
            elif state == AgentState.GENERATING:
                state = await self._handle_generating_state(agent_data, sampling_params)
            elif state == AgentState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools_state(agent_data)
            elif state == AgentState.INTERACTING:
                state = await self._handle_interacting_state(agent_data)
            else:
                logger.error(f"Invalid state: {state}")
                state = AgentState.TERMINATED

        # Finalize output
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask) :]
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]
        multi_modal_data = {}
        if agent_data.image_data is not None:
            multi_modal_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            multi_modal_data["videos"] = agent_data.video_data

        output: AgentLoopOutput = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            multi_modal_data=multi_modal_data,
            response_logprobs=agent_data.response_logprobs[: self.response_length]
            if agent_data.response_logprobs
            else None,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=agent_data.routed_experts,
            extra_fields=agent_data.extra_fields,
        )
        # Seed defaults for fields that may-or-may-not be populated per
        # rollout. DataProto.concat asserts all samples share the same keys,
        # so we need every trajectory to carry these even when the tool that
        # writes them (EHRFinishTool) was never called. Note: pydantic's
        # AgentLoopOutput copies the dict on construction, so we mutate
        # `output.extra_fields` (not `agent_data.extra_fields`).
        output.extra_fields.setdefault("final_answer", None)
        output.extra_fields.setdefault("final_answer_submitted", False)
        output.extra_fields.update(
            {
                "turn_scores": agent_data.turn_scores,
                "tool_rewards": agent_data.tool_rewards,
                "num_context_resets": agent_data._num_resets,
                "forced_answer_injected": bool(agent_data.metrics.get("forced_answer_injected", False)),
                "turn_limit_rescued": bool(agent_data.metrics.get("turn_limit_rescued", False)),
                "turn_limit_rescues": int(agent_data.metrics.get("turn_limit_rescues", 0)),
            }
        )
        return output

    def _get_generation_prompt(self, agent_data: AgentData) -> list[int]:
        """Return the token list to feed vLLM for generation.

        Normally this is the full trajectory `prompt_ids`. After a context
        reset, it is the reset prefix (system + original user task + reset
        note, tokenized once) followed by only the tokens accumulated since
        the last reset. This keeps vLLM under `max_model_len` while
        `prompt_ids` (used for training / reward / response_mask alignment)
        remains a complete rollout.
        """
        if not agent_data._context_was_reset:
            return agent_data.prompt_ids
        post_reset_tokens = agent_data.prompt_ids[agent_data._post_reset_offset :]
        return agent_data._reset_generation_prefix + list(post_reset_tokens)

    async def _maybe_reset_context(self, agent_data: AgentData) -> bool:
        """Reset the vLLM generation prompt when it crosses the threshold.

        Keeps the original system prompt + original user task + a reset note
        in the generation prompt. `prompt_ids` / `response_mask` are not
        modified; the reset only changes what vLLM sees on the next call.

        Returns True iff a reset was performed this call.
        """
        if not self.context_reset_enabled or self.context_reset_threshold <= 0:
            return False

        gen_prompt = self._get_generation_prompt(agent_data)
        if len(gen_prompt) < self.context_reset_threshold:
            return False
        return await self._do_context_reset(agent_data, reason="threshold")

    async def _do_context_reset(self, agent_data: AgentData, reason: str = "threshold") -> bool:
        """Context reset — truncates what sglang sees on the next generation
        while leaving `prompt_ids` / `response_mask` untouched so GRPO still
        trains on the full trajectory.

        Two body styles, gated by `context_reset_mode`:
          - "summarizer"    : [system, original_user, <summary note>] — note
                              comes from Claude Haiku if ContextSummarizer
                              is configured, else the static message. Best
                              for keeping rollouts submitting.
          - "sliding_window": [system, original_user, *last N×2 messages] —
                              keeps recent tool/assistant turns verbatim.

        Resets fire freely — there is no hard cap on `_num_resets`. The
        trajectory is instead bounded by `response_length` (training-tensor
        hard cap) and by the soft `force_answer_token_threshold` which the
        caller uses to inject a finish-tool prefix before the hard cap.
        """

        # Find the first system message and first user message to anchor the
        # reset prefix on the task framing.
        system_msg = None
        user_msg = None
        first_user_idx = None
        for idx, msg in enumerate(agent_data.messages):
            role = msg.get("role")
            if role == "system" and system_msg is None:
                system_msg = msg
            elif role == "user" and user_msg is None:
                user_msg = msg
                first_user_idx = idx
            if system_msg is not None and user_msg is not None:
                break
        if user_msg is None:
            return False

        reset_messages: list[dict[str, Any]] = []
        body_descr: str

        if self.context_reset_mode == "sliding_window":
            # Keep [system, user, last N×2 tail messages].
            tail_start = (first_user_idx or 0) + 1
            tail = agent_data.messages[tail_start:]
            keep_n = max(0, self.context_reset_keep_last_rounds) * 2
            if keep_n and len(tail) > keep_n:
                tail = tail[-keep_n:]
            if system_msg is not None:
                reset_messages.append(system_msg)
            reset_messages.append(user_msg)
            reset_messages.extend(tail)
            body_descr = f"sliding_window(keep={len(tail)} tail msgs)"
        else:
            # Default: summarizer — try Claude Haiku, fall back to static note.
            summary: Optional[str] = None
            if self.context_summarizer is not None:
                try:
                    summary = await self.context_summarizer.summarize(agent_data.messages)
                except Exception as e:  # noqa: BLE001
                    logger.warning("context summarizer raised (%s); using static reset", e)
                    summary = None

            if summary:
                reset_note = (
                    "[CONTEXT RESET] Your previous research context has been condensed. "
                    "Here is a summary of your research so far:\n\n"
                    f"{summary}\n\n"
                    "The original question is above. Continue your research based on these findings — "
                    "you may search again for missing details or submit your answer."
                )
            else:
                reset_note = self.context_reset_message

            if system_msg is not None:
                reset_messages.append(system_msg)
            reset_messages.append(user_msg)
            reset_messages.append({"role": "user", "content": reset_note})
            body_descr = f"summarizer(used={'haiku' if summary else 'static'})"

        try:
            reset_prompt_ids = await self.apply_chat_template(
                reset_messages,
                tools=self.tool_schemas,
                images=agent_data.image_data,
                videos=agent_data.video_data,
            )
        except Exception as e:  # noqa: BLE001
            # Some chat templates reject a conversation that starts a tool/
            # user block without a preceding assistant turn. Fall back to a
            # tighter keep (assistant-terminated tail or static note).
            logger.warning("reset prefix tokenize failed (%s); retrying minimal", e)
            trimmed = [system_msg, user_msg] if system_msg is not None else [user_msg]
            if self.context_reset_mode == "sliding_window":
                for m in reversed(reset_messages[2 if system_msg is not None else 1:]):
                    if m.get("role") == "assistant":
                        trimmed.append(m)
                        break
            else:
                trimmed.append({"role": "user", "content": self.context_reset_message})
            reset_prompt_ids = await self.apply_chat_template(
                trimmed,
                tools=self.tool_schemas,
                images=agent_data.image_data,
                videos=agent_data.video_data,
            )

        agent_data._context_was_reset = True
        agent_data._reset_generation_prefix = reset_prompt_ids
        agent_data._post_reset_offset = len(agent_data.prompt_ids)
        agent_data._num_resets += 1

        # Record the reason so downstream logic / metrics can differentiate
        # threshold-triggered resets from turn-limit rescues.
        last_reasons = agent_data.metrics.setdefault("context_reset_reasons", [])
        last_reasons.append(reason)

        logger.warning(
            "[RESET] Context reset #%d/%d (reason=%s, mode=%s) for request %s: "
            "%s; generation prompt %d tokens (threshold=%d, full prompt_ids=%d)",
            agent_data._num_resets,
            self.context_reset_max_count,
            reason,
            self.context_reset_mode,
            agent_data.request_id,
            body_descr,
            len(reset_prompt_ids),
            self.context_reset_threshold,
            len(agent_data.prompt_ids),
        )
        return True

    async def _turn_limit_rescue(self, agent_data: AgentData) -> bool:
        """On turn-limit hit, do a context reset and extend the turn budget
        so the model can keep rolling out. Bounded by the same
        `context_reset_max_count` counter as threshold-triggered resets —
        when the cap is hit, returns False and the caller falls through to
        `_maybe_force_answer`.

        Returns True iff the rescue fired; caller should then return
        AgentState.GENERATING to continue the loop instead of terminating.
        """
        # The rescue is only useful if context reset is plumbed — otherwise
        # extending turns without shortening the prompt just re-hits the same
        # response-length limit immediately.
        if not self.context_reset_enabled:
            return False

        reset_ok = await self._do_context_reset(agent_data, reason="turn_limit")
        if not reset_ok:
            return False

        n_rescued = int(agent_data.metrics.get("turn_limit_rescues", 0))

        # Extend turn budgets **per-agent-data** (self.max_* is shared across
        # all rollouts in this loop instance — never mutate it). The effective
        # caps stored on metrics override self.max_* at the call sites below.
        # Extension size matches the original `max_assistant_turns` so each
        # rescue effectively doubles the remaining budget.
        extension = self.max_assistant_turns or 0
        new_max_assistant = agent_data.assistant_turns + extension if self.max_assistant_turns else None
        new_max_user = agent_data.user_turns + extension if self.max_user_turns else None
        if new_max_assistant is not None:
            agent_data.metrics["max_assistant_turns_effective"] = new_max_assistant
        if new_max_user is not None:
            agent_data.metrics["max_user_turns_effective"] = new_max_user

        n_rescued += 1
        agent_data.metrics["turn_limit_rescues"] = n_rescued
        # Keep the legacy bool flag in sync for downstream consumers that
        # just check "did rescue fire at all".
        agent_data.metrics["turn_limit_rescued"] = True
        # Use WARNING so the event is visible at default verl log level
        # without bumping the whole logger to INFO (would flood with vLLM
        # chatter). This is a per-rollout-once event so volume is bounded.
        logger.warning(
            "[RESCUE] Turn-limit rescue #%d for request %s: context reset done, "
            "budget extended by %d turns (effective max_assistant=%s max_user=%s)",
            n_rescued,
            agent_data.request_id,
            extension,
            new_max_assistant,
            new_max_user,
        )
        return True

    async def _maybe_force_answer(self, agent_data: AgentData) -> bool:
        """Inject a forced answer prefix into the trajectory on budget hit.

        Two variants, gated by `multi_turn.force_finish_tool_enabled`:
          - (default) `<answer>` text prefix — for base/non-SFT models that
            learned a free-text <answer>…</answer> submission format.
          - (SFT path) Qwen3-XML `<tool_call><function=ehr.finish>
            <parameter=response>` prefix — for SFT-ed Qwen3.5 models that
            learned to submit via the ehr.finish tool call with `response`
            as the argument name.

        In both cases the model has one last chance to emit an answer body
        (plus the closing token) on its next generation step.

        Returns True iff a forced prefix was injected this call.
        """
        if agent_data.metrics.get("forced_answer_injected"):
            return False

        # We intentionally do NOT repeat the semantic-alignment reminder in the
        # force-answer <think> block here: at this point the model has no more
        # tool turns to actually call `ehr.get_candidates_by_*`, so the
        # reminder would be noise. Alignment is steered via the system-prompt
        # nudge + the early-finish reward bonus instead.
        if self.force_finish_tool_enabled:
            tool_name = self.force_finish_tool_name
            param = self.force_finish_tool_param
            # Qwen3-XML tool-call prefix (matches what the SFT model was
            # trained to emit). The model fills in the `response=[...]` body
            # and closes the tool_call block.
            prefix_text = (
                "<|im_start|>assistant\n"
                "<think>\n"
                "I've reached my turn limit. Based on all my research so far, "
                f"I need to submit my final answer now via {tool_name}.\n"
                "</think>\n\n"
                f"<tool_call>\n<function={tool_name}>\n<parameter={param}>\n"
            )
            tag = f"[FORCE_ANSWER] Forced {tool_name}({param}=…) tool-call prefix injected"
        else:
            prefix_text = (
                "<|im_start|>assistant\n"
                "<think>\n"
                "I've reached my turn limit. Based on all my research so far, "
                "I need to provide my final answer now.\n"
                "</think>\n\n"
                "<answer>"
            )
            tag = "[FORCE_ANSWER] Forced <answer> prefix injected"

        prefix_ids = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.encode(prefix_text, add_special_tokens=False),
        )
        # Only inject if there's still room for the prefix + at least a short
        # completion; otherwise terminate cleanly.
        if len(agent_data.response_mask) + len(prefix_ids) + 16 > self.response_length:
            return False

        agent_data.prompt_ids += prefix_ids
        agent_data.response_mask += [1] * len(prefix_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(prefix_ids)
        agent_data.metrics["forced_answer_injected"] = True
        logger.warning(
            "%s for request %s (assistant_turns=%d, user_turns=%d, response_len=%d)",
            tag,
            agent_data.request_id,
            agent_data.assistant_turns,
            agent_data.user_turns,
            len(agent_data.response_mask),
        )
        return True

    async def _tokenize_tool_messages_fallback(
        self, add_messages: list[dict[str, Any]], agent_data: AgentData
    ) -> list[int]:
        """Render tool/user messages when apply_chat_template rejects a
        standalone message list (e.g. Qwen3.5's strict chat template).

        Strategy: (1) re-tokenize the full conversation with tool schemas and
        return the delta against the current prompt_ids; (2) if that is empty,
        emit a bare `<|im_start|>user\n<tool_response>…</tool_response><|im_end|>`
        block directly and encode with the raw tokenizer.
        """
        try:
            full_ids = await self.apply_chat_template(
                agent_data.messages,
                tools=self.tool_schemas,
                images=agent_data.image_data,
                videos=agent_data.video_data,
            )
            current_len = len(agent_data.prompt_ids)
            delta = full_ids[current_len:]
            if delta:
                return list(delta)
        except Exception as e:
            logger.warning("Full-conversation re-tokenization fallback failed: %s", e)

        # Last-resort raw-string encoding. Works for Qwen-family models that
        # expect <|im_start|>user\n<tool_response>…</tool_response><|im_end|>\n.
        parts: list[str] = []
        for m in add_messages:
            role = m.get("role")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                )
            if role == "tool":
                parts.append(f"<tool_response>\n{content}\n</tool_response>")
            else:
                parts.append(str(content))
        raw = "<|im_start|>user\n" + "\n".join(parts) + "<|im_end|>\n"
        return await self.loop.run_in_executor(
            None, lambda: self.tokenizer.encode(raw, add_special_tokens=False)
        )

    async def _handle_pending_state(self, agent_data: AgentData, sampling_params: dict[str, Any]) -> AgentState:
        """Handle the pending state: prepare the prompt and start generation."""
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self.tool_schemas,
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        agent_data.prompt_ids = prompt_ids
        return AgentState.GENERATING

    async def _handle_generating_state(
        self, agent_data: AgentData, sampling_params: dict[str, Any], ignore_termination: bool = False
    ) -> AgentState:
        """Handle the generating state: generate model response and check for tool calls."""
        add_messages: list[dict[str, Any]] = []

        # Possibly shrink the vLLM prompt if it would cross the configured
        # threshold. `prompt_ids` (the training trajectory) is never modified.
        await self._maybe_reset_context(agent_data)
        generation_prompt = self._get_generation_prompt(agent_data)

        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=generation_prompt,
                sampling_params=sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )
        # first time to set num_preempted
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
        # then add num_preempted to the metrics
        else:
            agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

        if not agent_data.extra_fields:
            agent_data.extra_fields.update(output.extra_fields)
        else:
            # Multi-round calls, only update the maximum max_global_steps.
            max_global_steps = output.extra_fields.get("max_global_steps", None)
            if max_global_steps:
                agent_data.extra_fields["max_global_steps"] = max_global_steps

        agent_data.assistant_turns += 1
        agent_data.response_ids = output.token_ids
        agent_data.prompt_ids += agent_data.response_ids
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        if output.log_probs:
            agent_data.response_logprobs += output.log_probs

        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        # Termination/rescue ladder.
        #
        # 1. `force_answer_token_threshold` (soft cap): when response_mask crosses
        #    this count, force-inject a finish-tool prefix so the model uses its
        #    remaining `response_length − threshold` tokens to emit the answer
        #    rather than keep exploring. Leaves headroom inside response_length.
        # 2. `response_length` (hard cap): last-resort safety net — if somehow
        #    we got past force-answer without terminating, try a context reset
        #    + rescue, otherwise inject force-answer (which will no-op if there
        #    are truly no tokens left) and terminate.
        # 3. Per-turn caps (`max_assistant_turns`, `max_user_turns`): same
        #    rescue → force-answer → terminate ladder. Rescues fire freely;
        #    there is no hard cap on rescue count.
        eff_max_assistant = agent_data.metrics.get("max_assistant_turns_effective") or self.max_assistant_turns
        eff_max_user = agent_data.metrics.get("max_user_turns_effective") or self.max_user_turns

        # Soft-cap force-answer: crosses threshold but still under hard cap.
        if (
            not ignore_termination
            and self.force_answer_token_threshold > 0
            and len(agent_data.response_mask) >= self.force_answer_token_threshold
            and len(agent_data.response_mask) < self.response_length
            and not agent_data.metrics.get("forced_answer_injected")
        ):
            if await self._maybe_force_answer(agent_data):
                return AgentState.GENERATING
            # Force-answer guard said no room — fall through to hard cap.

        if not ignore_termination and len(agent_data.response_mask) >= self.response_length:
            # Hard response-length exhausted: this is the last-resort guard.
            # Do NOT rescue here — rescue keeps rollouts looping past the hard
            # training-tensor cap. Try one final force-answer (it's size-aware
            # and will no-op if there's truly no room), otherwise TERMINATE.
            if await self._maybe_force_answer(agent_data):
                return AgentState.GENERATING
            return AgentState.TERMINATED
        if eff_max_assistant and agent_data.assistant_turns >= eff_max_assistant:
            if await self._turn_limit_rescue(agent_data):
                return AgentState.GENERATING
            if await self._maybe_force_answer(agent_data):
                return AgentState.GENERATING
            return AgentState.TERMINATED
        if eff_max_user and agent_data.user_turns >= eff_max_user:
            if await self._turn_limit_rescue(agent_data):
                return AgentState.GENERATING
            if await self._maybe_force_answer(agent_data):
                return AgentState.GENERATING
            return AgentState.TERMINATED

        # Extract tool calls
        tools = [tool.tool_schema for tool in self.tools.values()]
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(agent_data.response_ids, tools)

        # Handle interaction if needed
        if self.interaction_config_file:
            assistant_message = await self.loop.run_in_executor(
                None, lambda: self.tokenizer.decode(agent_data.response_ids, skip_special_tokens=True)
            )
            add_messages.append({"role": "assistant", "content": assistant_message})
            agent_data.messages.extend(add_messages)

        # Determine next state
        if agent_data.tool_calls:
            return AgentState.PROCESSING_TOOLS
        elif self.interaction_config_file:
            return AgentState.INTERACTING
        else:
            return AgentState.TERMINATED

    async def _handle_processing_tools_state(self, agent_data: AgentData) -> AgentState:
        """Handle the processing tools state: execute tool calls and prepare tool responses."""
        add_messages: list[dict[str, Any]] = []
        new_images_this_turn: list[Any] = []  # Local variable instead of agent_data attribute

        tasks = []
        tool_call_names = []
        for tool_call in agent_data.tool_calls[: self.max_parallel_calls]:
            tasks.append(self._call_tool(tool_call, agent_data.tools_kwargs, agent_data))
            tool_call_names.append(tool_call.name)

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        # Process tool responses and update multi_modal_data
        # Removed: agent_data.new_images_this_turn = []
        for tool_response, tool_reward, _ in responses:
            # Create message from tool response
            if tool_response.image or tool_response.video:
                # Multi-modal content with structured format
                if not getattr(self.processor, "image_processor", None):
                    raise ValueError(
                        "Multimedia data can only be processed by `processor`, but the processor is None. "
                        "This error is often caused if you are using a LLM model but your tool returns multimodal "
                        "data. Plase use a vlm as the base model."
                    )
                content = []
                if tool_response.image:
                    content.append({"type": "image"})
                if tool_response.video:
                    content.append({"type": "video"})
                if tool_response.text:
                    content.append({"type": "text", "text": tool_response.text})
                message = {"role": "tool", "content": content}
            else:
                # Text-only content
                message = {"role": "tool", "content": tool_response.text or ""}

            add_messages.append(message)

            # Handle image data
            if tool_response.image:
                # Add new image data
                if isinstance(tool_response.image, list):
                    # Ensure all elements in the list are valid image objects
                    for img in tool_response.image:
                        if img is not None:  # Add a check to ensure the image is not None
                            new_images_this_turn.append(img)  # Using local variable
                else:
                    # Ensure the image is not None
                    if tool_response.image is not None:
                        new_images_this_turn.append(tool_response.image)  # Using local variable

            # Handle video data
            if tool_response.video:
                # Currently not supported, raise informative error
                logger.warning("Multimedia type 'video' is not currently supported. Only 'image' is supported.")
                raise NotImplementedError(
                    "Multimedia type 'video' is not currently supported. Only 'image' is supported."
                )

            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)

        agent_data.messages.extend(add_messages)

        if self.tool_parser_name == "gpt-oss":
            logger.info("manually format tool responses for gpt-oss")
            tool_response_text = build_gpt_oss_tool_response_text(add_messages, tool_call_names)
            response_ids = await self.loop.run_in_executor(
                None, lambda: self.tokenizer.encode(tool_response_text, add_special_tokens=False)
            )
        else:
            # Note that we have to pass None to the images and videos if there are no new images / videos
            # to stay compatible with downstream image processing logic!
            images = new_images_this_turn if new_images_this_turn else None
            videos = None
            try:
                response_ids = await self.apply_chat_template(
                    add_messages,
                    images=images,
                    videos=videos,
                    remove_system_prompt=True,
                )
            except Exception as e:
                # Qwen3.5-style strict chat templates reject a standalone
                # [{"role":"tool", ...}] list. Fall back to re-tokenizing the
                # full conversation and diffing, or raw-encoding tool_response.
                logger.warning(
                    "apply_chat_template(tool-delta) failed (%s); using fallback tokenization", e
                )
                response_ids = await self._tokenize_tool_messages_fallback(add_messages, agent_data)

        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            if await self._maybe_force_answer(agent_data):
                # On forced-answer injection the prefix tokens were already
                # appended; skip the tool-delta to avoid pushing past budget.
                return AgentState.GENERATING
            return AgentState.TERMINATED
        # Update prompt_ids and response_mask

        if new_images_this_turn:
            if agent_data.image_data is None:
                agent_data.image_data = []
            elif not isinstance(agent_data.image_data, list):
                agent_data.image_data = [agent_data.image_data]
            for img in new_images_this_turn:
                agent_data.image_data.append(img)

        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)
        agent_data.user_turns += 1
        return AgentState.GENERATING

    async def _handle_interacting_state(self, agent_data: AgentData) -> AgentState:
        """Handle the interacting state: get user input from interaction."""
        (
            should_terminate_sequence,
            interaction_responses,
            reward,
            metrics,
        ) = await agent_data.interaction.generate_response(
            agent_data.request_id, agent_data.messages, **agent_data.interaction_kwargs
        )

        # If the interaction returns an empty user turn (e.g. "no answer yet,
        # keep rolling out"), skip injecting a placeholder user message so the
        # trajectory doesn't get cluttered with repeated nags. The agent loop
        # simply goes back to GENERATING to let the model continue naturally.
        if not should_terminate_sequence and not (interaction_responses or "").strip():
            if reward is not None:
                agent_data.turn_scores.append(reward)
            return AgentState.GENERATING

        agent_data.user_turns += 1

        add_messages: list[dict[str, Any]] = [{"role": "user", "content": interaction_responses}]
        agent_data.messages.extend(add_messages)

        if reward is not None:
            agent_data.turn_scores.append(reward)

        # Update prompt with user responses (similar to _handle_processing_tools_state)
        try:
            response_ids = await self.apply_chat_template(
                add_messages,
                remove_system_prompt=True,
            )
        except Exception as e:
            logger.warning(
                "apply_chat_template(interaction-delta) failed (%s); using fallback tokenization", e
            )
            response_ids = await self._tokenize_tool_messages_fallback(add_messages, agent_data)

        # Update prompt_ids and response_mask
        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        # double check prompt
        # Check termination condition
        if should_terminate_sequence:
            return AgentState.TERMINATED
        else:
            return AgentState.GENERATING

    async def _call_tool(
        self, tool_call: FunctionCall, tools_kwargs: dict[str, Any], agent_data: AgentData
    ) -> tuple[ToolResponse, float, dict]:
        """Call tool and return tool response."""
        tool, instance_id = None, None
        try:
            # TODO: append malformed tool_call to the prompt: invalid function name or arguments
            tool_name = tool_call.name
            tool_args = json.loads(tool_call.arguments)
            tool = self.tools[tool_name]
            kwargs = tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(create_kwargs=kwargs.get("create_kwargs", {}))
            tool_execution_response, tool_reward, res = await tool.execute(
                instance_id, tool_args, agent_data=agent_data
            )
        except Exception as e:
            logger.warning(f"Error when executing tool: {e}")
            return (
                ToolResponse(
                    text=f"Error when executing tool: {e}",
                ),
                0.0,
                {},
            )
        finally:
            if tool and instance_id:
                await tool.release(instance_id)

        tool_response_text = tool_execution_response.text
        if tool_response_text and len(tool_response_text) > self.max_tool_response_length:
            if self.tool_response_truncate_side == "left":
                tool_response_text = tool_response_text[: self.max_tool_response_length] + "...(truncated)"
            elif self.tool_response_truncate_side == "right":
                tool_response_text = "(truncated)..." + tool_response_text[-self.max_tool_response_length :]
            else:
                length = self.max_tool_response_length // 2
                tool_response_text = tool_response_text[:length] + "...(truncated)..." + tool_response_text[-length:]

        # Create ToolResponse from tool execution result
        tool_response_kwargs = {"text": tool_response_text}

        # Add multimedia data if present
        for attr_name in ["image", "video"]:
            if hasattr(tool_execution_response, attr_name):
                attr_value = getattr(tool_execution_response, attr_name)
                if attr_value is not None:
                    tool_response_kwargs[attr_name] = attr_value

        return ToolResponse(**tool_response_kwargs), tool_reward, res

    def _initialize_interactions(self, interaction_config_file):
        """Initialize interactions from configuration.
        Returns:
            dict[str, BaseInteraction]: A dictionary mapping interaction names to interaction instances.
        """
        if interaction_config_file is None:
            return {}

        interaction_map = initialize_interactions_from_config(interaction_config_file)
        return interaction_map
