# Agent 执行机制详解

本文档详细介绍评测（evaluation）过程中，Agent 从初始化到完成一个临床预测任务的完整运行流程。本文所述流程基于 `openresearcher_ehr/` 目录下的评测管线，建议先阅读 [evaluation.md](./evaluation.md) 了解评测环境的搭建方式。

---

## 1. 整体流程概览

```
run_test_subset.sh
  │
  │  解析环境变量、自动探测 vLLM 模型名
  │
  └─▶ python deploy_agent.py  (主入口)
       │
       ├─ 初始化 Generator（vLLM 或 Bedrock）
       ├─ 初始化 BrowserPool（网页搜索工具池）
       ├─ 初始化 EHRToolPool（EHR MCP 客户端池）
       ├─ load_query_data()  加载评测数据
       │
       └─ 并发执行所有任务（asyncio.Semaphore 控制并发数）
            │
            ├─ process_query_item()   ← 单任务包装
            │    └─ run_one_query()
            │         └─ run_one_native()  ← 核心行动循环
            │              │
            │              │  while round < max_rounds:
            │              │    LLM → tool_calls → 路由到 Browser/EHR → 结果回填
            │              │    如果调用了 ehr.finish → 退出
            │              │
            │              └─ 返回完整 messages
            │
            └─ 结果写入 results.jsonl（每条即时落盘）
```

---

## 2. 启动入口：run_test_subset.sh

**文件**: `openresearcher_ehr/run_test_subset.sh`

这个脚本是评测的起点，负责环境变量设置和参数组装。关键行为：

1. **自动探测模型名**：当 `VLLM_MODEL_NAME=auto`（默认）时，脚本会访问 vLLM 的 `/v1/models` 接口获取实际模型 ID
2. **生成 slug**：从模型名提取简短标识（如 `qwen3_5_35b_a3b`），用于输出目录命名
3. **日志管理**：通过 `tee` 同时输出到终端和日志文件
4. **调用 deploy_agent.py**：将所有配置以命令行参数传入

可配置的核心环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATA_PATH` | `../data/.../merged_subsets_500.json` | 评测数据集路径 |
| `EHR_MCP_URL` | `http://127.0.0.1:5103/mcp` | MCP 服务地址 |
| `VLLM_BASE_URL` | `http://127.0.0.1:4000` | vLLM 服务地址 |
| `MAX_CONCURRENCY` | 5 | 最大并发任务数 |
| `MAX_ROUNDS` | 200 | 单任务最大对话轮次 |
| `MAX_TOOL_RESULT_CHARS` | 100000 | 工具返回截断字符数 |
| `ENABLE_THINKING` | 1 | 是否启用模型 thinking 模式 |
| `TEMPERATURE` | 0.0 | 采样温度 |

---

## 3. 主程序：deploy_agent.py

**文件**: `openresearcher_ehr/deploy_agent.py`

### 3.1 `main()` — 初始化阶段

`main()` 是 `async` 入口，由 `asyncio.run()` 驱动。初始化按以下顺序执行：

```python
async def main():
    # 1. 解析命令行参数
    args = parser.parse_args()

    # 2. 初始化 LLM Generator（二选一）
    if backend == "bedrock":
        generator = BedrockAsyncGenerator(model_id=..., enable_thinking=...)
    elif backend == "vllm":
        generator = VLLMOpenAIAsyncGenerator(model_name=..., base_url=...)

    # 3. 初始化工具池
    browser_pool = BrowserPool(search_url, browser_backend)
    ehr_pool = EHRToolPool(mcp_url=args.ehr_mcp_url)

    # 4. 加载评测数据
    data = load_query_data(args.data_path)

    # 5. 并发执行
    for run_index in range(1, runs_per_question + 1):
        batch_tasks = [asyncio.create_task(process_query_item(...)) for item in data]
        await asyncio.gather(*batch_tasks)
```

### 3.2 LLM Generator

系统支持两种后端：

**VLLMOpenAIAsyncGenerator**（`vllm_generator.py`）：
- 通过 OpenAI 兼容 API 与本地 vLLM 服务通信
- 支持标准 tool calling 以及多种 fallback 解析（XML `<tool_call>`、`<function=...>`、JSON 等）
- 对 OpenSeeker 模型有专用的 prompt 渲染和工具调用解析逻辑
- 通过 `ThreadPoolExecutor` 将同步 OpenAI SDK 调用包装为 async

**BedrockAsyncGenerator**（`bedrock_generator.py`）：
- 通过 boto3 调用 AWS Bedrock（Claude 系列）
- 自动将 OpenAI 格式的 messages/tools 转换为 Anthropic 原生格式
- 支持 extended thinking（adaptive 模式）
- 内置重试机制处理 throttling 和临时性错误

两者对外暴露统一的 `chat_completion()` 接口，返回 OpenAI 风格的 dict：

```python
response = await generator.chat_completion(
    messages=messages,
    tools=tools,
    tool_choice="auto",
    temperature=temperature,
    max_tokens=8192
)
# response["choices"][0]["message"] 包含 content、tool_calls、reasoning_content
```

### 3.3 数据加载与问题生成

`load_query_data()` 支持 JSON 和 JSONL 两种格式，自动探测。每条数据包含：

```json
{
    "subject_id": 10000032,
    "prediction_time": "2150-12-01 10:00:00",
    "task": "diagnoses_ccs",
    "ground_truth": [{"name": "Coronary atherosclerosis..."}, ...]
}
```

`resolve_question()` 会调用 `data_utils.generate_question_from_task()`，根据 `task` 字段选择对应的 prompt 模板（定义在 `data_utils.TASK_PROMPT_TEMPLATES` 中），填入 `subject_id` 和 `prediction_time`。

---

## 4. 任务类型与 Prompt 模板

**文件**: `openresearcher_ehr/data_utils.py`

每种临床预测任务有一套专用的 prompt 模板，定义在 `TASK_PROMPT_TEMPLATES` 中：

| 任务类型 | Agent 角色 | 预测目标 | 参考候选表 |
|----------|-----------|----------|-----------|
| `diagnoses_ccs` | 诊断医生 | 所有可能的 CCS 诊断 | `diagnoses_ccs_candidates` |
| `procedures_ccs` | 手术规划师 | 所有必要的 CCS 手术操作 | `procedures_ccs_candidates` |
| `labevents` | 检验医学专家 | 所有必要的实验室检查 | `labevents_candidates` |
| `prescriptions` | 药剂师 | 所有必要的 ATC 药物分类 | `prescriptions_atc_candidates` |
| `microbiologyevents` | 临床微生物学家 | 所有必要的微生物学检查 | `microbiologyevents_candidates` |
| `transfers` | 医院病房协调员 | 最合适的转科去向 | `transfers_candidates` |

每个模板的结构一致：

```xml
<task_instruction>
  角色描述 + 目标 + 候选表说明 + browser.search 提示 + 输出格式要求
</task_instruction>

<patient_info>
  Current Time: {current_time}
  Patient Subject ID: {subject_id}
</patient_info>
```

---

## 5. 工具体系

Agent 可使用两类工具，共 19 个，工具定义以 JSON 格式维护在 `data_utils.py` 中。

### 5.1 Browser 工具（3 个）

用于网络搜索，获取医学知识和临床指南：

| 工具 | 功能 |
|------|------|
| `browser.search` | 搜索网页，返回 top-N 结果 |
| `browser.open` | 打开并阅读指定网页 |
| `browser.find` | 在当前页面中精确查找文本 |

`BrowserPool` 为每个 qid 维护独立的 `BrowserTool` 会话，支持 local 和 Serper 两种搜索后端。

### 5.2 EHR 工具（16 个）

用于查询患者电子健康记录：

| 工具 | 功能 |
|------|------|
| `ehr.load_ehr` | **必须首先调用**，加载患者 EHR 数据库 |
| `ehr.get_table_names` | 列出所有可用数据表 |
| `ehr.get_column_names` | 获取指定表的列信息 |
| `ehr.get_table_description` | 获取表描述和 schema |
| `ehr.get_unique_values` | 获取某列的所有唯一值 |
| `ehr.get_records_by_time` | 按时间范围查询记录 |
| `ehr.get_event_counts_by_time` | 统计时间范围内的事件数 |
| `ehr.get_latest_records` | 获取最近一次记录 |
| `ehr.get_records_by_keyword` | 按关键词搜索文本列 |
| `ehr.get_records_by_value` | 按精确值匹配查询 |
| `ehr.run_sql_query` | 执行自定义 SQL 查询 |
| `ehr.get_candidates_by_keyword` | 在候选表中关键词搜索 |
| `ehr.get_candidates_by_fuzzy_matching` | 模糊匹配候选实体 |
| `ehr.get_candidates_by_semantic_similarity` | 语义相似度搜索候选实体 |
| `ehr.think` | 记录推理过程（不执行实际操作） |
| `ehr.finish` | **提交最终答案，终止任务** |

### 5.3 EHRToolPool — MCP 客户端

**文件**: `openresearcher_ehr/ehr_pool.py`

`EHRToolPool` 封装了与 MCP 服务器的 HTTP 通信：

```
EHRToolPool
  ├─ 通过 httpx 发送 JSON-RPC 2.0 请求到 MCP 服务器
  ├─ 每个 qid 维护独立的 MCP session（mcp-session-id 头）
  ├─ 支持 SSE（Server-Sent Events）和普通 JSON 两种响应格式
  └─ 自动重新加载：如果工具调用返回"请先 load_ehr"，自动重载后重试
```

会话生命周期：
1. `init_session(qid)` → 发送 `initialize` + `notifications/initialized`
2. `call_tool(qid, name, args)` → 发送 `tools/call`
3. `cleanup(qid)` → 调用 `clear_session_ehr` 后释放会话

---

## 6. 核心行动循环：run_one_native

**文件**: `openresearcher_ehr/deploy_agent.py` — `run_one_native()`

这是单个任务的执行核心。以下是完整的循环逻辑。

### 6.1 初始化

```python
# 初始化 browser 和 EHR 会话
browser_pool.init_session(qid)
await ehr_pool.init_session(qid)

# 构造初始 messages
messages = [
    {"role": "system",  "content": DEVELOPER_CONTENT_CLAUDE},   # 系统指令
    {"role": "user",    "content": question},                    # 任务 prompt
]

# 加载所有 19 个工具定义
tools = json.loads(COMBINED_TOOL_CONTENT_FULL)
```

**System Prompt**（`DEVELOPER_CONTENT_CLAUDE`）定义了 Agent 的角色和工具使用规范：

```
You are a research assistant with access to both web browsing and clinical EHR tools.

**Browser Tools** (for web research and medical knowledge):
- browser.search / browser.open / browser.find

**EHR Tools** (for clinical data analysis):
- ehr.load_ehr / ehr.get_table_names / ehr.run_sql_query / ...

**Important:** Whenever you engage in thinking, reasoning, or analysis,
you MUST use Browser Tools to support your process...
```

### 6.2 行动循环

```python
while round_num < max_rounds:
    round_num += 1

    # ① 调用 LLM
    response = await generator.chat_completion(
        messages=messages,
        tools=tools,
        tool_choice="auto",
        temperature=temperature,
        max_tokens=8192
    )

    # ② 提取 LLM 输出
    message = response["choices"][0]["message"]
    content = message.get("content", "")
    reasoning_content = message.get("reasoning_content", "")
    tool_calls = message.get("tool_calls", [])

    # ③ 名称标准化（处理模型输出的各种变体）
    normalized_tool_calls = normalize_tool_calls(tool_calls)

    # ④ 追加 assistant 消息到历史
    messages.append(assistant_message)

    # ⑤ 如果没有工具调用 → 结束
    if not tool_calls:
        break

    # ⑥ 逐个执行工具调用
    for tool_call in normalized_tool_calls:
        function_name = tool_call["function"]["name"]
        function_args = tool_call["function"]["arguments"]

        if function_name.startswith("ehr."):
            # EHR 工具 → 通过 EHRToolPool 调用 MCP 服务器
            result = await ehr_pool.call_tool(qid, actual_name, function_args)

        elif function_name.startswith("browser."):
            # Browser 工具 → 通过 BrowserPool 调用搜索后端
            result = await browser_pool.call_tool(qid, actual_name, function_args)

        # 截断过长的结果
        result = truncate_tool_result(result, max_tool_result_chars)

        # 追加 tool result 到历史
        messages.append({"role": "tool", "tool_call_id": ..., "content": result})

        # 检查是否调用了 finish
        if actual_name == "finish":
            finish_tool_called = True

    # ⑦ 如果 finish 被调用 → 结束
    if finish_tool_called:
        break
```

### 6.3 终止条件

| 条件 | 说明 |
|------|------|
| `ehr.finish` 被调用 | Agent 主动提交答案（正常完成） |
| `round_num >= max_rounds` | 达到最大轮次上限（默认 200） |
| LLM 未产出任何 tool_calls | 模型停止调用工具（无更多动作） |
| 异常 | 工具执行或 LLM 调用报错 |

### 6.4 工具名称标准化

模型输出的工具名可能有多种格式变体（尤其是开源模型），`normalize_tool_call_name()` 统一处理：

```
"browser_search"      → "browser.search"
"search"              → "browser.search"
"ehr_load_ehr"        → "ehr.load_ehr"
"load_ehr"            → "ehr.load_ehr"
"get_records_by_time" → "ehr.get_records_by_time"
```

---

## 7. 对话历史（messages）结构

Agent 维护的 `messages` 列表遵循 OpenAI Chat API 格式：

```python
[
    # 系统指令：角色定义 + 工具使用规范
    {"role": "system",    "content": DEVELOPER_CONTENT_CLAUDE},

    # 用户任务：包含 <task_instruction> 和 <patient_info>
    {"role": "user",      "content": question},

    # 第 1 轮：模型调用 ehr.load_ehr
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool",      "tool_call_id": "...", "content": "EHR loaded..."},

    # 第 2 轮：模型查询某张表
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool",      "tool_call_id": "...", "content": "Records: ..."},

    # ... 反复查询、搜索、推理 ...

    # 最后一轮：模型调用 ehr.finish 提交答案
    {"role": "assistant", "content": "...", "tool_calls": [
        {"function": {"name": "ehr.finish", "arguments": '["Diagnosis A", "Diagnosis B"]'}}
    ]},
    {"role": "tool",      "tool_call_id": "...", "content": "..."},
]
```

如果模型支持 thinking，assistant 消息中还会包含 `reasoning_content` 字段。

---

## 8. 并发调度

`deploy_agent.py` 使用 `asyncio` 实现并发：

```
runs_per_question（默认 1）
  × 数据集大小（如 500 条）
  = 总任务数（如 500）

按 run 批次执行：
  run_1: [task_1, task_2, ..., task_500]  ← 并发，受 Semaphore 限制
  run_2: [task_1, task_2, ..., task_500]  ← 第二轮（如有）
  ...
```

`asyncio.Semaphore(concurrency)` 控制同时运行的任务数（默认 5，上限 12）。

每个任务完成后，通过 `asyncio.Lock` 保护的写操作即时追加到 `results.jsonl`，确保即使中断也不丢失已完成的结果。

---

## 9. 结果格式与评分

### 9.1 结果格式

每个任务的结果以 JSONL 格式写入 `results.jsonl`，单条结构：

```json
{
    "qid": "diagnoses_ccs_10000032",
    "run_index": 1,
    "session_id": "diagnoses_ccs_10000032__q_1__run_1",
    "question": "<task_instruction>...",
    "messages": [ ... ],
    "completed": true,
    "status": "success",
    "stop_reason": "finish_tool_call",
    "subject_id": 10000032,
    "task": "diagnoses_ccs",
    "ground_truth": [...]
}
```

`completed` 和 `stop_reason` 由 `summarize_conversation_completion()` 判定：
- `finish_tool_call`：最后一条 assistant 消息包含 `finish` 工具调用
- `exact_answer_text_*`：消息文本中包含 "exact answer:" + "confidence:" 模式
- `no_final_answer`：未检测到有效的终止信号

### 9.2 评分

评测完成后，使用 `openresearcher_ehr/helper/evaluate_results.py` 进行离线打分。该脚本：

1. 从 `results.jsonl` 提取每条任务的预测（解析 `ehr.finish` 的参数）
2. 与 benchmark 文件中的 `ground_truth` 做集合匹配
3. 计算 Precision / Recall / F1（大小写不敏感）
4. 统计平均 tool call 次数和 browser 工具占比

详细用法参见 [evaluation.md](./evaluation.md) 第 5 节。

---

## 10. 完整单任务时序图

以一次诊断预测任务为例：

```
┌──────────────┐   ┌───────────┐   ┌──────────────┐   ┌──────────────┐
│ deploy_agent │   │ Generator │   │  EHRToolPool │   │ BrowserPool  │
│  (调度器)     │   │  (LLM)    │   │  (MCP Client)│   │  (Web搜索)   │
└──────┬───────┘   └─────┬─────┘   └──────┬───────┘   └──────┬───────┘
       │                 │                │                   │
       │ init_session(qid)               │                   │
       │─────────────────────────────────>│                   │
       │                 │                │ JSON-RPC init     │
       │                 │                │──────────────────>│MCP Server
       │                 │                │<─ session_id ─────│
       │                 │                │                   │
       │ messages=[system, user]         │                   │
       │                 │                │                   │
  ┌────┤ Round 1         │                │                   │
  │    │ chat_completion(messages, tools) │                   │
  │    │────────────────>│                │                   │
  │    │<── tool_calls: [ehr.load_ehr]   │                   │
  │    │                 │                │                   │
  │    │ ehr_pool.call_tool("load_ehr")  │                   │
  │    │─────────────────────────────────>│                   │
  │    │<─────────── "EHR loaded" ───────│                   │
  │    │                 │                │                   │
  ├────┤ Round 2         │                │                   │
  │    │ chat_completion(messages, tools) │                   │
  │    │────────────────>│                │                   │
  │    │<── tool_calls: [ehr.get_table_names]                │
  │    │─────────────────────────────────>│                   │
  │    │<──────── table list ────────────│                   │
  │    │                 │                │                   │
  ├────┤ Round 3         │                │                   │
  │    │ chat_completion(...)             │                   │
  │    │────────────────>│                │                   │
  │    │<── tool_calls: [browser.search] │                   │
  │    │                 │                │                   │
  │    │ browser_pool.call_tool("search")│                   │
  │    │─────────────────────────────────────────────────────>│
  │    │<────────────── search results ──────────────────────│
  │    │                 │                │                   │
  │    │  ... 反复查询 EHR 表、搜索知识、交叉验证 ...         │
  │    │                 │                │                   │
  ├────┤ Round N         │                │                   │
  │    │ chat_completion(...)             │                   │
  │    │────────────────>│                │                   │
  │    │<── tool_calls: [ehr.finish(["Diagnosis A", ...])]   │
  │    │                 │                │                   │
  └────┤ finish detected → break         │                   │
       │                 │                │                   │
       │ cleanup(qid)    │                │                   │
       │─────────────────────────────────>│                   │
       │                 │                │                   │
       │ 写入 results.jsonl               │                   │
       │                 │                │                   │
```

---

## 11. 关键配置参数速查

### 命令行参数（deploy_agent.py）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--backend` | `bedrock` | LLM 后端：`bedrock` 或 `vllm` |
| `--model_name_or_path` | Claude Opus 4.6 | 模型 ID 或路径 |
| `--api_base_url` | `http://127.0.0.1:4000` | vLLM 服务地址 |
| `--enable_ehr` | — | 启用 EHR 工具（flag） |
| `--ehr_mcp_url` | `http://127.0.0.1:5003/mcp` | EHR MCP 服务地址 |
| `--max_rounds` | 200 | 单任务最大对话轮次 |
| `--max_concurrency` | 12 | 最大并发数 |
| `--runs_per_question` | 1 | 每个问题运行几次 |
| `--max_tool_result_chars` | 100000 | 工具返回截断长度 |
| `--temperature` | 1.0 | 采样温度 |
| `--enable_thinking` | — | 启用 thinking 模式（flag） |

---

## 12. 关键源文件索引

| 文件 | 内容 |
|------|------|
| `openresearcher_ehr/run_test_subset.sh` | 评测启动脚本，环境变量配置 |
| `openresearcher_ehr/deploy_agent.py` | 主程序：初始化、并发调度、行动循环 |
| `openresearcher_ehr/data_utils.py` | 任务 prompt 模板、工具定义、系统指令 |
| `openresearcher_ehr/ehr_pool.py` | EHR MCP 客户端池（HTTP JSON-RPC） |
| `openresearcher_ehr/vllm_generator.py` | vLLM OpenAI 兼容 Generator |
| `openresearcher_ehr/bedrock_generator.py` | AWS Bedrock Claude Generator |
| `openresearcher_ehr/browser.py` | Browser 工具实现 |
| `openresearcher_ehr/helper/evaluate_results.py` | 离线评分脚本 |
