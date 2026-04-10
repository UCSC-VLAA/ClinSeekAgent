# Evaluation Workflow

本文记录 `openresearcher_ehr` 的标准 evaluation 流程。当前流程分三步：

1. 后台启动 EHR MCP 服务
2. 后台启动 vLLM 服务
3. 运行 `run_test_subset.sh` 发起测试

## 目录与脚本

- DeepResearch 根目录：`/home/efs/zlt/deepresearch`
- Evaluation repo 目录：`/home/efs/zlt/deepresearch/openresearcher_ehr`
- MCP 启动脚本：`/home/efs/zlt/deepresearch/scripts/run/run_mcp_server.sh`
- vLLM 启动脚本：`/home/efs/zlt/deepresearch/scripts/run/run_vllm_server_3_5.sh`
- 测试脚本：`/home/efs/zlt/deepresearch/openresearcher_ehr/run_test_subset.sh`

## 一次完整运行

### 1. 启动 MCP 服务

注意：`run_mcp_server.sh` 内部使用了相对路径 `src/run_mcp_server.py`，所以要从 `/home/efs/zlt/deepresearch` 目录启动。

```bash
cd /home/efs/zlt/deepresearch
mkdir -p logs
nohup bash scripts/run/run_mcp_server.sh 0 5103 > logs/mcp_server_5103.log 2>&1 &
```

默认含义：

- `0`：使用 GPU 0
- `5103`：MCP 服务端口
- 数据目录固定为 `/home/efs/zlt/deepresearch/data/EHRAgentBench`
- 服务监听地址固定为 `127.0.0.1`

### 2. 启动 vLLM 服务

同样建议从 `/home/efs/zlt/deepresearch` 目录启动。

```bash
cd /home/efs/zlt/deepresearch
mkdir -p logs
nohup bash scripts/run/run_vllm_server_3_5.sh 0,1,2,3,4,5,6,7 4000 > logs/vllm_4000.log 2>&1 &
```

默认含义：

- `0,1,2,3,4,5,6,7`：使用 8 张卡
- `4000`：vLLM 服务端口
- 默认模型：
  `/home/efs/zlt/deepresearch/models/Qwen3.5-35B-A3B-DeepMed-SFT-epoch2`
- `tensor-parallel-size` 会根据 GPU 数量自动计算
- 默认启用了：
  `--enable-auto-tool-choice`
  `--tool-call-parser qwen3_xml`
  `--reasoning-parser qwen3`
  `--language-model-only`

### 3. 运行 evaluation

从 `openresearcher_ehr` 目录运行测试脚本：

```bash
cd /home/efs/zlt/deepresearch/openresearcher_ehr
bash run_test_subset.sh
```

脚本默认配置如下：

- `DATA_PATH=/home/efs/zlt/deepresearch/data/EHRAgentBench/common/subset_100/merged_subsets_600.json`
- `EHR_MCP_URL=http://127.0.0.1:5103/mcp`
- `VLLM_BASE_URL=http://127.0.0.1:4000`
- `MAX_CONCURRENCY=5`
- `RUNS_PER_QUESTION=1`
- `MAX_ROUNDS=200`
- `TEMPERATURE=0.0`
- `ENABLE_THINKING=1`

`run_test_subset.sh` 会自动请求 `http://127.0.0.1:4000/v1/models`，解析当前 vLLM 提供的模型名，并将结果输出到：

```bash
./subsets_600_${MODEL_SLUG}/results.jsonl
```

例如模型名被解析为 `Qwen3.5-35B-A3B-DeepMed-SFT-epoch2` 时，输出目录通常类似：

```bash
./subsets_600_qwen3_5_35b_a3b_deepmed_sft_epoch2/results.jsonl
```

当前 evaluation 侧已不再额外注入 `[Tool Call: ...]` 这类 tool call 文本格式提示；工具调用依赖模型原生的 tool-calling 能力。如果某一轮模型没有产生 tool call，流程也不会再追加 reminder 提示，而是直接结束该条运行。

## 推荐执行版本

如果只是按当前默认流程跑一遍，直接复制下面三段即可：

```bash
cd /home/efs/zlt/deepresearch
mkdir -p logs
nohup bash scripts/run/run_mcp_server.sh 0 5103 > logs/mcp_server_5103.log 2>&1 &
```

```bash
cd /home/efs/zlt/deepresearch
mkdir -p logs
nohup bash scripts/run/run_vllm_server_3_5.sh 0,1,2,3,4,5,6,7 4000 > logs/vllm_4000.log 2>&1 &
```

```bash
cd /home/efs/zlt/deepresearch/openresearcher_ehr
bash run_test_subset.sh
```

## 启动后检查

### 检查端口

```bash
ss -ltnp | rg ':(5103|4000)\b'
```

### 检查 vLLM 是否返回模型列表

```bash
curl http://127.0.0.1:4000/v1/models
```

### 查看日志

```bash
tail -f /home/efs/zlt/deepresearch/logs/mcp_server_5103.log
tail -f /home/efs/zlt/deepresearch/logs/vllm_4000.log
```

## 常用覆盖参数

如果需要改端口、并发或输出目录，可以在运行 `run_test_subset.sh` 前覆盖环境变量：

```bash
cd /home/efs/zlt/deepresearch/openresearcher_ehr
EHR_MCP_URL=http://127.0.0.1:5104/mcp \
VLLM_BASE_URL=http://127.0.0.1:4001 \
MAX_CONCURRENCY=2 \
RUNS_PER_QUESTION=1 \
OUTPUT_DIR=./subsets_600_custom \
bash run_test_subset.sh
```

## 常见问题

### 1. MCP 脚本能执行，但服务没起来

优先检查当前目录是否是 `/home/efs/zlt/deepresearch`。因为脚本里调用的是相对路径 `src/run_mcp_server.py`，如果从别的目录启动，通常会直接失败。

### 2. `run_test_subset.sh` 找不到模型名

这通常表示 vLLM 还没完全启动，或者 `VLLM_BASE_URL` 配错了。先执行：

```bash
curl http://127.0.0.1:4000/v1/models
```

确认接口已经可用后再跑测试。

### 3. 端口冲突

如果 `5103` 或 `4000` 已被占用，可以换端口启动，并同步修改：

- MCP 启动命令里的端口
- vLLM 启动命令里的端口
- `run_test_subset.sh` 对应的 `EHR_MCP_URL` / `VLLM_BASE_URL`

## 结果文件

evaluation 主结果文件位于：

```bash
/home/efs/zlt/deepresearch/openresearcher_ehr/subsets_600_*/results.jsonl
```

每次运行会写入对应输出目录下的 `results.jsonl`。
