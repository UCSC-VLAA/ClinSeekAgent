"""Dump agentic + reasoning trajectories for a curated list of Claude Opus
cases, so that analyze_trajectory.py and the case renderer have everything
they need.

Also copies any linked CXR jpg(s) into a per-case subdir for MM cases.

Outputs per case (in findings_20260501/):
    trajectory_<slug>.md                 — full agentic trajectory
    reasoning_<slug>.md                  — full reasoning-mode assistant text
    cases_assets/<slug>/cxr_*.jpg        — MM-only: linked chest-X-ray images
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/results")
OUT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/findings_20260501")
CASES_ROOT = OUT / "cases"
CASES_ROOT.mkdir(parents=True, exist_ok=True)


def _case_dir(slug: str) -> Path:
    d = CASES_ROOT / slug
    d.mkdir(parents=True, exist_ok=True)
    return d

EHR_AGENTIC = ROOT / "ehr_bench/agentic/full1800/claude_opus_4_6/results.jsonl"
EHR_ONESHOT = ROOT / "ehr_bench/oneshot/full1800/claude_opus_4_6/results.jsonl"
MM_AGENTIC = ROOT / "mm_bench/agentic/full2703/claude_opus_4_6/results.jsonl"
MM_ONESHOT = ROOT / "mm_bench/oneshot/full2703/claude_opus_4_6/results.jsonl"
MM_TEST_JSONL = Path(
    "/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench_tests/combined_test_set_nonempty.jsonl"
)
MM_BENCH_ROOTS = [
    Path("/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/EHRXQAAgentBench_v3"),
    Path("/fsx-shared/juncheng/EHR/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3"),
]


@dataclass
class Case:
    slug: str
    qid: str
    kind: str                  # "ehr" | "mm"
    mode: str                  # "won_the_answer" | "lost_the_answer"
    title: str                 # human-readable heading
    outcome_note: str          # <=60 words for the case header
    ag_path: Path = field(init=False)
    os_path: Path = field(init=False)

    def __post_init__(self):
        if self.kind == "ehr":
            self.ag_path, self.os_path = EHR_AGENTIC, EHR_ONESHOT
        elif self.kind == "mm":
            self.ag_path, self.os_path = MM_AGENTIC, MM_ONESHOT
        else:
            raise ValueError(self.kind)


CASES: List[Case] = [
    # ==== Original trio ====
    Case("case1_lengthofstay",         "ehr_bench_risk_prediction_6017",
         "ehr", "won_the_answer",
         "Case 1 — LengthOfStay_3day: agentic recovers DRG + prescription horizon",
         "Agentic Opus discovered drgcodes with DRG-806 'Vaginal delivery WITH CC' — the billing code "
         "is absent from the reasoning-mode prompt and directly encodes extended-stay expectation."),
    Case("case2_mm_phenotyping",       "medmod_phenotyping_test_38131454_130.604444",
         "mm", "won_the_answer",
         "Case 2 — MM phenotyping: CXR vision + SQL + taxonomy lookup",
         "Agentic Opus ran 196 messages including a chest-xray classifier + report generator and "
         "11 browser calls to recover the 25-phenotype Harutyunyan taxonomy."),
    Case("case3_pyxis",                "ehr_bench_decision_making_11698",
         "ehr", "lost_the_answer",
         "Case 3 — Pyxis next-dispense: answer lost in 66 tool calls",
         "Agentic Opus reasoned from clinical guidelines to metronidazole while the reasoning-mode "
         "prompt directly showed the next Pyxis pull was piperacillin."),

    # ==== 2 new text-only agentic wins ====
    Case("case4_inpatient_mortality",  "ehr_bench_risk_prediction_5496",
         "ehr", "won_the_answer",
         "Case 4 — Inpatient mortality: agentic reads the discharge timeline",
         "Agentic Opus correctly predicts death during hospitalization using evidence the rule-based "
         "reasoning prompt omitted."),
    Case("case5_ed_hospitalization",   "ehr_bench_risk_prediction_858",
         "ehr", "won_the_answer",
         "Case 5 — ED hospitalization: agentic finds the discharge-home signal",
         "Agentic Opus predicts 'no admission' from evidence outside the curated reasoning prompt."),

    # ==== 2 new MM agentic wins ====
    Case("case6_mm_decompensation",    "medmod_decompensation_test_39190812_149.0",
         "mm", "won_the_answer",
         "Case 6 — Multimodal decompensation (24 h mortality)",
         "Agentic Opus composes CXR tools and SQL on ICU events to correctly predict 'no death in "
         "24 h' despite a concerning prompt."),
    Case("case7_mm_mortality",         "medmod_in-hospital-mortality_test_32646816_0",
         "mm", "won_the_answer",
         "Case 7 — Multimodal in-hospital mortality",
         "Agentic Opus reasons through ICU events + CXR evidence to correctly predict 'no death this "
         "stay' where reasoning-mode misses the recovery pattern."),

    # ==== 2 new DM losses ====
    Case("case8_labevents",            "ehr_bench_decision_making_5080",
         "ehr", "lost_the_answer",
         "Case 8 — labevents: agent under-predicts the right labs",
         "On a 63-item gold-label lab panel, agentic Opus commits to just one lab; reasoning-mode "
         "Opus enumerates ~40 labs and scores well."),
    Case("case9_next_event",           "ehr_bench_decision_making_7062",
         "ehr", "lost_the_answer",
         "Case 9 — next_event: agent chases admissions instead of radiology",
         "Gold next-event is 'radiology'; agentic Opus ran 39 tool calls and guessed 'admissions'."),
]


def _find(path: Path, qid: str) -> Dict[str, Any] | None:
    with path.open() as f:
        for line in f:
            if qid not in line:
                continue
            r = json.loads(line)
            if r.get("qid") == qid:
                return r


def _msg_text(m: Dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for blk in c:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    return ""


def dump_agentic_trajectory(row: Dict[str, Any], out_path: Path,
                            trunc_result: int = 1200) -> None:
    msgs = row.get("messages") or []
    results_by_id = {
        m.get("tool_call_id", ""): _msg_text(m)
        for m in msgs if m.get("role") == "tool"
    }
    calls = []
    for mi, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args)
            tcid = tc.get("id", "")
            calls.append({
                "idx": len(calls),
                "msg_idx": mi,
                "name": fn.get("name", "?"),
                "args": args,
                "result": results_by_id.get(tcid, ""),
            })

    lines = [
        f"# Agentic trajectory — {row.get('task','?')} — qid `{row.get('qid')}`",
        "",
        f"- **gold label:** `{row.get('label')}`",
        f"- **total messages:** {len(msgs)}",
        f"- **total tool calls:** {len(calls)}",
        "",
        "## Final answer (`ehr.finish` arguments)",
        "```",
    ]
    for c in calls:
        if "finish" in c["name"].lower():
            lines.append(c["args"])
    lines.append("```")
    lines.append("")
    lines.append("## Tool call trajectory")
    lines.append(
        "Each entry lists the call index, name, args and the full tool result "
        f"(truncated to {trunc_result} chars if longer). When deciding which calls "
        "were vital, focus on calls whose results contained the specific evidence "
        "cited in the final `ehr.think` synthesis."
    )
    lines.append("")
    for c in calls:
        lines.append(f"### Call #{c['idx']} — `{c['name']}` (msg {c['msg_idx']})")
        lines.append("")
        lines.append("**args:**")
        lines.append("```json")
        lines.append(c["args"][:800])
        lines.append("```")
        lines.append("")
        lines.append("**tool result:**")
        lines.append("```")
        r = c["result"]
        if len(r) > trunc_result:
            lines.append(r[:trunc_result] + f"\n…[truncated {len(r)-trunc_result} chars]")
        else:
            lines.append(r or "(empty)")
        lines.append("```")
        lines.append("")
    out_path.write_text("\n".join(lines))


def dump_reasoning_trajectory(row: Dict[str, Any], out_path: Path) -> None:
    """Emit the reasoning-mode output as a standalone markdown file with:
        - the final user prompt (what the model saw)
        - the full assistant reply
    """
    msgs = row.get("messages") or []
    user_text = ""
    for m in msgs:
        if m.get("role") == "user":
            user_text = _msg_text(m)
            break
    asst_text = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            asst_text = _msg_text(m)
            break

    # Extract any final ehr.finish prediction
    preds = []
    for m in reversed(msgs or []):
        for tc in reversed(m.get("tool_calls") or []):
            if "finish" not in (tc.get("function", {}).get("name", "") or "").lower():
                continue
            args = tc["function"].get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if isinstance(args, dict):
                preds = args.get("response", [])
            else:
                preds = args
            break
        if preds:
            break

    lines = [
        f"# Reasoning-mode run — {row.get('task','?')} — qid `{row.get('qid')}`",
        "",
        f"- **gold label:** `{row.get('label')}`",
        f"- **final prediction:** `{preds}`",
        "",
        "## Rendered user prompt (what the model saw)",
        "```",
        user_text if len(user_text) < 30000 else user_text[:30000] + "\n…[truncated]",
        "```",
        "",
        "## Assistant reply (full)",
        "```",
        asst_text if asst_text else "(empty)",
        "```",
    ]
    out_path.write_text("\n".join(lines))


def copy_mm_assets(qid: str, slug: str) -> List[Path]:
    """For MM cases, resolve the image paths and copy the jpg(s) directly
    into the case's folder (cases/<slug>/)."""
    paths: List[Path] = []
    row = None
    with MM_TEST_JSONL.open() as f:
        for line in f:
            if qid not in line:
                continue
            r = json.loads(line)
            if r.get("qid") == qid:
                row = r
                break
    if not row:
        return paths
    dst_dir = _case_dir(slug)
    for rel in row.get("image_paths") or []:
        for root in MM_BENCH_ROOTS:
            src = root / rel
            if src.exists():
                dst = dst_dir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
                paths.append(dst)
                break
    # Also copy reports if any (MedMod radiology sometimes has them)
    for rel in row.get("report_paths") or []:
        for root in MM_BENCH_ROOTS:
            src = root / rel
            if src.exists():
                dst = dst_dir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
                paths.append(dst)
                break
    return paths


def main() -> None:
    for case in CASES:
        ag = _find(case.ag_path, case.qid)
        os_ = _find(case.os_path, case.qid)
        if not ag:
            print(f"  MISSING agentic row: {case.qid}"); continue
        if not os_:
            print(f"  MISSING reasoning row: {case.qid}"); continue

        cdir = _case_dir(case.slug)
        dump_agentic_trajectory(ag, cdir / "trajectory.md")
        dump_reasoning_trajectory(os_, cdir / "reasoning.md")
        print(f"  wrote cases/{case.slug}/trajectory.md  ({len(ag.get('messages', []))} msgs)")
        print(f"  wrote cases/{case.slug}/reasoning.md   ({len(os_.get('messages', []))} msgs)")
        if case.kind == "mm":
            imgs = copy_mm_assets(case.qid, case.slug)
            print(f"    copied {len(imgs)} MM asset(s) → cases/{case.slug}/")


if __name__ == "__main__":
    main()
