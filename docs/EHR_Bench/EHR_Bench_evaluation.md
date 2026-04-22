# Model Evaluation Guide

本文记录如何在当前仓库中完成一次完整的模型评测流程。以下命令默认在仓库根目录执行，并全部使用相对路径。

关于评测过程中 Agent 的具体运行机制，请参阅 [agent_execution.md](./agent_execution.md)。

---

## 1. 环境准备

### 1.1 数据与模型

在安装依赖和启动服务前，需要先准备评测所依赖的数据和模型。以下路径均相对于仓库根目录：

- 数据集 `https://huggingface.co/datasets/BlueZeros/AgentEHR-Bench` 需要下载到 `data/AgentEHR-Bench`
- EHR-Bench 需要准备到 `data/EHR-Bench`（包含 benchmark JSON 与病人 SQLite 数据库）。**具体准备流程请参考 [EHR_Bench_data_prepare.md](./EHR_Bench_data_prepare.md)**，文档中提供两种方式：
  - 快速准备：直接下载已打包好的数据，解压即用
  - 完整流程：从 MIMIC-IV 原始数据反向匹配并生成病人 db
- 模型按需下载到 `models/` 目录（具体路径见第 3 节的模型配置表）

AgentEHR-Bench 可使用 Hugging Face CLI 下载：

```bash
hf download --repo-type dataset BlueZeros/AgentEHR-Bench --local-dir data/AgentEHR-Bench
```

#### EHR-Bench 评测子集

`data/EHR-Bench/ehr_bench_merged_filtered.json` 是全量库（20302 条，45 个 task，分属 `risk_prediction` 和 `decision_making` 两类）。为控制评测成本，仓库提供两个按 task 分层抽样的子集：

| 文件 | 每 task 条数 | 总条数 | task_type 分布 | 说明 |
|------|------------|-------|---------------|------|
| `data/EHR-Bench/ehr_bench_sampled_20_per_task.json` | 20 | 900 | risk 360 / decision 540 | 小规模快速冒烟 |
| `data/EHR-Bench/ehr_bench_sampled_40_per_task.json` | 40 | 1800 | risk 720 / decision 1080 | **推荐用于正式评测** |

抽样方式：按 `task` 字段分桶，在每桶内用固定随机种子无放回采样。采样脚本位于 `helper/sample_per_task.py`，从仓库根目录复现命令：

```bash
python helper/sample_per_task.py                           # 默认：per-task=40, seed=42
python helper/sample_per_task.py --per-task 20 --seed 42   # 生成 900 条子集
```

可传 `--input`、`--output`、`--per-task`、`--seed` 覆盖默认值。

将评测脚本切换到 1800 子集：

```bash
DATA_PATH=../data/EHR-Bench/ehr_bench_sampled_40_per_task.json \
    bash openresearcher_ehr/eval_ehrbench.sh
```

打分时 `--benchmark` 需指向同一个文件：

```bash
python openresearcher_ehr/helper/evaluate_results.py \
    --results openresearcher_ehr/results/ehrbench_1800_<model_slug> \
    --benchmark data/EHR-Bench/ehr_bench_sampled_40_per_task.json
```

`evaluate_results.py` 会同时输出整体、每 task、以及按 `task_type`（risk_prediction / decision_making）两类的分组指标。

### 1.2 安装依赖（使用 uv）

使用 [uv](https://docs.astral.sh/uv/) 创建虚拟环境并安装依赖。如尚未安装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**前置要求：**

- Python 3.10+
- CUDA 12.x

**创建虚拟环境并安装依赖：**

```bash
uv venv venv/gemma --python 3.12
uv pip install -r docs/EHR_Bench/requirements.txt --python venv/gemma/bin/python
uv pip install -r docs/EHR_Bench/requirements_openresearcher_ehr.txt --python venv/gemma/bin/python
uv pip install "vllm>=0.19.0" "transformers>=5.5" --python venv/gemma/bin/python
```

> **注意：** `vllm>=0.19.0` 默认会拉取 `transformers 4.x`，安装完 vllm 后需要再单独升级 transformers 到 5.x。

**关键版本要求：**

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| vllm | >= 0.19.0 | 支持 Gemma4、Qwen3.5 等新架构 |
| transformers | >= 5.5 | 支持 `gemma4` 等新 model type |

**激活环境：**

```bash
source venv/gemma/bin/activate
```

确保后续执行脚本时，`python`、`vllm` 和相关依赖都来自这个已激活的环境。

---

## 2. 启动 MCP 服务

MCP 服务为 Agent 提供 EHR 数据库查询工具。打开一个新的终端后运行：

```bash
bash scripts/run/run_mcp_server.sh
```

默认情况下该脚本会使用：

- `GPU_ID=0`
- `PORT=5103`
- 数据路径 `data/AgentEHR-Bench/MIMICIVAgentBench`

如果需要自定义 GPU 或端口，可以显式传参：

```bash
bash scripts/run/run_mcp_server.sh <GPU_ID> <PORT>
# 例如：
bash scripts/run/run_mcp_server.sh 0 5103
```

> **注意：** 该脚本内部使用 `--disable-knowledge-tools` 标志启动，不加载语料检索工具（评测中用 browser 工具替代）。如果修改了端口，后续评测脚本中的 `EHR_MCP_URL` 也需要同步修改。

**验证 MCP 服务是否正常：** 服务启动后应在终端看到类似 `Uvicorn running on http://127.0.0.1:5103` 的输出。

---

## 3. 启动 vLLM 服务

vLLM 服务提供 OpenAI 兼容的推理 API。根据要评测的模型选择对应的启动脚本。

### 3.1 可用模型与脚本

| 模型 | 启动脚本 | 默认端口 | 模型路径 | 备注 |
|------|---------|---------|---------|------|
| Qwen3.5-35B-A3B | `run_vllm_server_3_5.sh` | 4000 | `models/Qwen3.5-35B-A3B` | 默认评测模型 |
| OpenSeeker-v1-30B-SFT | `run_vllm_server.sh` | 4000 | `models/OpenSeeker-v1-30B-SFT` | 自动配置 chat template 和 tool parser |
| OpenResearcher-30B-A3B | `run_vllm_server_Nemotron.sh` | 4000 | `models/OpenResearcher-30B-A3B` | Nemotron 架构 |
| Meissa-4B | `run_vllm_server_Meissa_4B.sh` | 4000 | `models/Meissa-4B` | 轻量模型，单卡可运行 |
| Gemma-4-26B-A4B-it | `run_vllm_server_gemma4.sh` | 4000 | `models/gemma-4-26B-A4B-it` | |

所有脚本都位于 `scripts/run/` 目录下。

### 3.2 Qwen3.5-35B-A3B（默认模型）

```bash
bash scripts/run/run_vllm_server_3_5.sh
```

默认使用所有 8 张 GPU（`0,1,2,3,4,5,6,7`），端口 `4000`。

该脚本的关键参数：

| 参数 | 值 | 说明 |
|------|---|------|
| `--max-model-len` | 1000000 | 最大上下文长度 |
| `--tool-call-parser` | `qwen3_xml` | 工具调用解析器 |
| `--reasoning-parser` | `qwen3` | 推理内容解析器 |
| `--language-model-only` | 启用 | MoE 模型仅加载语言模型部分 |
| `--dtype` | `bfloat16` | 推理精度 |

### 3.3 OpenSeeker-v1-30B-SFT

```bash
bash scripts/run/run_vllm_server.sh
```

OpenSeeker 模型会自动配置专用的 chat template（`openresearcher_ehr/openseeker_vllm/chat_template.jinja`）和 tool parser plugin（`openresearcher_ehr/openseeker_vllm/tool_parser.py`）。

### 3.4 Gemma-4

```bash
bash scripts/run/run_vllm_server_gemma4.sh
```

### 3.5 验证 vLLM 是否就绪

服务启动需要一段时间加载模型权重。可以用以下命令确认服务已就绪：

```bash
curl -s http://127.0.0.1:4000/v1/models | python -m json.tool
```

正常输出应包含模型 ID。如果返回 `Connection refused`，说明服务尚未启动完成。

### 3.6 GPU 显存与 max_model_len 参考

| 模型 | 卡数 | max_model_len | 显存占用参考 |
|------|------|--------------|------------|
| Qwen3.5-35B-A3B | 8×A100 80GB | 1000000 | ~60GB/卡 |
| OpenSeeker-v1-30B-SFT | 8×A100 80GB | 262144 | ~60GB/卡 |
| OpenResearcher-30B-A3B | 8×A100 80GB | 262144 | ~60GB/卡 |
| Meissa-4B | 1×A100 80GB | 8192 | ~15GB |
| Gemma-4-26B-A4B-it | 8×A100 80GB | 262144 | ~60GB/卡 |

如果遇到 OOM，可通过环境变量降低显存使用：

```bash
GPU_MEMORY_UTILIZATION=0.7 bash scripts/run/run_vllm_server_3_5.sh
```

---

## 4. 启动 Evaluation

### 4.1 使用 vLLM 后端评测（默认）

打开第三个终端后运行。先切换到 `openresearcher_ehr` 目录：

```bash
cd openresearcher_ehr
bash eval_ehrbench.sh
```

该脚本会：
1. 自动从 vLLM 的 `/v1/models` 接口探测模型名称
2. 根据模型名生成输出目录（如 `results/ehrbench_1800_qwen3_5_35b_a3b/`）
3. 调用 `deploy_agent.py` 启动并发评测

默认连接：

| 配置 | 默认值 |
|------|--------|
| MCP 服务地址 | `http://127.0.0.1:5103/mcp` |
| vLLM 服务地址 | `http://127.0.0.1:4000` |
| 评测数据 | `data/EHR-Bench/ehr_bench_sampled_40_per_task.json` |
| 最大并发 | 5 |
| 最大轮次 | 200 |
| 单次最大 tokens | 32768 |
| 工具返回截断 | 100000 字符 |
| 采样温度 | 0.0 |
| Thinking 模式 | 启用 |

**自定义参数：** 通过环境变量覆盖默认值：

```bash
# 指定不同的 vLLM 地址和端口
VLLM_BASE_URL=http://127.0.0.1:4001 bash eval_ehrbench.sh

# 调整并发数和温度
MAX_CONCURRENCY=10 TEMPERATURE=0.6 bash eval_ehrbench.sh

# 使用不同的数据集（例如切换到 900 条小规模子集）
DATA_PATH=../data/EHR-Bench/ehr_bench_sampled_20_per_task.json bash eval_ehrbench.sh

# 关闭 thinking 模式
ENABLE_THINKING=0 bash eval_ehrbench.sh

# 指定输出目录
OUTPUT_DIR=./results/my_experiment bash eval_ehrbench.sh
```

完整环境变量列表：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `VLLM_BASE_URL` | `http://127.0.0.1:4000` | vLLM 服务地址 |
| `VLLM_MODEL_NAME` | `auto`（自动探测） | 模型名称，设为 auto 会自动获取 |
| `VLLM_API_KEY` | `EMPTY` | API Key |
| `EHR_MCP_URL` | `http://127.0.0.1:5103/mcp` | MCP 服务地址 |
| `DATA_PATH` | `../data/EHR-Bench/ehr_bench_sampled_40_per_task.json` | 评测数据路径 |
| `OUTPUT_DIR` | `./results/ehrbench_1800_<model_slug>` | 输出目录 |
| `MAX_CONCURRENCY` | `5` | 最大并发任务数 |
| `MAX_ROUNDS` | `200` | 单任务最大对话轮次 |
| `MAX_TOOL_RESULT_CHARS` | `100000` | 工具返回内容截断字符数 |
| `MAX_TOKENS` | `32768` | 单次调用最大生成 tokens |
| `RUNS_PER_QUESTION` | `1` | 每个问题独立运行几次 |
| `ENABLE_THINKING` | `1` | 是否启用 thinking 模式（1/0） |
| `TEMPERATURE` | `0.0` | 采样温度 |

### 4.2 使用 Bedrock 后端评测（Claude 模型）

如果需要使用 AWS Bedrock 上的 Claude 模型而非本地 vLLM，使用 `run.sh`：

```bash
cd openresearcher_ehr
bash run.sh
```

该脚本需要配置 Bedrock 凭证。可通过环境变量传入：

```bash
BEDROCK_API_KEY=<your_token> bash run.sh
```

或提前设置 AWS 凭证链（IAM Role、环境变量等），boto3 会自动使用。

`run.sh` 的默认配置：

| 配置 | 默认值 |
|------|--------|
| 模型 | `us.anthropic.claude-opus-4-6-v1` |
| Region | `us-east-1` |
| 并发数 | 12 |
| 每题运行次数 | 4 |
| Thinking | 关闭 |

### 4.3 评测输出

评测结果会输出到指定的输出目录中，包含：

```
results/subset_500_qwen3_5_35b_a3b/
├── results.jsonl              # 主结果文件（每行一条任务的完整记录）
└── run_test_subset_*.log      # 运行日志
```

`results.jsonl` 中每一行是一个 JSON 对象，包含：
- `qid`: 任务 ID（如 `diagnoses_ccs_10000032`）
- `messages`: 完整的对话历史
- `completed`: 是否成功完成
- `stop_reason`: 终止原因（`finish_tool_call` / `no_final_answer`）
- `subject_id`, `task`, `ground_truth` 等原始数据字段

评测过程中，每完成一条任务就会即时写入 `results.jsonl`。如果评测意外中断，已完成的结果不会丢失。

> **注意：** 目前 `run_test_subset.sh` 每次运行会覆盖 `results.jsonl`（`open(..., 'w')`）。如需保留历史结果，请在重新运行前备份输出目录或修改 `OUTPUT_DIR`。

---

## 5. 评测打分

评测结束后，使用 `openresearcher_ehr/helper/evaluate_results.py` 对结果进行打分和 tool call 统计。该脚本会：

1. 从 `results.jsonl` 解析每条任务中 `ehr.finish` 工具调用的参数作为预测
2. 与 benchmark 文件中的 `ground_truth` 做集合匹配（大小写不敏感）
3. 输出每个 task 的 Precision / Recall / F1、平均 tool call 次数以及 browser tool 占比

```bash
# --results 可以传 results.jsonl 文件路径，也可以传其所在目录（自动检测 results.jsonl）
python openresearcher_ehr/helper/evaluate_results.py \
    --results openresearcher_ehr/results/<exp_name> \
    --benchmark data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json
```

可选参数：

| 参数 | 说明 |
|------|------|
| `--extract-text-answer-without-finish` | 当模型未调用 `ehr.finish` 时，尝试从最后一条 assistant 消息中提取答案 |
| `--output scores.json` | 将详细结果写入 JSON 文件 |

> **注意：** `--benchmark` 应与评测时使用的 `DATA_PATH` 一致，否则 qid 无法匹配。

输出示例：

```
Task                   Total  Done  Runs    Prec     Rec      F1  ToolAvg  Brows%
--------------------------------------------------------------------------------
diagnoses_ccs            500   500   500  0.4391  0.6371  0.4773     43.5    8.9%
labevents                500   500   500  0.3311  0.7737  0.4407     83.0    9.6%
...
--------------------------------------------------------------------------------
Overall                 3000  3000  3000  0.2423  0.6447  0.3192     54.8    9.6%
```

各列含义：

| 列 | 说明 |
|------|------|
| Total | benchmark 中该 task 的样本总数 |
| Done | results 中有结果的样本数 |
| Runs | 总运行次数（Done × runs_per_question） |
| Prec | 平均 Precision |
| Rec | 平均 Recall |
| F1 | 平均 F1 Score |
| ToolAvg | 每次运行的平均 tool call 次数 |
| Brows% | tool call 中 browser 工具的占比 |

---

## 6. 常见问题排查

### MCP 服务连接失败

**现象：** 评测日志中出现 `Error calling MCP tool` 或 `Connection refused`。

**排查：**
1. 确认 MCP 服务终端有正常运行的日志输出
2. 确认端口一致：`run_mcp_server.sh` 的端口（默认 5103）应与 `run_test_subset.sh` 中 `EHR_MCP_URL` 的端口一致
3. 检查是否有端口冲突：`lsof -i :5103`

### vLLM 服务连接失败

**现象：** 评测日志中出现 `No served models reported by vLLM` 或 `Connection refused`。

**排查：**
1. vLLM 启动需要较长时间加载模型，等待直到终端输出 `Uvicorn running on ...`
2. 用 `curl http://127.0.0.1:4000/v1/models` 验证服务是否就绪
3. 确认 `VLLM_BASE_URL` 端口与 vLLM 启动端口一致

### OOM（显存不足）

**排查：**
1. 降低 `GPU_MEMORY_UTILIZATION`（如 `0.7`）
2. 减小 `MAX_MODEL_LEN`
3. 增加 tensor parallel 卡数

### 评测中途中断后恢复

目前 `run_test_subset.sh` 不支持断点续跑——每次启动会覆盖 `results.jsonl`。中断后需要：

1. 将已有的 `results.jsonl` 备份或移走
2. 修改 `DATA_PATH` 仅包含剩余任务，或重新运行全部任务

### 打分时 qid 匹配不上

`evaluate_results.py` 通过 `{task}_{subject_id}` 格式的 qid 将预测与 ground truth 匹配。确保 `--benchmark` 参数指向与评测时 `DATA_PATH` 相同的数据文件。

---

## 7. 推荐执行顺序

按下面顺序依次执行：

1. 使用 `uv` 和 `requirements.txt` 搭建环境（见 1.2）
2. 下载数据集和模型到指定路径
3. 启动 MCP 服务：`bash scripts/run/run_mcp_server.sh`
4. 启动 vLLM 服务：`bash scripts/run/run_vllm_server_3_5.sh`（或其他模型对应脚本）
5. **等待两个服务都完全启动后**，运行评测：`cd openresearcher_ehr && bash run_test_subset.sh`
6. 评测完成后打分：`python openresearcher_ehr/helper/evaluate_results.py --results <结果目录> --benchmark <数据文件>`

如果使用 Bedrock 后端（Claude），跳过第 4 步，第 5 步改用 `bash run.sh`。
