# Model Evaluation Guide

本文记录如何在当前仓库中完成一次完整的模型评测流程。以下命令默认在仓库根目录执行，并全部使用相对路径。

## 1. 环境准备

在安装依赖和启动服务前，需要先准备评测所依赖的数据和模型。以下路径均相对于仓库根目录：

- 数据集 `https://huggingface.co/datasets/BlueZeros/AgentEHR-Bench` 需要下载到 `data/AgentEHR-Bench`
- 数据集 `https://huggingface.co/datasets/BlueZeros/EHR-Bench` 需要下载到 `data/EHR-Bench`
- 模型 `Qwen3.5-35B-A3B` 需要下载到 `models/Qwen3.5-35B-A3B`

如果使用 Hugging Face CLI，两个数据集可以按下面方式下载：

```bash
hf download --repo-type dataset BlueZeros/AgentEHR-Bench --local-dir data/AgentEHR-Bench
hf download --repo-type dataset BlueZeros/EHR-Bench --local-dir data/EHR-Bench
```

当前脚本默认依赖：

- `scripts/run/run_mcp_server.sh` 使用 `data/AgentEHR-Bench/MIMICIVAgentBench`
- `openresearcher_ehr/run_test_subset.sh` 使用 `data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json`
- `scripts/run/run_vllm_server_3_5.sh` 使用 `models/Qwen3.5-35B-A3B`

当前脚本默认依赖仓库外的 `../miniconda3/bin/python3.13` 及对应环境。建议直接使用这个解释器安装 `requirements.txt` 中的依赖。

```bash
../miniconda3/bin/python3.13 -m pip install --upgrade pip
../miniconda3/bin/python3.13 -m pip install -r requirements.txt
```

如果你使用的不是 `../miniconda3/bin/python3.13` 这套环境，需要确保后续执行脚本时 `python` 和 `vllm` 都能正确解析到已安装依赖的环境中。

## 2. 启动 MCP 服务

打开一个新的终端后运行：

```bash
bash scripts/run/run_mcp_server.sh
```

默认情况下该脚本会使用：

- `GPU_ID=0`
- `PORT=5103`
- 数据路径 `data/AgentEHR-Bench/MIMICIVAgentBench`

如果需要自定义 GPU 或端口，可以显式传参：

```bash
bash scripts/run/run_mcp_server.sh 0 5103
```

## 3. 启动 vLLM 服务

打开第二个终端后运行：

```bash
PATH="../miniconda3/bin:${PATH}" bash scripts/run/run_vllm_server_3_5.sh
```

默认情况下该脚本会使用：

- `CUDA_DEVICES=0,1,2,3,4,5,6,7`
- `PORT=4000`
- 模型路径 `models/Qwen3.5-35B-A3B`

如果需要显式指定 GPU 和端口，可以这样运行：

```bash
PATH="../miniconda3/bin:${PATH}" bash scripts/run/run_vllm_server_3_5.sh 0,1,2,3,4,5,6,7 4000
```

## 4. 启动 Evaluation

打开第三个终端后运行。为了让输出目录稳定落在评测目录下，先切换到 `openresearcher_ehr`：

```bash
cd openresearcher_ehr
PATH="../../miniconda3/bin:${PATH}" bash run_test_subset.sh
```

该脚本默认会连接：

- MCP 服务地址 `http://127.0.0.1:5103/mcp`
- vLLM 服务地址 `http://127.0.0.1:4000`
- 评测数据 `data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json`

评测结果会输出到当前工作目录下的 `subset_500_<model_slug>` 目录中。按照上面的 `cd` 方式运行时，结果目录会位于：

```bash
openresearcher_ehr/subset_500_<model_slug>
```

## 5. 推荐执行顺序

按下面顺序依次执行：

1. 使用 `requirements.txt` 搭建环境。
2. 启动 `scripts/run/run_mcp_server.sh`。
3. 启动 `scripts/run/run_vllm_server_3_5.sh`。
4. 运行 `openresearcher_ehr/run_test_subset.sh` 开始评测。

只有在 MCP 服务和 vLLM 服务都已经正常启动后，再执行 evaluation。
