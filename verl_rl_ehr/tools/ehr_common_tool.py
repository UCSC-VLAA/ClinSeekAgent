"""BaseTool subclasses that proxy through the EHR MCP server via EHRToolPool.

Exposes one verl tool per EHR MCP tool (e.g. `ehr.load_ehr`, `ehr.run_sql_query`).
`tools_kwargs[tool_name]["create_kwargs"]` carries `subject_id`/`prediction_time`/
`task_type`; those are merged into the MCP `tools/call` arguments at execute time.

Keying: `instance_id = request_id` so EHRToolPool's per-query session (including
loaded_ehrs, load_ehr retry state) is shared across every tool call within a
single rollout trajectory.
"""
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


# Arguments we inject from tools_kwargs["create_kwargs"] (populated by the
# preprocess script per sample). Only include them when the tool schema actually
# declares them — many EHR tools don't take `subject_id` directly.
_CONTEXT_KEYS = ("subject_id", "prediction_time", "task_type")


class EHRMCPTool(BaseTool):
    """Generic EHR MCP tool wrapper.

    Each sample's `tools_kwargs[tool_name]["create_kwargs"]` should carry the
    per-patient context (`subject_id`, `prediction_time`). On `create()` we
    stash it keyed by `request_id`; on `execute()` we merge whichever context
    keys are declared in the tool schema into `parameters` and dispatch via
    MCP. Tool response is the textual result from EHRToolPool.call_tool.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        # Figure out which context keys this tool actually accepts so we don't
        # send unknown kwargs to the MCP server.
        self._accepts: set[str] = set()
        try:
            props = self.tool_schema.function.parameters.properties or {}
            self._accepts = set(props.keys()) & set(_CONTEXT_KEYS)
        except AttributeError:
            self._accepts = set()

    async def create(self, instance_id: Optional[str] = None, create_kwargs: Optional[dict] = None,
                     **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {
            "create_kwargs": dict(create_kwargs or {}),
            "n_calls": 0,
        }
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs
                      ) -> tuple[ToolResponse, float, dict]:
        state = self._instance_dict.get(instance_id, {"create_kwargs": {}, "n_calls": 0})
        create_kwargs = state["create_kwargs"]

        # Merge context keys the tool declares (subject_id/prediction_time/task_type)
        # from create_kwargs into the parameters — but only if the caller didn't
        # already supply them. This lets the SFT model keep passing subject_id
        # explicitly while protecting us if it omits the argument.
        call_args = dict(parameters)
        for k in self._accepts:
            if k not in call_args and k in create_kwargs:
                call_args[k] = create_kwargs[k]

        agent_data = kwargs.get("agent_data")
        qid = getattr(agent_data, "request_id", instance_id)

        pool = get_pool()
        try:
            result = await pool.call_tool(qid, self.name, call_args)
        except Exception as e:
            logger.warning("EHR tool %s failed: %s", self.name, e)
            return ToolResponse(text=f"Error executing {self.name}: {e}"), 0.0, {"error": str(e)}

        state["n_calls"] += 1
        self._instance_dict[instance_id] = state

        # ehr.load_ehr returns a ~1KB table-listing blob that doesn't contain
        # clinically useful information — just "Loading 'diagnoses_icd' with 0
        # rows" etc. Short-circuit it to a minimal status string so we don't
        # burn response_length on every rollout. Model still knows load
        # succeeded and can call get_table_names if it needs the table list.
        if self.name == "ehr.load_ehr":
            return ToolResponse(text="EHR loaded. Use ehr.get_table_names to list available tables."), 0.0, {"tool": self.name}

        return ToolResponse(text=str(result)), 0.0, {"tool": self.name}

    async def release(self, instance_id: str, **kwargs) -> None:
        # We intentionally do NOT cleanup the MCP session on per-call release
        # (EHRToolPool sessions persist for the full rollout keyed by request_id).
        # Session cleanup is handled at the end of the trajectory by the finish
        # tool or by process teardown.
        self._instance_dict.pop(instance_id, None)
