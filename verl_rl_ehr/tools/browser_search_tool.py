"""browser.search — minimal search wrapper with stub mode.

BROWSER_SEARCH_MODE=stub (default): returns "[search disabled]" so smoke
tests and infra-free RL runs work without a search service.

BROWSER_SEARCH_MODE=http: POSTs {"query","topn"} to `search_service_url` and
returns the response body verbatim. No result-ID registry — our EHR SFT
model uses browser.search mostly to fetch medical knowledge, not to cross-
link into browser.open.
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


class BrowserSearchTool(BaseTool):
    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        self.search_service_url = config.get(
            "search_service_url",
            os.environ.get("SEARCH_SERVICE_URL", "http://127.0.0.1:8090") + "/search",
        )
        self.default_topn = int(config.get("topn", 10))
        self.timeout = float(config.get("timeout", 30))

    async def create(self, instance_id: Optional[str] = None, **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {"n": 0}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs
                      ) -> tuple[ToolResponse, float, dict]:
        query = str(parameters.get("query", "")).strip()
        topn = int(parameters.get("topn", self.default_topn) or self.default_topn)
        if not query:
            return ToolResponse(text="Error: empty search query"), 0.0, {}

        agent_data = kwargs.get("agent_data")
        traj_id = getattr(agent_data, "request_id", instance_id)
        increment_tool_calls(traj_id)
        if is_budget_exhausted(traj_id):
            return ToolResponse(text=BUDGET_EXHAUSTED_MSG), 0.0, {"budget_exhausted": True}

        if _stub_mode():
            text = (
                "[search disabled — BROWSER_SEARCH_MODE=stub]\n"
                f"query: {query}\n"
                "No external search results are available. Rely on EHR tools or base-model "
                "medical knowledge for this task."
            )
            if should_warn(traj_id):
                text += BUDGET_WARNING_MSG
            return ToolResponse(text=text), 0.0, {"stub": True}

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                r = await client.post(self.search_service_url, json={"query": query, "topn": topn})
                r.raise_for_status()
                body = r.text
        except Exception as e:  # noqa: BLE001
            logger.warning("browser.search to %s failed: %s", self.search_service_url, e)
            return ToolResponse(text=f"Search failed: {e}"), 0.0, {"error": True}

        if should_warn(traj_id):
            body += BUDGET_WARNING_MSG
        return ToolResponse(text=body), 0.0, {"topn": topn}

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
