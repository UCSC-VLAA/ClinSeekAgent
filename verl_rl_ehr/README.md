# verl_rl_ehr

Multi-turn GRPO training pipeline for Qwen3.5-35B-A3B on EHR-Bench. Sits
on top of the in-tree editable verl at `../verl/` and uses sglang 0.5.9
for rollout. OpenResearcher-style **context reset** (with optional
Claude Haiku summarization via Bedrock) keeps long trajectories from
blowing past `sglang max_model_len`.

**Full setup + run + bug-fix history:** [`../docs/multiturn_rl_training.md`](../docs/multiturn_rl_training.md)

## Layout

```
tools/              # BaseTool subclasses
  _mcp_session.py     process-global EHRToolPool accessor
  ehr_common_tool.py  factory: one wrapper per MCP tool
  ehr_finish_tool.py  verl-native answer submission (writes agent_data.extra_fields)
  browser_{search,open,find}_tool.py   stubbed when BROWSER_SEARCH_MODE=stub
  _session_state.py   per-trajectory tool-call budget tracking

interactions/       # BaseInteraction
  ehr_evaluation_interaction.py   grades final answer via openresearcher_ehr f1_score

reward/             # custom_reward_function
  ehr_reward.py     F1 + tool_engagement + efficiency − format_penalty

config/             # Hydra configs
  ehr_multiturn_grpo_qwen35.yaml       top-level (inherits ppo_trainer)
  tool_config/ehr_tool_config.yaml     11 tools (3 browser + 7 EHR + finish)
  interaction_config/ehr_interaction_config.yaml

preprocess/
  build_ehr_rl_parquet.py   MIMIC-IV-Bench JSONs → data/ehr_rl_qwen35/{train,val}.parquet

patches/
  verify_verl_patches.py     preflight sentinel-check (run every launch)
  apply_sglang_patches.sh    upstream OpenResearcher Qwen3.5-MoE patches for sglang

scripts/
  smoke_test_1step.sh        4 GPUs, 1 step, ~150 s
  run_grpo_qwen35_ehr.sh     full 8-GPU launcher
  convert_sft_ckpt_to_hf.sh  rarely needed — HF weights already saved beside Megatron dist_ckpt
```

## Quickstart

```bash
# From $REPO = /fsx-shared/juncheng/EHR
source venvs/qwen3_5_rl/bin/activate

# Preflight
python verl_rl_ehr/patches/verify_verl_patches.py

# Start MCP (once):
bash scripts/run/run_mcp_server.sh 0 5103 &

# Smoke:
bash verl_rl_ehr/scripts/smoke_test_1step.sh

# Full run:
bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh
```

## Status as of 2026-04-27

1-step GRPO smoke passed end-to-end on 4× H200 in 150 s/step with the
full pipeline: agent loop + 9-turn EHR tool use + sglang weight-sync +
FSDP actor update. See `docs/multiturn_rl_training.md` §9 for the
baseline metrics and §10 for known open items (e.g. the SFT model
doesn't yet call `ehr.finish`, so every rollout hits the format
penalty floor at −0.2).
