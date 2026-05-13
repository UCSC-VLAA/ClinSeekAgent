"""Interaction handler that grades the agent's final answer against the
benchmark ground truth.

Signature matches verl/interactions/base.py::BaseInteraction.generate_response
  -> (should_terminate_sequence, response_text, turn_score, metadata)

Extraction preference:
  1. `agent_data.extra_fields["final_answer"]` written by EHRFinishTool.
  2. Regex over the latest assistant message for <answer>...</answer> or
     "Exact Answer: …" / "Final Answer: …".

Scoring uses the F1 defined in openresearcher_ehr/helper/evaluate_results.py
so RL metrics are comparable with prior Bedrock eval runs.
"""
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


_PROJECT_ROOT = Path("/fsx-shared/juncheng/EHR")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from verl.interactions.base import BaseInteraction  # noqa: E402
from openresearcher_ehr.helper.evaluate_results import f1_score  # noqa: E402


logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


_ANSWER_TAG_RE = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
_EXACT_RE = re.compile(r"Exact Answer:\s*(.*?)(?:\n|Confidence:|$)", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"Final Answer:\s*(.*?)(?:\n|$)", re.IGNORECASE | re.DOTALL)


def _extract_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = _ANSWER_TAG_RE.findall(text)
    if m:
        return m[-1].strip()
    m2 = _EXACT_RE.search(text)
    if m2:
        return m2.group(1).strip()
    m3 = _FINAL_RE.search(text)
    if m3:
        return m3.group(1).strip()
    return None


def _as_pred_list(raw: Any) -> list[str]:
    """Normalize an extracted answer to list[str] for F1 comparison."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    # Allow either a JSON-encoded list or a comma/newline-separated plain string.
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[\n;]|,\s", s)
    return [p.strip() for p in parts if p.strip()]


class EHREvaluationInteraction(BaseInteraction):
    """Grades EHR-Bench trajectories by F1 against `ground_truth`."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._instance_dict: dict[str, dict[str, Any]] = {}

    async def start_interaction(
        self,
        instance_id: Optional[str] = None,
        ground_truth: Any = None,
        **kwargs,
    ) -> str:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {
            "ground_truth": ground_truth if ground_truth is not None else kwargs.get("ground_truth"),
            "task_type": kwargs.get("task_type"),
            "best_f1": 0.0,
            "turns": 0,
        }
        return instance_id

    async def generate_response(
        self,
        instance_id: str,
        messages: list[dict[str, Any]],
        **kwargs,
    ) -> tuple[bool, str, float, dict[str, Any]]:
        state = self._instance_dict.setdefault(
            instance_id, {"ground_truth": kwargs.get("ground_truth"), "task_type": kwargs.get("task_type"),
                          "best_f1": 0.0, "turns": 0}
        )
        state["turns"] += 1
        gt = state["ground_truth"]

        # 1) Preferred path: EHRFinishTool wrote the answer into agent_data.extra_fields.
        agent_data = kwargs.get("agent_data")
        raw_answer: Any = None
        if agent_data is not None:
            raw_answer = agent_data.extra_fields.get("final_answer")

        # 2) Fallback: regex over the latest assistant message.
        if raw_answer in (None, "", []):
            latest_assistant = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                        )
                    latest_assistant = str(content)
                    break
            raw_answer = _extract_from_text(latest_assistant)

        preds = _as_pred_list(raw_answer)
        if not preds:
            # Let the model rollout naturally — don't spam a reminder each turn
            # (that diverges from the system prompt's <answer> guidance, and
            # burns context on a low-signal nag). Just return an empty user
            # turn so the agent loop can advance.
            return False, "", 0.0, {"no_answer": True}

        scores = f1_score(preds, gt)
        f1 = float(scores.get("f1", 0.0))
        state["best_f1"] = max(state["best_f1"], f1)

        return (
            True,  # terminate — we have an answer
            "",    # no response_text needed; trajectory is done
            f1,    # per-turn score (the sole graded turn)
            {
                "f1": f1,
                "precision": float(scores.get("prec", 0.0)),
                "recall": float(scores.get("rec", 0.0)),
                "em": float(scores.get("em", 0.0)),
                "n_pred": len(preds),
            },
        )

    async def calculate_score(self, instance_id: str, **kwargs) -> float:
        return self._instance_dict.get(instance_id, {}).get("best_f1", 0.0)

    async def finalize_interaction(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
