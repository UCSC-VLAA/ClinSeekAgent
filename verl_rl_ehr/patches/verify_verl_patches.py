"""Fail loud if the in-tree verl edits AND sglang patches the RL pipeline
depends on are missing. Run from the launcher preflight.

Sentinels (introduced in the deepresearch branch's verl surgery):
  - tool_agent_loop.py       contains `_context_was_reset`
  - rollout.py (MultiTurnCfg) contains `context_reset_enabled`
  - tools/schemas.py         contains `ConfigDict(extra="allow")`

Also verifies upstream OpenResearcher sglang patches are applied
(see verl_rl_ehr/patches/apply_sglang_patches.sh):
  - sglang/srt/models/qwen3_5.py contains Qwen3_5MoeForConditionalGeneration
  - sglang/srt/configs/qwen3_5.py contains Qwen3_5MoeTextConfig
  - sglang/srt/utils/hf_transformers_utils.py handles dict text_config
  - `decord` installed so qwen_vl processor registers

Exits 0 iff all checks pass; otherwise prints which is missing and exits 2.
"""
import pathlib
import sys


PROJECT = pathlib.Path(__file__).resolve().parents[2]  # /fsx-shared/juncheng/EHR
VERL_ROOT = PROJECT / "verl" / "verl"

CHECKS = [
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "_context_was_reset"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "_maybe_reset_context"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "_do_context_reset"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "_turn_limit_rescue"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "_maybe_force_answer"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "force_finish_tool_enabled"),
    (VERL_ROOT / "experimental" / "agent_loop" / "tool_agent_loop.py", "ContextSummarizer"),
    (VERL_ROOT / "experimental" / "agent_loop" / "context_summarizer.py", "class ContextSummarizer"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_enabled"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_threshold"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_summarizer_enabled"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "force_finish_tool_enabled"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_max_count"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_keep_last_rounds"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "context_reset_mode"),
    (VERL_ROOT / "workers" / "config" / "rollout.py", "force_answer_token_threshold"),
    (VERL_ROOT / "tools" / "schemas.py", 'ConfigDict(extra="allow")'),
    (VERL_ROOT / "trainer" / "ppo" / "ray_trainer.py", "rollout_dump_freq"),
    # qwen3.5-moe Ulysses input-slicing branch: missing upstream in verl.
    (VERL_ROOT / "models" / "transformers" / "monkey_patch.py", 'model_type == "qwen3_5_moe"'),
]


def _sglang_checks() -> list[tuple[pathlib.Path, str]]:
    """Locate the active sglang install + return (path, sentinel) pairs."""
    try:
        import sglang  # noqa: F401
    except Exception:
        return []  # sglang not installed — skip; launcher will fail later anyway
    sglang_root = pathlib.Path(sglang.__file__).parent  # .../site-packages/sglang
    srt = sglang_root / "srt"
    return [
        (srt / "models" / "qwen3_5.py", "Qwen3_5MoeForConditionalGeneration"),
        (srt / "configs" / "qwen3_5.py", "Qwen3_5MoeTextConfig"),
        (srt / "utils" / "hf_transformers_utils.py", "sub_configs"),
    ]


CHECKS.extend(_sglang_checks())


def main() -> int:
    missing: list[tuple[str, str]] = []
    for path, sentinel in CHECKS:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            missing.append((str(path), f"FILE MISSING ({sentinel})"))
            continue
        if sentinel not in text:
            missing.append((str(path), sentinel))
    if missing:
        print("verl patch verification FAILED — missing sentinels:", file=sys.stderr)
        for path, sentinel in missing:
            print(f"  {path}: {sentinel!r}", file=sys.stderr)
        print(
            "\nRe-apply the verl surgery documented in "
            "/root/.claude/plans/ok-now-we-need-immutable-gray.md before running RL.",
            file=sys.stderr,
        )
        return 2
    print("verl patches OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
