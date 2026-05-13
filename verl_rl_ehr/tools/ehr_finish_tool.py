"""ehr.finish — verl-native answer submission.

Writes the submitted answer into `agent_data.extra_fields["final_answer"]`
and returns a ToolResponse whose text wraps the answer in <answer>…</answer>
so the reward / interaction path can extract it either way.

Not proxied through MCP — the finish action is a pure RL-side signal;
avoiding a round-trip also means the EHR session does not get torn down
before downstream reward extraction.
"""
import json
import logging
import os
from typing import Any, Optional
from uuid import uuid4

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse
from verl.utils.rollout_trace import rollout_trace_op

from ._mcp_session import get_pool


logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class EHRFinishTool(BaseTool):
    """Final-answer submission. Stores parsed answer on agent_data so the
    interaction / reward can grade it without text-parsing the trajectory."""

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}

    async def create(self, instance_id: Optional[str] = None, create_kwargs: Optional[dict] = None,
                     **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {"submitted": False, "answer": None}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs
                      ) -> tuple[ToolResponse, float, dict]:
        # SFT-ed Qwen3.5 was trained with `response`; accept it first, fall
        # back to `answer` for non-SFT callers.
        raw_answer = parameters.get("response")
        if raw_answer is None or raw_answer == "":
            raw_answer = parameters.get("answer", "")
        # Normalize to list[str] for list-typed tasks (diagnoses_ccs etc.) but
        # also accept plain strings.
        if isinstance(raw_answer, str):
            answer: Any = raw_answer.strip()
        elif isinstance(raw_answer, list):
            answer = [str(a).strip() for a in raw_answer]
        else:
            answer = raw_answer

        self._instance_dict[instance_id] = {"submitted": True, "answer": answer}

        agent_data = kwargs.get("agent_data")
        if agent_data is not None:
            agent_data.extra_fields["final_answer"] = answer
            agent_data.extra_fields["final_answer_submitted"] = True

        # Also cleanup the MCP session — this trajectory is about to end.
        try:
            qid = getattr(agent_data, "request_id", instance_id)
            await get_pool().cleanup(qid)
        except Exception as e:  # noqa: BLE001
            logger.debug("MCP cleanup after finish failed (ignored): %s", e)

        # Wrap in <answer> so regex-based extractors (reward/interaction) still
        # find it even if agent_data.extra_fields is not consulted.
        if isinstance(answer, list):
            payload = json.dumps(answer, ensure_ascii=False)
        else:
            payload = str(answer)
        return ToolResponse(text=f"<answer>{payload}</answer>"), 0.0, {"answer_submitted": True}

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
