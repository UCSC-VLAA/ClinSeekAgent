"""browser.open — minimal stub/proxy.

Always returns a stub message in stub mode. In http mode, forwards the
OpenAI-shape parameters to `search_service_url`/open and returns the
response body verbatim.
"""
import logging
import os
from typing import Any, Optional
from uuid import uuid4

import httpx

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse
from verl.utils.rollout_trace import rollout_trace_op

from ._session_state import (
    BUDGET_EXHAUSTED_MSG,
    BUDGET_WARNING_MSG,
    increment_tool_calls,
    is_budget_exhausted,
    should_warn,
)


logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


def _stub_mode() -> bool:
    return os.environ.get("BROWSER_SEARCH_MODE", "stub").lower() == "stub"


class BrowserOpenTool(BaseTool):
    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        self.search_service_url = config.get(
            "search_service_url",
            os.environ.get("SEARCH_SERVICE_URL", "http://127.0.0.1:8090"),
        ).rstrip("/")
        self.timeout = float(config.get("timeout", 30))
        self.default_num_lines = int(config.get("num_lines", 100))

    async def create(self, instance_id: Optional[str] = None, **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {"n": 0}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs
                      ) -> tuple[ToolResponse, float, dict]:
        agent_data = kwargs.get("agent_data")
        traj_id = getattr(agent_data, "request_id", instance_id)
        increment_tool_calls(traj_id)
        if is_budget_exhausted(traj_id):
            return ToolResponse(text=BUDGET_EXHAUSTED_MSG), 0.0, {"budget_exhausted": True}

        if _stub_mode():
            text = (
                "[browser.open disabled — BROWSER_SEARCH_MODE=stub]\n"
                f"args: {parameters}\n"
                "No external web content available. Use EHR tools instead."
            )
            if should_warn(traj_id):
                text += BUDGET_WARNING_MSG
            return ToolResponse(text=text), 0.0, {"stub": True}

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                r = await client.post(f"{self.search_service_url}/open", json=parameters)
                r.raise_for_status()
                body = r.text
        except Exception as e:  # noqa: BLE001
            logger.warning("browser.open failed: %s", e)
            return ToolResponse(text=f"Open failed: {e}"), 0.0, {"error": True}

        if should_warn(traj_id):
            body += BUDGET_WARNING_MSG
        return ToolResponse(text=body), 0.0, {}

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
