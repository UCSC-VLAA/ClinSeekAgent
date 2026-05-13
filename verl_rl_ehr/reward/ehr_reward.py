"""compute_score for verl's custom_reward_function hook.

Combines three signals:

  correctness      = f1_score(extracted_answer, ground_truth)            (weight 2.0)
  efficiency       = max(0, 1 − num_turns / EFFICIENCY_TURN_BUDGET);     (weight 0.2)
                     zero when force-answer was injected.
  format_penalty   = −0.2 flat when no <answer>/ehr.finish found.

Final score clamped to [-0.5, 1.35]. Each component is also returned as
`reward/correctness`, `reward/efficiency`, `reward/format_penalty` so wandb
plots show per-component contribution.

Call signature is what verl.workers.reward_manager.naive passes:

  compute_score(data_source, solution_str, ground_truth, extra_info, **kwargs)

`solution_str` is `tokenizer.decode(response_ids, skip_special_tokens=True)` —
the full assistant side of the trajectory including tool-call XML and tool
responses. We count EHR tool calls and extract the LAST <answer>/ehr.finish
payload.

When env `REWARD_DEBUG_LOG` points at a path, one JSONL row per sample is
written for post-hoc analysis.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional


_PROJECT_ROOT = Path("/fsx-shared/juncheng/EHR")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from openresearcher_ehr.helper.evaluate_results import f1_score as _f1_score_upstream  # noqa: E402


# Weights.
#   correctness (F1):   2.0   → dominant learning signal.
#   efficiency:         0.2   → linearly decreases with num_turns over a
#                                soft budget. Zero when force-answer had to be
#                                injected (i.e. the model didn't commit on its
#                                own), so it subsumes the previous early-finish
#                                bonus / turn-budget penalty / tool-engage shaping.
#   format_penalty:     0.2   → flat −0.2 when no <answer>/ehr.finish found.
W_CORRECTNESS = 2.0
W_EFFICIENCY = 0.2
W_FORMAT_PENALTY = 0.2
# Soft denominator for the efficiency reward: efficiency = max(0, 1 − turns / budget).
# `num_turns` = user_turns + assistant_turns + 1, so with max_assistant_turns=18
# the no-rescue ceiling is ~36, one rescue ~72, two rescues ~108. We set the
# budget to the two-rescue ceiling so efficiency gives a smooth 1→0 signal across
# the full observed range (median ~104, p25 ~90). A rollout that commits by
# num_turns=54 gets efficiency ≈ 0.5, by num_turns=30 gets ≈ 0.72, etc.
EFFICIENCY_TURN_BUDGET = 108
SCORE_MIN, SCORE_MAX = -0.5, 1.35

# Signature string injected by `_maybe_force_answer` in tool_agent_loop.py.
# If present in solution_str the rollout hit the turn-limit rescue fallback
# rather than submitting on its own — used to gate the early-finish bonus.
_FORCE_ANSWER_SIGNATURE = "I've reached my turn limit"


# ---------------------------------------------------------------------------
# Stricter F1 normalization
# ---------------------------------------------------------------------------
# The upstream `f1_score` in openresearcher_ehr only lowercases before set
# comparison. That's too strict for RL: the model often emits the right lab
# name with a subtype qualifier the GT lacks — e.g. "Sodium, Whole Blood" vs
# GT "Sodium" → F1=0. We normalize both sides with a token-set Jaccard match
# in _normalize_term below, preserving upstream's API and call signature.
_PUNCT_RE = re.compile(r"[\(\)\[\]\{\},;:.\"'`]+")
_WS_RE = re.compile(r"\s+")
# Drop qualifier suffixes the model tends to add but GT omits. Only strip
# *after* a separator so we don't eat qualifiers that are part of the head
# name (e.g. "Hemoglobin A1c" keeps the A1c).
_QUALIFIER_SUFFIXES = (
    ", whole blood", ", serum", ", plasma", ", urine", ", blood",
    ", stool", ", csf", ", body fluid", ", other body fluid",
    " (calculated)", " (measured)", " (mdrd equation)", " (ckd-epi)",
)


def _normalize_term(s: str) -> str:
    """Lowercase → drop qualifier suffix → strip punct → collapse ws → strip."""
    t = (s or "").strip().lower()
    for suf in _QUALIFIER_SUFFIXES:
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
            break
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def _extract_gt_strings(standard_answer) -> set:
    """Mirror the upstream f1_score GT extraction (handles str / list[str] /
    list[dict] with atc_name or name)."""
    gt: set = set()
    if isinstance(standard_answer, str):
        gt.add(standard_answer)
    elif isinstance(standard_answer, list):
        has_atc = any(isinstance(a, dict) and a.get("atc_name") for a in standard_answer)
        for ans in standard_answer:
            if isinstance(ans, str):
                gt.add(ans)
            elif isinstance(ans, dict):
                if has_atc:
                    atc = ans.get("atc_name")
                    if isinstance(atc, str):
                        gt.add(atc)
                else:
                    name = ans.get("name")
                    if isinstance(name, str):
                        gt.add(name)
    return gt


def f1_score(predictions, standard_answer):
    """Normalized set-F1 with qualifier-suffix stripping.

    A predicted term matches a GT term when their normalized forms are equal.
    Falls back to the upstream scorer's shape for fields (`f1`, `prec`, `rec`,
    `em`) so downstream code is unchanged.
    """
    pred_strs = {p for p in predictions if isinstance(p, str)}
    gt_strs = _extract_gt_strings(standard_answer)
    if not pred_strs:
        # Keep upstream semantics: empty pred = zero everything.
        return {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0}

    pred_norm = {_normalize_term(p) for p in pred_strs}
    gt_norm = {_normalize_term(g) for g in gt_strs}
    pred_norm.discard("")
    gt_norm.discard("")
    if not gt_norm or not pred_norm:
        return {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0}

    inter = pred_norm & gt_norm
    prec = len(inter) / len(pred_norm)
    rec = len(inter) / len(gt_norm)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    em = 1.0 if pred_norm == gt_norm else 0.0
    return {"f1": f1, "prec": prec, "rec": rec, "em": em}

_ANSWER_TAG_RE = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
_EXACT_RE = re.compile(r"Exact Answer:\s*(.*?)(?:\n|Confidence:|$)", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"Final Answer:\s*(.*?)(?:\n|$)", re.IGNORECASE | re.DOTALL)

# Recognize both JSON tool-call text ("name": "ehr.run_sql_query") and Qwen3/3.5
# native XML tool-calls (<function=ehr.run_sql_query>).
_EHR_TOOL_RE = re.compile(
    r'(?:"name"\s*:\s*"(ehr\.[A-Za-z0-9_.]+)")|(?:<function\s*=\s*(ehr\.[A-Za-z0-9_.]+)\s*>)',
)
_BROWSER_TOOL_RE = re.compile(
    r'(?:"name"\s*:\s*"(browser\.[A-Za-z0-9_.]+)")|(?:<function\s*=\s*(browser\.[A-Za-z0-9_.]+)\s*>)',
)
_FINISH_RE = re.compile(
    r'(?:"name"\s*:\s*"(ehr\.finish)")|(?:<function\s*=\s*(ehr\.finish)\s*>)',
)

# Hermes-style tool-call block emitted by Qwen3.5 under the `hermes` format.
# Rendered literally (via tokenizer.decode with skip_special_tokens=True) as:
#   <tool_call>
#   {"name": "ehr.finish", "arguments": {"answer": [...]}}
#   </tool_call>
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(?P<body>.*?)\s*</tool_call>",
    re.DOTALL,
)
# Qwen3-XML form: <function=ehr.finish>{"answer": [...]}</function>
# or with <parameter=answer>[...]</parameter> subtags.
_QWEN_XML_FINISH_RE = re.compile(
    r"<function\s*=\s*ehr\.finish\s*>(?P<body>.*?)</function>",
    re.DOTALL,
)
# Last-resort: grab "answer": [...] or "answer": "..."  anywhere in the body.
# Greedy across the list so "answer": ["a, b", "c"] is captured whole.
# Also accept "response" as an alias (SFT-ed model emits `response=...`).
_ANSWER_ARRAY_RE = re.compile(
    r'"(?:answer|response)"\s*:\s*(\[[\s\S]*?\])',
    re.DOTALL,
)
_ANSWER_STRING_RE = re.compile(
    r'"(?:answer|response)"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)
# Qwen3-XML subtag form used by SFT-ed model under qwen3_xml parser:
#   <parameter=response>[...]</parameter>  or  <parameter=answer>"..."</parameter>
_QWEN_XML_PARAM_RE = re.compile(
    r"<parameter\s*=\s*(?P<name>answer|response)\s*>(?P<val>[\s\S]*?)</parameter>",
    re.DOTALL,
)


def _parse_finish_body(body: str) -> Optional[Any]:
    """Parse one tool-call body into the `answer` value, or return None.

    Tries three strategies in order:
      1. Full JSON parse of the body -> read obj["answer"]
      2. JSON parse of the "arguments" field -> read its "answer"
      3. Regex-grab "answer": [...] / "answer": "..."
    """
    if not body:
        return None
    body = body.strip()

    # Strategy 1: try full JSON parse (the common Hermes shape).
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        # Hermes: {"name": ..., "arguments": {...}}
        if "arguments" in obj:
            args = obj["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = None
            if isinstance(args, dict):
                if "answer" in args:
                    return args["answer"]
                if "response" in args:
                    return args["response"]
        # Directly on the dict (Qwen3-XML body might be just {"answer": [...]}).
        if "answer" in obj:
            return obj["answer"]
        if "response" in obj:
            return obj["response"]

    # Strategy 1.5: Qwen3-XML subtag form.
    #   <parameter=response>[...]</parameter>  (SFT-style)
    #   <parameter=answer>"..."</parameter>    (generic)
    m_param = _QWEN_XML_PARAM_RE.search(body)
    if m_param:
        val = m_param.group("val").strip()
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            # Plain string payload (e.g. "<parameter=response>diabetes</parameter>").
            return val

    # Strategy 2: regex grab on the raw body — handles malformed JSON as long as
    # the answer field itself parses (e.g. model forgot the closing `}`).
    m_arr = _ANSWER_ARRAY_RE.search(body)
    if m_arr:
        try:
            return json.loads(m_arr.group(1))
        except json.JSONDecodeError:
            return None
    m_str = _ANSWER_STRING_RE.search(body)
    if m_str:
        # Re-decode JSON escapes.
        try:
            return json.loads(f'"{m_str.group(1)}"')
        except json.JSONDecodeError:
            return m_str.group(1)
    return None


def _extract_finish_answer(text: str) -> Optional[Any]:
    """Pull the `answer` field out of the LAST ehr.finish tool call in text."""
    if not text:
        return None
    # Collect candidate (source, body) in appearance order.
    candidates: list[str] = []
    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        body = m.group("body")
        # Only bodies that actually belong to ehr.finish.
        if '"ehr.finish"' in body:
            candidates.append(body)
    for m in _QWEN_XML_FINISH_RE.finditer(text):
        candidates.append(m.group("body"))
    if not candidates:
        return None
    # Walk from the last-emitted finish call backwards; return the first parse
    # that yields a non-None answer.
    for body in reversed(candidates):
        ans = _parse_finish_body(body)
        if ans is not None and ans != "":
            return ans
    return None


def _extract_answer(text: str) -> tuple[Optional[Any], bool]:
    if not text:
        return None, False
    # 1) Explicit <answer>…</answer> tag (preferred when model honors it).
    m = _ANSWER_TAG_RE.findall(text)
    if m:
        return m[-1].strip(), True
    # 2) ehr.finish tool-call arguments (the canonical submission path).
    finish = _extract_finish_answer(text)
    if finish is not None:
        return finish, True
    # 3) Fallback textual patterns.
    m2 = _EXACT_RE.search(text)
    if m2:
        return m2.group(1).strip(), True
    m3 = _FINAL_RE.search(text)
    if m3:
        return m3.group(1).strip(), True
    return None, False


def _as_pred_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    parts = re.split(r"[\n;]|,\s", s)
    return [p.strip() for p in parts if p.strip()]


def _count_matches(regex: re.Pattern, text: str) -> int:
    if not text:
        return 0
    # Each match yields one non-empty capture group.
    return sum(1 for _ in regex.finditer(text))


def _debug_log(payload: dict) -> None:
    path = os.environ.get("REWARD_DEBUG_LOG", "")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def compute_score(
    data_source: str,
    solution_str: str,
    ground_truth: Any,
    extra_info: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Primary entry point called by verl's NaiveRewardManager."""
    extra_info = dict(extra_info or {})

    n_ehr = _count_matches(_EHR_TOOL_RE, solution_str)
    n_browser = _count_matches(_BROWSER_TOOL_RE, solution_str)
    n_finish = _count_matches(_FINISH_RE, solution_str)

    answer_raw, is_explicit = _extract_answer(solution_str)
    preds = _as_pred_list(answer_raw)

    has_answer_signal = is_explicit or n_finish > 0

    # Was the rollout force-finished via _maybe_force_answer? Detect via the
    # signature prefix the agent loop injects. We check this regardless of
    # which reward branch we end up in, because rollouts that fail to emit a
    # parseable answer even after force-answer shouldn't receive the
    # early-finish bonus.
    force_answer_injected = _FORCE_ANSWER_SIGNATURE in solution_str

    # Efficiency: linearly decreases from 1 at turn 0 to 0 at EFFICIENCY_TURN_BUDGET.
    # Zero when the rollout needed force-answer injection — i.e. the model did
    # not commit within the natural budget, so efficiency doesn't apply.
    num_turns = int(extra_info.get("num_turns") or 0)
    if force_answer_injected:
        efficiency = 0.0
    else:
        efficiency = max(0.0, 1.0 - num_turns / max(1, EFFICIENCY_TURN_BUDGET))

    # Format penalty fires when the rollout has no extractable answer signal.
    format_penalty_applied = not preds and not has_answer_signal

    if not preds and not has_answer_signal:
        # No format signal at all — full format penalty, no other components.
        f1 = 0.0
        scores_dict = {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0}
        correctness = 0.0
        score = -W_FORMAT_PENALTY
    elif not preds:
        # ehr.finish seen but couldn't parse — no format penalty, no correctness.
        # Keep the efficiency bonus so the model is still rewarded for attempting
        # to submit within budget.
        f1 = 0.0
        scores_dict = {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0}
        correctness = 0.0
        score = W_EFFICIENCY * efficiency
    else:
        scores_dict = f1_score(preds, ground_truth)
        f1 = float(scores_dict.get("f1", 0.0))
        correctness = f1
        score = (
            W_CORRECTNESS * correctness
            + W_EFFICIENCY * efficiency
        )

    score = max(SCORE_MIN, min(SCORE_MAX, score))

    # Component breakdown (all logged to wandb via reward_extra_info).
    # Each field is the weighted contribution that actually went into `score`,
    # so the wandb plots sum visually to the total reward.
    correctness_weighted = W_CORRECTNESS * (correctness if preds else 0.0)
    # Efficiency only counts when the rollout got past the format gate (it's
    # zeroed out in the no-format branch above).
    efficiency_weighted = W_EFFICIENCY * efficiency if not format_penalty_applied else 0.0
    format_penalty_weighted = -W_FORMAT_PENALTY if format_penalty_applied else 0.0

    out = {
        "score": score,
        "f1": f1 if preds else 0.0,
        "prec": float(scores_dict.get("prec", 0.0)),
        "rec": float(scores_dict.get("rec", 0.0)),
        "em": float(scores_dict.get("em", 0.0)),
        "correctness": correctness if preds else 0.0,
        # Per-component weighted contributions (what the wandb plots see).
        "reward/correctness": correctness_weighted,
        "reward/efficiency": efficiency_weighted,
        "reward/format_penalty": format_penalty_weighted,
        # Raw efficiency (pre-weight) for sanity-checking against num_turns.
        "efficiency_raw": float(efficiency),
        "n_ehr_tool_calls": n_ehr,
        "n_browser_tool_calls": n_browser,
        "n_finish_calls": n_finish,
        "has_answer": bool(preds),
        "has_format_signal": bool(has_answer_signal),
        "force_answer_injected": bool(force_answer_injected),
        "format_penalty_applied": bool(format_penalty_applied),
    }

    row = {
        "data_source": data_source,
        "ground_truth": ground_truth,
        "answer_extracted": (str(answer_raw)[:200] if answer_raw is not None else None),
        "num_turns": int(extra_info.get("num_turns") or 0),
        "num_context_resets": extra_info.get("rollout_reward_scores", {}).get("num_context_resets"),
        "forced_answer_injected": extra_info.get("rollout_reward_scores", {}).get("forced_answer_injected"),
        **out,
    }
    # On finish-without-extracted-answer, include solution_str for post-hoc
    # diagnosis. Bounded to 6 KB to keep the trace file small.
    if n_finish > 0 and not preds:
        row["solution_str_head"] = solution_str[:6000]
    # Optional full-rollout dump for hand-inspection. Controlled via env so we
    # don't spam the trace file during normal runs. Set
    # REWARD_DUMP_FULL_STRS=N to bound total samples captured.
    _dump_cap = int(os.environ.get("REWARD_DUMP_FULL_STRS") or 0)
    if _dump_cap > 0:
        dump_path = os.environ.get("REWARD_FULL_DUMP") or "/tmp/reward_full_dump.jsonl"
        try:
            # Rough cap: ~20 KB per sample average. Writes are serialized by
            # the OS; we accept a small overshoot if multiple workers race.
            cur_size = os.path.getsize(dump_path) if os.path.exists(dump_path) else 0
            if cur_size < _dump_cap * 20000:
                # Reconstruct the original prompt from extra_info (verl only
                # passes response_str to the reward fn, not the rendered prompt).
                task_type = extra_info.get("task_type") or ""
                subject_id = extra_info.get("subject_id") or ""
                prediction_time = extra_info.get("prediction_time") or ""
                try:
                    from openresearcher_ehr.data_utils import TASK_PROMPT_TEMPLATES
                    from verl_rl_ehr.preprocess.build_ehr_rl_parquet import _RL_SYSTEM_PROMPT
                    system_prompt = _RL_SYSTEM_PROMPT
                    tmpl = TASK_PROMPT_TEMPLATES.get(task_type, TASK_PROMPT_TEMPLATES.get("diagnoses_ccs", ""))
                    user_query = tmpl.format(subject_id=str(subject_id), current_time=prediction_time)
                except Exception as e:  # noqa: BLE001
                    system_prompt = f"<reconstruction_error: {e}>"
                    user_query = f"task={task_type} subject={subject_id} t={prediction_time}"
                with open(dump_path, "a") as f:
                    f.write(json.dumps({
                        "data_source": data_source,
                        "task_type": task_type,
                        "subject_id": subject_id,
                        "prediction_time": prediction_time,
                        "system_prompt": system_prompt,
                        "user_query": user_query,
                        "rollout_response": solution_str,
                        "extracted_answer": answer_raw,
                        "ground_truth": ground_truth,
                        "score": score,
                        "f1": f1 if preds else 0.0,
                        "has_answer": bool(preds),
                        "has_format_signal": bool(has_answer_signal),
                        "num_turns": int(extra_info.get("num_turns") or 0),
                        "n_ehr_tool_calls": n_ehr,
                        "n_browser_tool_calls": n_browser,
                        "n_finish_calls": n_finish,
                    }, ensure_ascii=False) + "\n")
        except OSError:
            pass
    _debug_log(row)

    return out
