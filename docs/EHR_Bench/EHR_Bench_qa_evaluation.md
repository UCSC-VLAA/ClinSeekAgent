# EHR-Bench QA-Mode Evaluation Guide

本文档记录 **single-turn QA 模式** 下对 EHR-Bench 的评测流程。

与 [EHR_Bench_evaluation.md](./EHR_Bench_evaluation.md) 中的 agentic 模式不同，QA 模式**不使用 MCP、不调用工具、不做 ReAct 多轮推理**——只把 benchmark 每条记录的 `question` 字段原样送进 vLLM 的 chat completion 接口，让模型一次性给出答案。

两种模式的核心区别：

| 维度 | Agentic (`eval_ehrbench.sh`) | QA (`eval_ehrbench_qa.sh`) |
|------|-----------------------------|---------------------------|
| 是否需要 MCP 服务 | 需要 (`run_mcp_server.sh`) | **不需要** |
| 是否允许 DB 查询 / browser | 允许 | 不允许 |
| 交互轮数 | 多轮（默认最多 200） | 单轮（1 条 user → 1 条 assistant） |
| 输入 | 任务简述 + 工具描述 | benchmark 里的完整 `question`（已内嵌患者 timeline） |
| 适用场景 | 模型需要按需检索 EHR 的完整能力评估 | 纯推理/理解能力评估，排除工具使用的影响 |

好在 `question` 字段本身就已经包含 `<task_instruction>` + `<patient_timeline>` + `<candidate_answers>`，所以 QA 模式可以直接把 prompt 一次性喂给模型。

评测结果会被写成与 agentic 模式**完全相同**的 `results.jsonl` 格式（把模型的文本答案伪装成一次 `ehr.finish` 工具调用），因此可以直接沿用 `helper/evaluate_results.py` 打分，不需要改打分脚本。

---

## 1. 环境准备

### 1.1 数据

只需要 EHR-Bench 的 benchmark JSON，**不需要**病人的 SQLite 数据库（因为不查库）。

参考 [EHR_Bench_data_prepare.md](./EHR_Bench_data_prepare.md) 下载或生成 `data/EHR-Bench/` 下的数据文件。QA 模式的**正式评测数据集就是 1800 条的 `ehr_bench_sampled_40_per_task.json`**，即每个 task 分层抽样 40 条、共 45 个 task：

| 文件 | 条数 | task_type 分布 | 用途 |
|------|------|---------------|------|
| `data/EHR-Bench/ehr_bench_sampled_20_per_task.json` | 900 | risk 360 / decision 540 | 仅用于冒烟测试、调参 |
| `data/EHR-Bench/ehr_bench_sampled_40_per_task.json` | 1800 | risk 720 / decision 1080 | **QA 模式默认 / 正式全量评测** |

### 1.2 依赖

QA 脚本只依赖 `openai` Python SDK（用于访问 vLLM 的 OpenAI 兼容接口），不依赖 MCP client、fastmcp 等。直接复用仓库已配好的 gemma venv 即可：

```bash
source venv/gemma/bin/activate
# 确认 openai SDK 可用
python -c "import openai; print(openai.__version__)"
```

如需从零搭建环境，参考 [EHR_Bench_evaluation.md §1.2](./EHR_Bench_evaluation.md#12-安装依赖使用-uv)。QA 模式**不**需要安装 MCP 相关依赖。

---

## 2. 启动 vLLM 服务（必需）

QA 模式**只需要 vLLM 服务**，不需要 MCP 服务。

### 2.1 选择模型与启动脚本

所有 vLLM 启动脚本位于 `scripts/run/`。常用的几个：

| 模型 | 启动脚本 | 默认端口 | 模型路径 | 备注 |
|------|---------|---------|---------|------|
| Qwen3.5-35B-A3B | `run_vllm_server_3_5.sh` | 4000 | `models/Qwen3.5-35B-A3B` | 默认评测模型 |
| OpenSeeker-v1-30B-SFT | `run_vllm_server.sh` | 4000 | `models/OpenSeeker-v1-30B-SFT` | 自动配置 chat template |
| OpenResearcher-30B-A3B | `run_vllm_server_Nemotron.sh` | 4000 | `models/OpenResearcher-30B-A3B` | Nemotron 架构 |
| Meissa-4B | `run_vllm_server_Meissa_4B.sh` | 4000 | `models/Meissa-4B` | 轻量，单卡可跑 |
| Gemma-4-26B-A4B-it | `run_vllm_server_gemma4.sh` | 4000 | `models/gemma-4-26B-A4B-it` | |
| Tongyi-DeepResearch-30B-A3B | `run_vllm_server_deepresearch.sh` | 4000 | `models/Tongyi-DeepResearch-30B-A3B` | Qwen3MoE 架构，max_pos_embed=131072 |

### 2.2 默认模型：Qwen3.5-35B-A3B

```bash
bash scripts/run/run_vllm_server_3_5.sh
```

默认使用 8 张 GPU (`0-7`)，监听 `http://127.0.0.1:4000`。关键参数：

| 参数 | 值 | 说明 |
|------|---|------|
| `--max-model-len` | 1000000 | 最大上下文长度 |
| `--tool-call-parser` | `qwen3_xml` | QA 模式下不调用工具，但 parser 依然保留 |
| `--reasoning-parser` | `qwen3` | 用于 thinking 模式下剥离 `<think>...</think>` |
| `--dtype` | `bfloat16` | 推理精度 |

### 2.3 其他模型

```bash
# OpenSeeker
bash scripts/run/run_vllm_server.sh

# Gemma-4
bash scripts/run/run_vllm_server_gemma4.sh

# Meissa-4B（单卡）
bash scripts/run/run_vllm_server_Meissa_4B.sh
```

### 2.4 GPU 显存 / OOM 应对

参考 [EHR_Bench_evaluation.md §3.6](./EHR_Bench_evaluation.md#36-gpu-显存与-max_model_len-参考)。遇到 OOM 时：

```bash
GPU_MEMORY_UTILIZATION=0.7 bash scripts/run/run_vllm_server_3_5.sh
```

### 2.5 确认 vLLM 就绪

服务加载模型权重需要数十秒至几分钟，终端出现 `Uvicorn running on http://127.0.0.1:4000` 后才算就绪。用以下命令验证：

```bash
curl -s http://127.0.0.1:4000/v1/models | python -m json.tool
```

正常输出应包含模型 ID。如果返回 `Connection refused`，说明服务尚未启动完成。

---

## 3. 运行 QA 评测

### 3.1 全量评测（1800 条）—— 推荐

不带任何参数直接跑就是**全量评测**（`ehr_bench_sampled_40_per_task.json`, 1800 条）：

```bash
cd openresearcher_ehr
bash eval_ehrbench_qa.sh
```

该脚本会：

1. 自动从 `http://127.0.0.1:4000/v1/models` 探测 served model
2. 根据模型名 + 数据文件名拼出输出目录（如 `results/qa_ehr_bench_sampled_40_per_task_qwen3_5_35b_a3b/`）
3. 并发地把每条 `question` 送进 vLLM，收集答案，每完成一条就 append 到 `results.jsonl`

**并发推荐值**：Qwen3.5-35B-A3B 在 8×A100 上实测 `MAX_CONCURRENCY=16` 比较稳（吞吐高、又不触发 vLLM 排队超时）；4B 级小模型可以开到 32 或更高。

```bash
# 全量评测 + 提升并发（最常用）
MAX_CONCURRENCY=16 bash eval_ehrbench_qa.sh
```

### 3.2 默认配置

| 配置 | 默认值 |
|------|--------|
| vLLM 服务地址 | `http://127.0.0.1:4000` |
| 评测数据 | `../data/EHR-Bench/ehr_bench_sampled_40_per_task.json` (1800 条) |
| 最大并发 | 8 |
| 单次最大 tokens | 32768 |
| 采样温度 | 0.0 |
| Thinking 模式 | 启用 |
| 失败重试次数 | 2 |

### 3.3 环境变量覆盖

完整可覆盖的环境变量：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `VLLM_BASE_URL` | `http://127.0.0.1:4000` | vLLM 服务地址 |
| `VLLM_MODEL_NAME` | `auto` | 模型 ID，`auto` 会自动探测 |
| `VLLM_API_KEY` | `EMPTY` | API Key |
| `DATA_PATH` | `../data/EHR-Bench/ehr_bench_sampled_40_per_task.json` | benchmark JSON 路径 |
| `OUTPUT_DIR` | `./results/qa_<data_slug>_<model_slug>` | 输出目录 |
| `MAX_CONCURRENCY` | `8` | 最大并发请求数 |
| `MAX_TOKENS` | `32768` | 单次最大生成 tokens |
| `MAX_RETRIES` | `2` | vLLM 调用失败重试次数 |
| `RETRY_SLEEP` | `2.0` | 重试间隔（秒） |
| `TEMPERATURE` | `0.0` | 采样温度 |
| `ENABLE_THINKING` | `1` | 是否启用 thinking 模式（1/0） |
| `LIMIT` | （空）| 只跑前 N 条（冒烟测试用） |
| `START_INDEX` | `0` | 跳过前 K 条（断点续跑用） |

### 3.4 常用示例

```bash
# 全量正式评测（默认就是 1800 条，thinking 开，温度 0）
bash eval_ehrbench_qa.sh

# 全量 + 高并发（8×A100 上的推荐配置）
MAX_CONCURRENCY=16 bash eval_ehrbench_qa.sh

# 冒烟测试：前 2 条，关闭 thinking（仅验证 pipeline，不做科研对比）
LIMIT=2 ENABLE_THINKING=0 MAX_TOKENS=2048 \
    OUTPUT_DIR=./results/qa_smoke \
    bash eval_ehrbench_qa.sh

# 降级到 900 条小子集（仅用于快速调参，不作为正式结果）
DATA_PATH=../data/EHR-Bench/ehr_bench_sampled_20_per_task.json \
    bash eval_ehrbench_qa.sh

# 指定端口 / 并发
VLLM_BASE_URL=http://127.0.0.1:4001 MAX_CONCURRENCY=16 \
    bash eval_ehrbench_qa.sh

# 关闭 thinking 模式
ENABLE_THINKING=0 bash eval_ehrbench_qa.sh

# 指定自定义输出目录
OUTPUT_DIR=./results/my_qa_run bash eval_ehrbench_qa.sh

# 跳过已完成部分（手动断点续跑）：先备份 results.jsonl，再用 START_INDEX 跳过
START_INDEX=500 OUTPUT_DIR=./results/qa_part2 bash eval_ehrbench_qa.sh
```

### 3.5 直接用 Python（调试 / 定制）

如果不想走 shell 包装，也可直接调 `eval_ehrbench_qa.py`：

```bash
cd openresearcher_ehr
python eval_ehrbench_qa.py \
    --data_path ../data/EHR-Bench/ehr_bench_sampled_20_per_task.json \
    --output_path ./results/qa_debug/results.jsonl \
    --vllm_base_url http://127.0.0.1:4000 \
    --model_name auto \
    --temperature 0.0 \
    --max_tokens 2048 \
    --max_concurrency 4 \
    --limit 5 \
    --disable_thinking
```

---

## 4. Pipeline 内部机制

### 4.1 整体流程

```
benchmark.json
      │
      ▼
┌─────────────────────────────┐
│  load_benchmark()           │  读入 list[item]
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  build_prompt(item)         │  question + ANSWER_FORMAT_INSTRUCTION
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  vLLM /v1/chat/completions  │  单轮，无 tools
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  parse_answer(raw, cands)   │  从文本抽 predictions
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  build_result_record()      │  合成 ehr.finish tool_call
└─────────────────────────────┘
      │
      ▼
  results.jsonl （每行一条）
```

### 4.2 Prompt 构造

每条样本的 prompt = 原 `question` 字段 + 统一追加的 `ANSWER_FORMAT_INSTRUCTION`：

```
<原 question，含 task_instruction + patient_timeline + candidate_answers>

Your response MUST end with a single line in the exact format:
Final Answer: ['answer1', 'answer2', ...]
where the list contains one or more items selected verbatim from the
<candidate_answers> list above. Do not invent answers outside that list.
```

这里关键是**显式给出带方括号 + 引号的样例**（`['answer1', 'answer2', ...]`），否则模型会把 `[YOUR ANSWER HERE]` 当 placeholder，倾向于输出 `Final Answer: no`（不带括号）。

### 4.3 答案提取（`parse_answer`）

按优先级顺序：

| 优先级 | `parse_mode` | 规则 |
|--------|--------------|------|
| 1 | `final_answer_list` | 正则扫描 `Final Answer: [...]`，取**最后一个**能解析的列表（先试 `json.loads`，失败退回 `ast.literal_eval`）。支持 `['a','b']` 和 `["a","b"]` 两种引号。 |
| 2 | `json_list` | 整段去掉 `<think>` 后本身就是 `[...]`，尝试解析 |
| 3 | `candidate_match` | 把模型回答小写化，对每个候选词做 `find()`，按出现位置排序去重返回 |
| 4 | `single_line` | 取第一行，去掉末尾 `.` |
| 5 | `raw` | 兜底：原文当作预测 |

Thinking 模式下，`<think>...</think>` 块会先被 `strip_think_blocks()` 剥掉，所以 thinking 过程中的伪 final answer 不会干扰解析。

### 4.4 输出格式兼容 agentic 打分

`build_result_record` 把 predictions 列表**伪装**成一次 `ehr.finish` 工具调用：

```json
{
  "qid": "...",
  "subject_id": ...,
  "task": "...",
  "ground_truth": "...",
  "messages": [
    {"role": "user", "content": "<prompt>"},
    {
      "role": "assistant",
      "content": "<raw model output>",
      "reasoning_content": "<optional>",
      "tool_calls": [{
        "id": "call_qa_finish",
        "type": "function",
        "function": {
          "name": "ehr.finish",
          "arguments": "{\"response\": [\"xxx\", \"yyy\"]}"
        }
      }]
    }
  ],
  "completed": true,
  "stop_reason": "finish_tool_call",
  "qa_mode": true,
  "parse_mode": "final_answer_list",
  "predictions": ["xxx", "yyy"],
  "elapsed_seconds": 3.2,
  "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...},
  "error": null
}
```

这保证 `helper/evaluate_results.py` 的 `extract_finish_predictions_with_source()` 能按照它处理 agentic 结果的老路径抽到 predictions。

### 4.5 并发 & 错误处理

- 用 `asyncio.Semaphore(MAX_CONCURRENCY)` 控制同时在飞的请求数
- `ThreadPoolExecutor` 把同步的 `openai` 调用包成 async
- 单条失败自动重试 `MAX_RETRIES` 次，间隔 `RETRY_SLEEP` 秒；最终仍失败的样本会以 `completed=false, stop_reason="error"` 写入 `results.jsonl`，**不阻塞其他样本**
- 每完成一条就 `flush()` 一次，中途中断也不会丢已完成结果

---

## 5. 打分

QA 结果文件结构和 agentic 完全一致，直接复用 `helper/evaluate_results.py`。对于全量 1800 条的结果：

```bash
python openresearcher_ehr/helper/evaluate_results.py \
    --results openresearcher_ehr/results/qa_ehr_bench_sampled_40_per_task_qwen3_5_35b_a3b/results.jsonl \
    --benchmark data/EHR-Bench/ehr_bench_sampled_40_per_task.json
```

`--benchmark` 必须和 `DATA_PATH` 保持一致（都是 1800 条的文件），否则 `qid` 匹配不上。

输出分三块：每个 task 一行、Overall 一行、按 `task_type` (risk_prediction / decision_making) 分组各一行。全量表格会有 45 行 task + 1 行 Overall + 2 行 task_type。示例（格式，数值以实际评测为准）：

```
Task                   Total  Done  Runs    Prec     Rec      F1  ToolAvg  Brows%  TurnAvg
-----------------------------------------------------------------------------------------
ED_Critical_Outcomes      40    40    40    xx.x    xx.x    xx.x      1.0    0.0%      1.0
ED_Hospitalization        40    40    40    xx.x    xx.x    xx.x      1.0    0.0%      1.0
...(共 45 行 task)...
admissions                40    40    40    xx.x    xx.x    xx.x      1.0    0.0%      1.0
-----------------------------------------------------------------------------------------
Overall                 1800  1800  1800    xx.x    xx.x    xx.x      1.0    0.0%      1.0
-----------------------------------------------------------------------------------------
By task_type:
decision_making         1080  1080  1080    xx.x    xx.x    xx.x      1.0    0.0%      1.0
risk_prediction          720   720   720    xx.x    xx.x    xx.x      1.0    0.0%      1.0
```

各列含义参考 [EHR_Bench_evaluation.md §5](./EHR_Bench_evaluation.md#5-评测打分)。

> 注意：QA 模式下每条样本恰好 1 次 assistant 轮次、1 次（合成的）tool call，因此 `ToolAvg` 与 `TurnAvg` 恒为 1.0、`Brows%` 恒为 0.0——对比不同 agent 时应关注 F1。

### 可选参数

`evaluate_results.py` 的几个有用选项：

| 参数 | 说明 |
|------|------|
| `--extract-text-answer-without-finish` | QA 模式始终有合成的 `ehr.finish`，通常用不到 |
| `--output scores.json` | 写出 per-task / per-task_type 的 JSON 详情 |

---

## 6. 推荐执行顺序（全量评测）

1. 准备数据（见 §1.1）——确认 `data/EHR-Bench/ehr_bench_sampled_40_per_task.json` 存在
2. 激活 venv：`source venv/gemma/bin/activate`
3. 启动 vLLM（占一个终端）：`bash scripts/run/run_vllm_server_3_5.sh`
4. 等 vLLM 就绪：`curl -s http://127.0.0.1:4000/v1/models | python -m json.tool` 返回 model id
5. （可选）先做 2 条冒烟测试，确认 pipeline 通：
   ```bash
   cd openresearcher_ehr
   LIMIT=2 ENABLE_THINKING=0 MAX_TOKENS=2048 \
       OUTPUT_DIR=./results/qa_smoke bash eval_ehrbench_qa.sh
   ```
6. **正式全量评测**（1800 条，thinking 开，温度 0）：
   ```bash
   cd openresearcher_ehr
   MAX_CONCURRENCY=16 bash eval_ehrbench_qa.sh
   ```
   结果落在 `results/qa_ehr_bench_sampled_40_per_task_<model_slug>/results.jsonl`
7. 打分：
   ```bash
   python openresearcher_ehr/helper/evaluate_results.py \
       --results openresearcher_ehr/results/qa_ehr_bench_sampled_40_per_task_<model_slug>/results.jsonl \
       --benchmark data/EHR-Bench/ehr_bench_sampled_40_per_task.json
   ```

**时间开销参考**（Qwen3.5-35B-A3B, 8×A100, `MAX_CONCURRENCY=16`, thinking 开）：
- 全量 1800 条 ≈ 1.5–2 小时（实测吞吐 ≈ 17 条/分钟；单条平均 prompt ~7.5k tokens、completion ~4k tokens、耗时 ~50s）
- 关闭 thinking (`ENABLE_THINKING=0`)，同配置 ≈ 20–30 分钟（completion 显著变短）
- 样本间方差较大，单条可能 15s–100s+，瓶颈取决于 patient timeline 长度

---

## 7. 常见问题排查

### 7.1 `ModuleNotFoundError: No module named 'openai'`

没有激活 venv：`source venv/gemma/bin/activate` 再跑。

### 7.2 `Connection refused` 或 `No served models reported by vLLM`

vLLM 尚未就绪。等终端出现 `Uvicorn running on ...`，或用 `curl http://127.0.0.1:4000/v1/models` 验证。

### 7.3 模型没输出 `Final Answer: [...]` 而是别的格式

脚本会按 `final_answer_list` → `json_list` → `candidate_match` → `single_line` → `raw` 五级 fallback。查看 `results.jsonl` 里每条记录的 `parse_mode` 字段可以确认使用了哪条路径：

```bash
python -c "
import json
from collections import Counter
c = Counter()
with open('results.jsonl') as f:
    for line in f:
        c[json.loads(line)['parse_mode']] += 1
print(c)
"
```

如果 `final_answer_list` 占比过低（比如 <90%），可能需要调整 `ANSWER_FORMAT_INSTRUCTION`（位于 `eval_ehrbench_qa.py` 头部）或检查具体 task 的候选格式。

### 7.4 部分样本 `completed=false`

说明该条在 `MAX_RETRIES` 次都失败了（常见原因：单次输入超出 `--max-model-len`、vLLM OOM、网络异常）。查看 `error` 字段定位原因：

```bash
grep '"completed": false' results.jsonl | head -3
```

### 7.5 断点续跑

脚本每次会**覆盖** `results.jsonl`。若中途中断，手动备份已完成的 `results.jsonl`，然后用 `START_INDEX` 跳过已完成的前 K 条、写到新 `OUTPUT_DIR`，最后 `cat` 合并：

```bash
# 假设前 500 条已完成
cp results/qa_.../results.jsonl results.jsonl.part1

START_INDEX=500 OUTPUT_DIR=./results/qa_..._part2 bash eval_ehrbench_qa.sh

cat results.jsonl.part1 results/qa_..._part2/results.jsonl > results.jsonl.all
```

### 7.6 想对比 Agentic vs QA

输出格式一致，直接各自跑完后用同一个 `evaluate_results.py` 打分即可。推荐把输出目录分别命名为 `ehrbench_1800_<model>`（agentic）和 `qa_..._<model>`（QA）以便区分。

---

## 8. 文件清单

| 文件 | 作用 |
|------|------|
| `openresearcher_ehr/eval_ehrbench_qa.sh` | Shell 启动器（auto-detect 模型、拼 OUTPUT_DIR、tee 日志） |
| `openresearcher_ehr/eval_ehrbench_qa.py` | 核心 Python 脚本（prompt 构造 / 并发调用 / 答案抽取 / 结果落盘） |
| `openresearcher_ehr/helper/evaluate_results.py` | 打分脚本（与 agentic 模式共用） |
| `docs/EHR_Bench/EHR_Bench_evaluation.md` | Agentic 模式评测指南 |
| `docs/EHR_Bench/EHR_Bench_qa_evaluation.md` | **本文档**（QA 模式评测指南）|
