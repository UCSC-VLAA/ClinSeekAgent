# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AgentEHR is a benchmark for evaluating autonomous clinical decision-making agents on Electronic Health Records (EHR). The project implements RETROSUM, a framework combining retrospective summarization with evolving experience strategies for long-context clinical reasoning tasks.

## Core Architecture

### Agent System
- **Agent Hierarchy**: All agents inherit from `ABCAgent` → `BaseAgent` → specialized MCP agents
- **Agent Types** (defined in `src/agentlite/agents/__init__.py`):
  - `mcp`: Base MCP agent (ReAct-style)
  - `mcp_resum`: ReSUM agent with recursive summarization
  - `mcp_retrosum`: RetroSUM agent with retrospective summarization + evolving memory
  - `mcp_reflectool`: ReflecTool agent with action search
  - `mcp_reasoningbank`: ReasoningBank agent with experience memory
  - `mcp_reflexion`: Reflexion agent with self-reflection

### MCP (Model Context Protocol) Server
- **Location**: `src/agentlite/mcp_tools/`
- **Purpose**: Provides standardized tool interface for EHR database access
- **Tool Categories**:
  - `candidate_tools.py`: Candidate answer retrieval tools
  - `record_tools.py`: Patient record retrieval tools
  - `table_tools.py`: Database table exploration tools
  - `knowledge_tools.py`: Medical knowledge tools
  - `resource_tools.py`: Resource and metadata tools
- **Dynamic Tool Building**: `mcp_builder.py` allows LLM-generated tool creation with auto-install dependencies

### Data Flow
1. **EHR Database**: SQLite databases in `data/AgentEHR-Bench/MIMICIVAgentBench/` containing patient records
2. **Task Metadata**: JSON files with subject_id, prediction_time, task type, and ground truth labels
3. **Agent Execution**: Agent loads EHR, uses MCP tools to query data, generates predictions
4. **Evaluation**: F1 score (precision/recall) between predictions and ground truth

## Virtual Environments

Six venvs under `venvs/` (see [`venvs/README.md`](venvs/README.md) for details).
Use the right one for the task:

| venv | Python | Use for | Key pins |
|---|---|---|---|
| `venvs/qwen3_5_sft` → `openresearcher_ehr/.venv/` | 3.10 | SFT of Qwen3.5 via Megatron + vLLM; Bedrock eval harness (`openresearcher_ehr/deploy_agent.py`) | torch 2.9.0 · transformers 5.3.0 · vllm 0.12.0 |
| `venvs/qwen3_5_rl` (own tree) | 3.10 | **Multi-turn GRPO** (`verl_rl_ehr/`); sglang rollout. Do NOT share with SFT — transformers is 5.5.3 here | torch 2.9.0 · transformers 5.5.3 · vllm 0.13.0 · sglang 0.5.9 · flash-attn 2.8.3 |
| `venvs/gemma4` | 3.10 | Gemma-4-26B SFT (FSDP + LoRA). Has its own `gemma4` model-type support | torch 2.9.0 · transformers 5.5.4 |
| `venvs/bedrock_agent` | 3.12 | Bedrock Claude eval runs (no local model) | torch 2.11.0 · transformers 5.6.0 |
| `venvs/mcp_ehr` | 3.12 | Runs the EHR MCP server only (`src/run_mcp_server.py`) | fastmcp + transformers 5.6.0 |
| `venvs/mcp_image` → `src/mcp_image/.venv/` | 3.12 | MM image MCP server for EHRXQA | fastmcp only |

Critical rule: **do not install anything into `venvs/qwen3_5_sft`**
unless you also verify SFT still works. It's the only venv with the
SFT-tested pins, and it's a symlink to `openresearcher_ehr/.venv/`
used by the live eval harness. RL-specific additions go in `venvs/qwen3_5_rl`.

## Commands

### Development Environment
```bash
# Install dependencies
pip install -r requirements.txt
pip install -U vllm
```

### Running the System

#### 1. Start vLLM Server (for LLM inference)
```bash
# Start on GPU 0, port 4000
bash ./scripts/run/run_vllm_server.sh 0

# Or manually:
CUDA_VISIBLE_DEVICES=0 vllm serve <model_path> \
    --port 4000 \
    --dtype auto \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --reasoning-parser qwen3 \
    --gpu-memory-utilization 0.8 \
    --max-model-len 64000
```

#### 2. Start MCP Server (for EHR tools)
```bash
# Start on GPU 0, port 5002
bash ./scripts/run/run_mcp_server.sh 0

# Or manually:
CUDA_VISIBLE_DEVICES=0 python src/run_mcp_server.py \
    --mode "http" \
    --host 127.0.0.1 \
    --port 5002 \
    --data_path "../data/AgentEHR-Bench/MIMICIVAgentBench"
```

#### 3. Run Agent Evaluation
```bash
# Run a specific agent method (e.g., ReAct)
bash ./scripts/method_run/qwen3_30b_moe_react.sh

# Or manually run test.py:
python src/test.py \
    --data_path "./data/AgentEHR-Bench/MIMICIVAgentBench/common/diagnoses_ccs_500.json" \
    --output_path "../results" \
    --model_name_or_path "qwen3_30b_moe" \
    --vllm_server_url "http://127.0.0.1:4000" \
    --mcp_url "http://127.0.0.1:5000/mcp" \
    --ehr_path "./data/AgentEHR-Bench/MIMICIVAgentBench" \
    --temperature 0.7 \
    --top_p 0.8 \
    --agent_type mcp_retrosum \
    --exp_name test_run \
    --task "diagnoses_ccs" \
    --max_exec_steps 100 \
    --num_runs 1 \
    --sum_per_step 10
```

### Data Preprocessing

#### Generate Task Metadata
```bash
# Generate metadata for a task
bash ./scripts/data_preprocess/meta_data_generation.sh

# Or manually:
python data_preprocess/meta_sql_data_generation.py \
    --data_path <MIMIC_IV_PATH> \
    --task diagnoses_ccs \
    --sample_num 500 \
    --output_path "./data/AgentEHR-Bench/MIMICIVAgentBench"
```

#### Generate Patient Databases
```bash
# Generate individual patient .db files
bash ./scripts/data_preprocess/generate_patient_db.sh

# Or manually:
python data_preprocess/patient_event2db.py \
    --data_path <MIMIC_IV_PATH> \
    --meta_file "./data/AgentEHR-Bench/MIMICIVAgentBench/common/diagnoses_ccs_500.json" \
    --output_path "./data/AgentEHR-Bench/MIMICIVAgentBench/common/patient_db"
```

#### Label-wise Sampling
```bash
# Sample data balanced by label distribution
bash ./scripts/data_preprocess/label_wise_sample.sh
```

### Training (for ReflecTool Agent)
```bash
# Train with action optimizer
bash ./scripts/train/train_reflectoolagent.sh

# Or manually:
python src/agentlite/train/optimization_mcp.py \
    --data_path "./data/AgentEHR-Bench/MIMICIVAgentBench/train/mix_600.json" \
    --output_path "./ckpt" \
    --model_name_or_path "qwen3_30b_moe" \
    --vllm_server_url "http://127.0.0.1:8000" \
    --mcp_url "http://127.0.0.1:9000/mcp" \
    --agent_type "mcp_reflectool" \
    --batch 4 \
    --max_step 100
```

### Multi-turn GRPO (Qwen3.5-35B-A3B)

Separate pipeline from the agentlite training above. Lives in `verl_rl_ehr/`
and uses the in-tree editable verl at `verl/` with sglang 0.5.9 rollout.
Full setup + bug-fix history in [`docs/multiturn_rl_training.md`](docs/multiturn_rl_training.md).

```bash
# One-time preflight (installed packages + in-tree verl surgery + sglang patch):
python verl_rl_ehr/patches/verify_verl_patches.py    # sentinel checks
bash   verl_rl_ehr/patches/apply_sglang_patches.sh   # idempotent

# Preprocess benchmark into parquet (~3000 train rows, 6 tasks):
python verl_rl_ehr/preprocess/build_ehr_rl_parquet.py

# Run MCP EHR server on :5103, then smoke test (4 GPUs, 1 step, ~150 s):
bash scripts/run/run_mcp_server.sh 0 5103 &
bash verl_rl_ehr/scripts/smoke_test_1step.sh

# Full run (8 GPUs):
bash verl_rl_ehr/scripts/run_grpo_qwen35_ehr.sh
```

Venv: `venvs/qwen3_5_rl/` (kept separate from `venvs/qwen3_5_sft/`;
transformers 5.5.3 + sglang 0.5.9 + torch 2.9.0 pins). Context management
is OpenResearcher-style **reset** (not summarization by the policy model);
optional Claude Haiku summarization via Bedrock when
`context_reset_summarizer_enabled=true`.

## Key Implementation Details

### Agent Execution Loop
1. **Load EHR**: Agent loads patient database with `load_ehr()` at specified timestamp
2. **Action Generation**: Agent generates next action using prompt + LLM + tool schemas
3. **Tool Execution**: Action forwarded to MCP server for execution
4. **Memory Update**: Action-observation pair stored in short-term memory
5. **Summarization** (for ReSUM/RetroSUM): Every `sum_per_step` rounds, agent summarizes context
6. **Termination**: Loop ends when agent calls `Finish` action or reaches `max_exec_steps`

### Summarization Strategies
- **ReSUM** (`sum_per_step=10`): Recursively summarizes patient state every N steps
- **RetroSUM** (`retrospect_context="both"`): Reviews entire trajectory to capture correlations
- **Memory Injection** (`enable_memory_injection=True`): Retrieves relevant past experiences

### Database Schema
- Patient data stored in individual SQLite `.db` files (one per subject_id)
- Main tables: `admissions`, `diagnoses_icd`, `procedures_icd`, `labevents`, `prescriptions`, `microbiologyevents`, `transfers`
- Temporal queries: All queries respect the `prediction_time` constraint (no future data leakage)

### Prompt Generation
- Located in `src/agentlite/agent_prompts/`
- Each agent type has specialized prompt generator (e.g., `ReSumPrompt.py`, `RetroSumEvolvingPrompt.py`)
- Prompts include: instruction, role, tool descriptions, action history, task description

## Testing and Debugging

### Enable Debug Logging
Set `debug=True` in `DataConfig` when running `test.py` to save detailed prompts to `agent.log`.

### Resume from Checkpoint
Use `--resume True` to continue evaluation from last saved result (useful for long-running experiments).

### Common Issues
- **Connection refused**: Ensure vLLM and MCP servers are running before executing agents
- **Database not found**: Verify `--ehr_path` points to correct `EHRAgentBench` directory
- **OOM errors**: Reduce `--max_exec_steps` or `--gpu_memory_utilization` in vLLM server
- **Tool call parsing errors**: Check LLM supports tool calling (use `--enable-auto-tool-choice` for vLLM)

## File Structure Reference

```
src/
├── agentlite/
│   ├── agents/           # Agent implementations (BaseAgent, MCPAgent, etc.)
│   ├── agent_prompts/    # Prompt templates for each agent type
│   ├── mcp_tools/        # MCP tool definitions and builder
│   ├── llm/              # LLM backend wrappers (OpenAI, vLLM)
│   ├── memory/           # Short-term memory implementations
│   ├── train/            # Training/optimization for agents
│   ├── dataloader/       # Dataset loading and result management
│   ├── commons/          # Shared utilities (TaskPackage, EHRManager, AgentAct)
│   └── logging/          # Logging infrastructure
├── test.py               # Main evaluation script
└── run_mcp_server.py     # MCP server startup script

data_preprocess/          # Scripts for generating task metadata and patient DBs
scripts/
├── run/                  # Server startup scripts
├── method_run/           # Agent evaluation scripts for different methods
└── train/                # Training scripts
```

## Important Configuration Parameters

### Agent Parameters
- `agent_type`: Choice of agent architecture (mcp, mcp_resum, mcp_retrosum, etc.)
- `max_exec_steps`: Maximum action steps before forced termination (default: 30)
- `sum_per_step`: Summarization frequency for ReSUM/RetroSUM agents (default: 10)
- `summary_mode`: How to handle summaries - "add" or "replace" in context
- `memory_inject`: Where to inject memory - "actor", "summarizer", or "both"
- `enable_memory_injection`: Whether to use experience memory bank (for RetroSUM)

### Model Parameters
- `temperature`: Sampling temperature (0.7 for Qwen3, 0.0 for deterministic)
- `top_p`: Nucleus sampling parameter
- `max_seq_len`: Maximum context length (default: 64000 for Qwen3)
- `enable_thinking`: Enable chain-of-thought reasoning (Qwen3 only)

### Evaluation Parameters
- `num_runs`: Number of rollouts per task (for variance estimation)
- `score_strategy`: Aggregation strategy - "avg" or "max" across rollouts
- `resume`: Continue from last checkpoint
- `start_index`: Skip to specific sample index (for debugging)
