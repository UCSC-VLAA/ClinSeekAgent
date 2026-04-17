# Evaluation Workflow (Bedrock Backend)

本文记录使用 AWS Bedrock 作为 LLM 后端的 `openresearcher_ehr` evaluation 流程。

支持两类模型：
- **Anthropic 模型** (Claude) — 通过 `invoke_model` + Anthropic Messages API
- **第三方模型** (Kimi, GPT-OSS, MiniMax 等) — 通过 `invoke_model` + OpenAI-compatible API

与 vLLM 流程的核心区别：**不需要启动 vLLM 服务**，LLM 推理直接走 AWS Bedrock API，只需配好 AWS 凭证。

流程分四步：

1. 准备数据
2. 创建 Python 虚拟环境（两个：一个跑 deploy_agent，一个跑 MCP server）
3. 后台启动 EHR MCP 服务
4. 运行 `run_test_subset_bedrock.sh`

## 目录与脚本

```
$HOME = /fsx-shared/juncheng/DeepMed-eval

$HOME/openresearcher_ehr/
  ├── deploy_agent.py              # 主入口，agent loop
  ├── bedrock_generator.py         # Bedrock API 封装（支持 Anthropic + OpenAI 格式）
  ├── vllm_generator.py            # vLLM API 封装（本流程不用）
  ├── ehr_pool.py                  # EHR MCP server HTTP 客户端
  ├── browser.py                   # 浏览器工具后端
  ├── data_utils.py                # prompt 模板 + 工具定义
  ├── run_test_subset_bedrock.sh   # Bedrock eval 启动脚本
  ├── run_test_subset.sh           # vLLM eval 启动脚本（对比参考）
  └── .venv/                       # deploy_agent 的虚拟环境（需创建）

$HOME/scripts/run/
  ├── run_mcp_server.sh            # MCP server 启动脚本

$HOME/src/
  └── run_mcp_server.py            # MCP server 入口

$HOME/data/EHRAgentBench/          # 评测数据（需下载或 symlink）
$HOME/models/BioLORD-2023/         # 语义搜索 embedding 模型（需下载或 symlink）
```

---

## Step 1: 准备数据

评测数据来自 HuggingFace：[BlueZeros/AgentEHR-Bench](https://huggingface.co/datasets/BlueZeros/AgentEHR-Bench)

> **注意**：HuggingFace 上的目录叫 `MIMICIVAgentBench`，代码内部使用 `EHRAgentBench`。
> 下载后需要 symlink 或 rename。

```bash
cd $HOME

# 方式 1: huggingface-cli
pip install huggingface_hub
huggingface-cli download --repo-type dataset BlueZeros/AgentEHR-Bench \
    --include "MIMICIVAgentBench/**" \
    --local-dir ./data

# 创建 symlink（代码内部使用 EHRAgentBench 这个名字）
ln -sf $HOME/data/MIMICIVAgentBench $HOME/data/EHRAgentBench

# 方式 2: 如果已有数据，直接 symlink
ln -sf /path/to/existing/MIMICIVAgentBench $HOME/data/EHRAgentBench
```

下载完成后，确认目录结构：

```bash
ls $HOME/data/EHRAgentBench/
# 应看到: all/  common/  database/  item_set/  rare/  sample/  table_description/  train/

ls $HOME/data/EHRAgentBench/database/ | wc -l
# 应有 ~9857 个 patient_*.db 文件
```

### 准备评测数据子集

默认评测使用 600 条数据（6 个 task 各 100 条）。如果 `subset_100/merged_subsets_600.json` 不存在，需要生成：

```bash
mkdir -p $HOME/data/subset_100
python3 -c "
import json, os, random
random.seed(42)
base = '$HOME/data/EHRAgentBench/common'
merged = []
for f in sorted(os.listdir(base)):
    if f.endswith('.json'):
        with open(os.path.join(base, f)) as fp:
            data = json.load(fp)
        merged.extend(random.sample(data, min(100, len(data))))
with open('$HOME/data/subset_100/merged_subsets_600.json', 'w') as fp:
    json.dump(merged, fp, indent=2, ensure_ascii=False)
print(f'Generated {len(merged)} records')
"
```

### 准备 BioLORD-2023 模型

MCP server 的语义搜索工具需要 `BioLORD-2023` embedding 模型：

```bash
# 如果已有，symlink 即可
mkdir -p $HOME/models
ln -sf /path/to/BioLORD-2023 $HOME/models/BioLORD-2023

# 如果没有，从 HuggingFace 下载
pip install sentence-transformers
python3 -c "
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('FremyCompany/BioLORD-2023')
model.save('$HOME/models/BioLORD-2023')
"
```

---

## Step 2: 创建虚拟环境

本流程需要**两个虚拟环境**：

### 2a: deploy_agent 虚拟环境（轻量，无 PyTorch）

```bash
cd $HOME/openresearcher_ehr
uv venv .venv --python 3.12
uv pip install boto3 httpx aiohttp requests python-dotenv openai-harmony gpt-oss
```

验证：

```bash
.venv/bin/python -c "import boto3, httpx, aiohttp, dotenv; print('OK')"
```

### 2b: MCP server 虚拟环境（需要 PyTorch + GPU）

```bash
cd $HOME
uv venv .venv_mcp --python 3.12

# CUDA GPU:
uv pip install --python .venv_mcp/bin/python \
    torch --index-url https://download.pytorch.org/whl/cu121

# 安装其余 MCP 依赖：
uv pip install --python .venv_mcp/bin/python \
    fastmcp pandas numpy sentence-transformers thefuzz pydantic
```

验证：

```bash
.venv_mcp/bin/python -c "import fastmcp, pandas, sentence_transformers, thefuzz; print('OK')"
```

---

## Step 3: 启动 MCP 服务

### 3a: 修改 MCP 启动脚本

编辑 `$HOME/scripts/run/run_mcp_server.sh`，将 Python 路径和数据路径改成你的环境：

```bash
DATA_PATH="$HOME/data/EHRAgentBench"

CUDA_VISIBLE_DEVICES=${GPU_ID} $HOME/.venv_mcp/bin/python src/run_mcp_server.py \
    --mode "http" \
    --host 127.0.0.1 \
    --port $PORT \
    --data_path "$DATA_PATH"
```

### 3b: 启动

**必须从 `$HOME` 目录启动**（脚本内使用相对路径 `src/run_mcp_server.py`）：

```bash
cd $HOME
mkdir -p logs
nohup bash scripts/run/run_mcp_server.sh 0 5103 > logs/mcp_server_5103.log 2>&1 &
```

### 3c: 验证

```bash
# 等待启动（加载 BioLORD-2023 模型需要 ~20 秒）
sleep 20

# 检查端口
ss -ltnp | grep ':5103'

# 查看日志，确认 "Uvicorn running on http://127.0.0.1:5103"
tail -5 $HOME/logs/mcp_server_5103.log
```

---

## Step 4: 运行 evaluation

### 4a: 确认 AWS 凭证

```bash
aws sts get-caller-identity
```

### 4b: 可用模型列表

已验证可用的 Bedrock 模型：

| Model ID | Region | 类型 | 说明 |
|----------|--------|------|------|
| `global.anthropic.claude-sonnet-4-6` | `ca-west-1` | Anthropic | Claude Sonnet 4.6 |
| `global.anthropic.claude-opus-4-6-v1` | `ca-west-1` | Anthropic | Claude Opus 4.6 |
| `moonshotai.kimi-k2.5` | `us-east-1` | OpenAI-compatible | Kimi K2.5 |
| `openai.gpt-oss-120b-1:0` | `us-east-1` | OpenAI-compatible | GPT-OSS 120B |
| `minimax.minimax-m2.5` | `us-east-1` | OpenAI-compatible | MiniMax M2.5 |
| `moonshot.kimi-k2-thinking` | `us-east-1` | OpenAI-compatible | Kimi K2 Thinking |

> **重要**：Anthropic 模型在 `ca-west-1`，第三方模型在 `us-east-1`。必须设对 `BEDROCK_REGION`。

查询某个 region 的可用模型：

```bash
$HOME/openresearcher_ehr/.venv/bin/python -c "
import boto3
client = boto3.client('bedrock', region_name='us-east-1')
for m in client.list_foundation_models()['modelSummaries']:
    print(f'{m[\"modelId\"]:50s} {m.get(\"modelName\",\"\")}')" | head -30
```

### 4c: 快速验证模型可用

```bash
# 验证 Anthropic 模型
$HOME/openresearcher_ehr/.venv/bin/python -c "
import boto3, json
client = boto3.client('bedrock-runtime', region_name='ca-west-1')
resp = client.invoke_model(
    modelId='global.anthropic.claude-sonnet-4-6',
    body=json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 32,
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    })
)
print(json.loads(resp['body'].read())['content'][0]['text'])
"

# 验证第三方模型（OpenAI 格式）
$HOME/openresearcher_ehr/.venv/bin/python -c "
import boto3, json
client = boto3.client('bedrock-runtime', region_name='us-east-1')
resp = client.invoke_model(
    modelId='moonshotai.kimi-k2.5',
    body=json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 32,
        'messages': [{'role': 'user', 'content': 'hi'}]
    })
)
print(json.loads(resp['body'].read())['choices'][0]['message']['content'])
"
```

### 4d: 启动 evaluation

**Claude (默认):**

```bash
cd $HOME/openresearcher_ehr
bash run_test_subset_bedrock.sh
```

**第三方模型 — 必须指定 `BEDROCK_REGION=us-east-1`:**

```bash
cd $HOME/openresearcher_ehr

# Kimi K2.5
BEDROCK_MODEL_ID=moonshotai.kimi-k2.5 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
bash run_test_subset_bedrock.sh

# GPT-OSS-120B
BEDROCK_MODEL_ID=openai.gpt-oss-120b-1:0 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
bash run_test_subset_bedrock.sh

# MiniMax M2.5
BEDROCK_MODEL_ID=minimax.minimax-m2.5 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
bash run_test_subset_bedrock.sh
```

### 4e: 并行运行多个模型

每个模型启动一个后台进程。它们共享同一个 MCP server，互不干扰：

```bash
cd $HOME/openresearcher_ehr

# 后台启动 Kimi
BEDROCK_MODEL_ID=moonshotai.kimi-k2.5 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
nohup bash run_test_subset_bedrock.sh > $HOME/logs/eval_kimi_k2.5.log 2>&1 &

# 后台启动 GPT-OSS
BEDROCK_MODEL_ID=openai.gpt-oss-120b-1:0 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
nohup bash run_test_subset_bedrock.sh > $HOME/logs/eval_gpt_oss_120b.log 2>&1 &

# 后台启动 MiniMax
BEDROCK_MODEL_ID=minimax.minimax-m2.5 \
BEDROCK_REGION=us-east-1 \
MAX_CONCURRENCY=20 \
nohup bash run_test_subset_bedrock.sh > $HOME/logs/eval_minimax_m2.5.log 2>&1 &
```

### 4f: 监控进度

```bash
# 实时监控所有模型的完成数量
watch -n 30 'for d in $HOME/openresearcher_ehr/subsets_600_*/; do
    name=$(basename $d)
    count=$(wc -l < "$d/results.jsonl" 2>/dev/null || echo 0)
    echo "$name: $count/600"
done'

# 查看某个模型的错误数
grep -c "ERROR" $HOME/logs/eval_kimi_k2.5.log

# 查看完成率
python3 -c "
import json, sys
c=e=0
with open(sys.argv[1]) as f:
    for l in f:
        d=json.loads(l)
        if d.get('completed'): c+=1
        else: e+=1
print(f'Completed: {c}, Incomplete: {e}, Total: {c+e}')
" $HOME/openresearcher_ehr/subsets_600_moonshotai_kimi_k2_5/results.jsonl
```

### 脚本参数总览

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `BEDROCK_REGION` | `ca-west-1` | AWS region（第三方模型用 `us-east-1`） |
| `DATA_PATH` | `$HOME/data/EHRAgentBench/common/subset_100/merged_subsets_600.json` | 评测数据文件 |
| `EHR_MCP_URL` | `http://127.0.0.1:5103/mcp` | MCP server 地址 |
| `MAX_CONCURRENCY` | `5` | 最大并行查询数 |
| `RUNS_PER_QUESTION` | `1` | 每个问题运行次数 |
| `MAX_ROUNDS` | `200` | 每个问题最大对话轮数 |
| `TEMPERATURE` | `1.0` | 采样温度 |
| `OUTPUT_DIR` | 自动生成（基于 model ID） | 输出目录 |

---

## API 路由说明

`bedrock_generator.py` 根据 model ID 自动选择 API 格式：

```
model ID 包含 "anthropic"
  → _chat_completion_anthropic()
  → invoke_model + Anthropic Messages API
  → 请求/响应使用 Anthropic 格式（content blocks, tool_use, tool_result）

其他模型（Kimi, GPT-OSS, MiniMax 等）
  → _chat_completion_openai()
  → invoke_model + OpenAI-compatible API
  → 请求/响应使用 OpenAI 格式（messages, tool_calls, function）
```

不需要手动指定格式，代码会自动处理。

---

## 安全机制

### 工具结果截断

单个 tool result 超过 50,000 字符时会被自动截断（`deploy_agent.py` 中的 `MAX_TOOL_RESULT_CHARS`）。
这防止了 `get_records_by_time` 查询大表（如 `chartevents`, `labevents`）导致 context overflow。

### JSON 参数修复

模型有时会生成格式错误的 tool call 参数（如未闭合的字符串）。
`_prepare_openai_messages()` 在发送请求前会验证所有 tool call 参数的 JSON 合法性，
无效的参数会被替换为 `"{}"`，避免整个请求被 Bedrock 拒绝。

### 请求体大小兜底

`_truncate_tool_results()` 作为兜底机制，在序列化后的请求体超过 10MB 时，
从最早的 tool result 开始逐步截断到 500 字符。

---

## 与 vLLM 流程的对比

| 维度 | vLLM 流程 | Bedrock 流程 |
|------|----------|-------------|
| 需要 GPU | vLLM 推理 + MCP 都需要 | 仅 MCP 需要（用于 semantic search） |
| 需要启动 vLLM 服务 | 是 | 否 |
| 需要 AWS 凭证 | 否 | 是 |
| deploy_agent venv | 需要 PyTorch + vLLM 等 | 轻量（boto3 + httpx + aiohttp） |
| MCP venv | 共用 | 独立（fastmcp + sentence-transformers） |
| 模型 | 任意 vLLM 支持的本地模型 | Bedrock 上可用的模型 |
| tool calling | 多格式文本解析 fallback | Anthropic 原生 API / OpenAI-compatible API |
| 并行评测 | 受限于 GPU 数量 | 可同时跑多个模型（共享 MCP server） |

---

## 常见问题

### 1. `run_mcp_server.sh` 报错找不到 Python

脚本内硬编码了 Python 路径。改成你的 `.venv_mcp/bin/python` 路径。

### 2. `run_mcp_server.sh` 报 `ModuleNotFoundError`

MCP venv 缺少依赖。检查是否安装了 fastmcp, sentence-transformers, pandas, thefuzz, pydantic。

### 3. MCP server 报 `FileNotFoundError: BioLORD-2023 not found`

需要下载或 symlink BioLORD-2023 模型到 `$HOME/models/BioLORD-2023/`。见 Step 1。

### 4. `AccessDeniedException`

AWS 凭证无效或过期。运行 `aws sts get-caller-identity` 确认。

### 5. `The provided model identifier is invalid`

模型不在当前 region。第三方模型（Kimi, GPT-OSS, MiniMax）需要 `BEDROCK_REGION=us-east-1`。

### 6. `ThrottlingException`

Bedrock API 限流。降低 `MAX_CONCURRENCY`（建议从 5 开始逐步加大）。

### 7. `context length` / `length limit exceeded`

Agent 对话过长。50k 字符的 tool result 截断已内置，通常不会再出现。
如果仍然出现，可以降低 `MAX_TOOL_RESULT_CHARS`（`deploy_agent.py` 中）。

### 8. 数据文件不存在

确认已下载数据且 `data/EHRAgentBench` symlink 正确指向 `MIMICIVAgentBench`。
确认 `subset_100/merged_subsets_600.json` 已生成（见 Step 1）。

### 9. HuggingFace 数据目录名不匹配

HuggingFace 上数据目录叫 `MIMICIVAgentBench`，代码内部使用 `EHRAgentBench`。
需要 symlink：`ln -sf .../MIMICIVAgentBench .../data/EHRAgentBench`。

### 10. `.venv` 已存在但环境不完整

删除后重建：`rm -rf .venv && uv venv .venv --python 3.12 && uv pip install ...`

---

## 结果文件

```bash
$HOME/openresearcher_ehr/subsets_600_*/results.jsonl
```

每行一个 JSON 对象，包含：`qid`, `question`, `messages`（完整对话历史）, `completed`, `status`, `stop_reason`, 以及原始 task 字段（`subject_id`, `task`, `label` 等）。

输出目录名自动从 model ID 生成：

| Model ID | Output dir |
|----------|-----------|
| `global.anthropic.claude-sonnet-4-6` | `subsets_600_claude_sonnet_4_6/` |
| `moonshotai.kimi-k2.5` | `subsets_600_moonshotai_kimi_k2_5/` |
| `openai.gpt-oss-120b-1:0` | `subsets_600_openai_gpt_oss_120b_1_0/` |
| `minimax.minimax-m2.5` | `subsets_600_minimax_minimax_m2_5/` |
