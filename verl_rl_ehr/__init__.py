"""verl_rl_ehr — EHR multi-turn GRPO scaffolding on top of verl.

Sibling package to verl/. Provides:
  - tools/:        BaseTool subclasses wrapping the EHR MCP server + browser tools
  - interactions/: BaseInteraction subclass that grades the final answer
  - reward/:       compute_score for verl's custom_reward_function hook
  - config/:       hydra yamls (tool config, interaction config, GRPO recipe)
  - preprocess/:   builder for data/ehr_rl_qwen35/{train,val}.parquet
  - scripts/:      launcher + ckpt-conversion + smoke-test shell scripts

Depends on three in-tree edits to verl (enforced by patches/verify_verl_patches.py).
"""
