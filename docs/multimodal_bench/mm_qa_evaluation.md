# Multimodal EHR Benchmark — Single-Turn QA Evaluation

本文档描述 **single-turn QA 模式** 下对 Multimodal EHR Benchmark 的评测流程。

与 [vllm_runbook.md](./vllm_runbook.md) 中的 agentic 模式不同，QA 模式**不使用 MCP 服务、不调用工具、不做多轮推理**——把 benchmark 中预渲染好的 `input_text`（含 EHR 上下文）加上关联的 CXR 图像一次性送入 vLLM 的 chat completion 接口，让模型单轮给出答案。

| 维度 | Agentic (`run_mm_pipeline.sh`) | QA (`eval_mm_qa.sh`) |
|------|-----------------------------|---------------------------|
| MCP 服务 | 需要 3 个（2 EHR + 1 Image） | **不需要** |
| 工具调用 | 25+ 个 ehr.*/image.* 工具 | 不允许 |
| 交互轮数 | 多轮（默认最多 200） | 单轮 |
| 输入 | 任务指令 + 模型自行查库 | 预渲染的 `input_text`（已含 EHR 数据） + base64 图像 |
| 适用场景 | 完整 agent 能力评估 | 纯推理/理解能力评估，排除工具使用影响 |

输出的 `results.jsonl` 与 agentic 模式格式兼容（合成 `ehr.finish` 工具调用），可直接用 `scorer_mm.py` 打分。

---

## 1. 数据准备

### 1.1 预渲染测试集

QA 模式使用 HuggingFace 上的预渲染数据集：

```
$REPO/data/EHR_multimodal_bench/EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl
```

共 2,703 行，每行包含：

| 字段 | 说明 |
|------|------|
| `qid` | 唯一样本 ID |
| `input_text` | 完整模型输入（task_instruction + patient_info + ehr_context + question） |
| `question` | 原始任务问题（短格式） |
| `image_paths` | CXR 图像相对路径列表 |
| `ground_truth` | 标准答案 `[{"name": "..."}]` |
| `task` | 任务类型 |
| `source_benchmark` | 来源（`medmod` / `ehrxqa`） |
| `answer_type` | 答案类型（`list` / `str`） |

如尚未下载，执行：

```bash
HF_TOKEN=<your_token> huggingface-cli download Chtholly17/EHR_multimodal_bench \
    --repo-type dataset \
    --include "EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl" \
    --local-dir $REPO/data/EHR_multimodal_bench
```

### 1.2 图像文件

图像位于已解压的 benchmark 目录下：

```
$REPO/data/EHR_multimodal_bench/extracted/
├── EHRXQAAgentBench_v3/mimic-cxr/...   (EHRXQA 图像)
└── MedModAgentBench_v3/mimic-cxr/...    (MedMod 图像)
```

脚本会自动将数据集中的路径前缀映射到实际目录：

| 数据集中的前缀 | 映射到 |
|---------------|--------|
| `MedModOriginalLinked_v1/` | `extracted/MedModAgentBench_v3/` |
| `EHRXQAOriginalLinked_v1/` | `extracted/EHRXQAAgentBench_v3/` |

997/2703 行有关联图像，其余为纯文本 EHR 问题。

### 1.3 图像输入验证

经 smoke test 验证，模型确实能读取图像内容——当仅发送图像（无 EHR 文本上下文）时，模型能准确描述 AP portable 片、ECG leads、patient positioning 等纯视觉细节。

但在完整评测中，由于 `input_text` 已包含丰富的 EHR 上下文（`<ehr_context>` 中的 events/stays/tb_cxr 表数据），模型的答案主要由文本驱动，有图和无图的输出可能高度相似。图像通道对 radiology 类任务（需要直接读片的问题）更有价值。

---

## 2. 环境准备

### 2.1 依赖

QA 脚本只依赖 `openai` 和 `Pillow`（用于图像加载/缩放），不依赖 MCP 相关库。复用仓库的 gemma venv 或 deploy_agent venv 即可：

```bash
# 方式 A：gemma venv（推荐，已有 openai + Pillow）
source venv/gemma/bin/activate
python -c "import openai, PIL; print('OK')"

# 方式 B：deploy_agent venv
$REPO/venvs/deploy_agent/bin/python -c "import openai, PIL; print('OK')"
```

### 2.2 vLLM 服务

需要一个支持多模态输入的 vLLM 服务。对于 Qwen3.5-35B-A3B（多模态模型）：

```bash
# 注意 LANGUAGE_MODEL_ONLY 必须为 0 以支持图像输入
LANGUAGE_MODEL_ONLY=0 GPU_MEMORY_UTILIZATION=0.80 \
  bash scripts/run/run_vllm_server_3_5.sh
```

如果模型不支持图像或只需测试文本部分，也可以用 `LANGUAGE_MODEL_ONLY=1`，此时无图像的行正常处理，有图像的行会退化为纯文本。

确认 vLLM 就绪：

```bash
curl -s http://127.0.0.1:4000/v1/models | python -m json.tool
```

---

## 3. 运行评测

### 3.1 全量评测（2703 条）

```bash
cd openresearcher_ehr
bash eval_mm_qa.sh
```

脚本会：
1. 自动从 vLLM 探测 served model
2. 根据模型名 + 数据文件名拼出输出目录
3. 对每条样本构造 `input_text` + 图像的多模态请求，并发送入 vLLM
4. 解析答案，每完成一条即 append 到 `results.jsonl`

### 3.2 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VLLM_BASE_URL` | `http://127.0.0.1:4000` | vLLM 地址 |
| `VLLM_MODEL_NAME` | `auto` | 模型 ID，auto 自动探测 |
| `DATA_PATH` | `../data/.../model_ready_combined_test_set.jsonl` | 数据路径 |
| `BENCH_ROOT` | `../data/EHR_multimodal_bench/extracted` | 图像根目录 |
| `IMAGE_MAX_EDGE` | `1568` | 图像最长边缩放上限 |
| `MAX_CONCURRENCY` | `8` | 最大并发 |
| `MAX_TOKENS` | `32768` | 单次最大生成 tokens |
| `TEMPERATURE` | `0.0` | 采样温度 |
| `ENABLE_THINKING` | `1` | 是否启用 thinking 模式 |
| `LIMIT` | （空） | 只跑前 N 条 |
| `START_INDEX` | `0` | 跳过前 K 条 |
| `OUTPUT_DIR` | 自动生成 | 自定义输出目录 |

### 3.3 常用示例

```bash
# 全量 + 高并发
MAX_CONCURRENCY=16 bash eval_mm_qa.sh

# 冒烟测试：2 条
LIMIT=2 OUTPUT_DIR=./results/mm_qa_smoke bash eval_mm_qa.sh

# 开启 thinking 模式
ENABLE_THINKING=1 bash eval_mm_qa.sh

# 指定自定义输出目录
OUTPUT_DIR=./results/my_mm_qa_run bash eval_mm_qa.sh

# 直接调用 Python（调试用）
python eval_mm_qa.py \
    --data_path ../data/EHR_multimodal_bench/EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl \
    --output_path ./results/mm_qa_debug/results.jsonl \
    --bench_root ../data/EHR_multimodal_bench/extracted \
    --max_concurrency 4 \
    --limit 5 \
    --disable_thinking
```

---

## 4. Pipeline 内部机制

### 4.1 流程

```
model_ready_combined_test_set.jsonl
      │
      ▼
┌─────────────────────────────┐
│  load_benchmark()           │  读入 JSONL，每行一条
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  build_prompt()             │  input_text + ANSWER_FORMAT
│                             │  + resolve & base64 encode images
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  vLLM /v1/chat/completions  │  单轮，无 tools，多模态内容
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  parse_answer()             │  从文本抽 predictions
└─────────────────────────────┘
      │
      ▼
┌─────────────────────────────┐
│  build_result_record()      │  合成 ehr.finish tool_call
│                             │  设置 label = ground_truth
└─────────────────────────────┘
      │
      ▼
  results.jsonl （每行一条，scorer_mm.py 兼容）
```

### 4.2 图像处理

1. 从 `image_paths` 读取相对路径
2. 前缀映射：`MedModOriginalLinked_v1/` → `MedModAgentBench_v3/`
3. 在 `--bench_root` 下查找实际文件
4. PIL 打开 → RGB → 缩放到 max_edge → JPEG 编码 → base64
5. 构造 OpenAI 格式的 `image_url` content block

每个样本最多处理 4 张图像。

### 4.3 答案解析

按优先级：

| 优先级 | 模式 | 规则 |
|--------|------|------|
| 1 | `final_answer_list` | 正则匹配 `Final Answer: [...]`，取最后一个可解析列表 |
| 2 | `json_list` | 整段文本本身是 `[...]` JSON 数组 |
| 3 | `candidate_match` | 在文本中匹配候选答案（本数据集无候选，不触发） |
| 4 | `single_line` | 取第一行 |
| 5 | `raw` | 兜底：原文 |

### 4.4 输出格式

兼容 `scorer_mm.py`，关键字段：

```json
{
  "qid": "...",
  "task": "medmod_radiology",
  "source_benchmark": "medmod",
  "label": [{"name": "Cardiomegaly"}, {"name": "Support Devices"}],
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...",
     "tool_calls": [{"function": {"name": "ehr.finish", "arguments": "{\"response\": [...]}"}}]}
  ],
  "completed": true,
  "stop_reason": "finish_tool_call",
  "qa_mode": true,
  "parse_mode": "final_answer_list",
  "predictions": ["Cardiomegaly", "Support Devices"]
}
```

---

## 5. 打分

使用 `scorer_mm.py`（与 agentic 模式共用）：

```bash
set -a; source $REPO/.env; set +a

$REPO/venvs/deploy_agent/bin/python \
  $REPO/openresearcher_ehr/scorer_mm.py \
  --results $REPO/openresearcher_ehr/results/mm_qa_<slug>/results.jsonl \
  --output-root $REPO/openresearcher_ehr/results/scored \
  --region us-east-1 \
  --concurrency 10
```

输出：
- `scored.jsonl` — 每行一条，含 precision/recall/F1/judge_match
- `summary.json` — 按 task 聚合统计
- `summary.md` — 人类可读摘要

---

## 6. 推荐执行顺序

1. 确认数据存在：`wc -l $REPO/data/EHR_multimodal_bench/EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl`（应为 2703）
2. 确认图像存在：`ls $REPO/data/EHR_multimodal_bench/extracted/MedModAgentBench_v3/mimic-cxr/` 有文件
3. 启动 vLLM（多模态模式）：`LANGUAGE_MODEL_ONLY=0 bash scripts/run/run_vllm_server_3_5.sh`
4. 等 vLLM 就绪
5. 冒烟测试：`LIMIT=2 OUTPUT_DIR=./results/mm_qa_smoke bash eval_mm_qa.sh`
6. 全量评测：`MAX_CONCURRENCY=16 bash eval_mm_qa.sh`
7. 打分（见 §5）

---

## 7. 时间开销参考

| 配置 | 2703 条预估时间 |
|------|----------------|
| Qwen3.5-35B-A3B, 8×A100, thinking=off, concurrency=16 | ~1-2 小时 |
| Qwen3.5-35B-A3B, 8×A100, thinking=on, concurrency=8 | ~3-5 小时 |

单条平均 prompt 约 10-40K tokens（取决于 EHR context 长度），completion 约 1-4K tokens。带图像的行因 base64 编码会增加约 10-50K tokens 的图像 token。

---

## 8. 常见问题

### 8.1 vLLM 返回图像相关错误

确认 vLLM 启动时 `LANGUAGE_MODEL_ONLY=0`。如果模型不支持多模态，图像会被忽略（只发文本）。

### 8.2 图像加载失败

检查 `BENCH_ROOT` 是否指向正确的 `extracted/` 目录。脚本会在 stderr 输出警告但不会中断。

### 8.3 `parse_mode` 不是 `final_answer_list`

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

如果 `final_answer_list` 占比低于 80%，可能需要调整 prompt 中的答案格式指令。

### 8.4 对比 Agentic vs QA

两种模式输出格式一致，都用 `scorer_mm.py` 打分。推荐输出目录分别命名以便区分：
- Agentic: `results/mm_prepared_<model>/`
- QA: `results/mm_qa_<slug>_<model>/`

---

## 9. 文件清单

| 文件 | 作用 |
|------|------|
| `openresearcher_ehr/eval_mm_qa.py` | 核心 Python 脚本 |
| `openresearcher_ehr/eval_mm_qa.sh` | Shell 启动器 |
| `openresearcher_ehr/scorer_mm.py` | 打分脚本（与 agentic 共用） |
| `docs/multimodal_bench/mm_qa_evaluation.md` | 本文档 |
| `docs/multimodal_bench/vllm_runbook.md` | Agentic 模式评测指南 |
