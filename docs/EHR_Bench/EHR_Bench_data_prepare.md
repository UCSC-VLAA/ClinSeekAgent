# 快速准备（推荐）

最简单的方式是直接下载我们已经打包好的 EHR-Bench 数据（含匹配后的 JSON 和全部病人 db），解压后即可用：

```bash
# 从仓库根目录执行
huggingface-cli download Letian2003/Bb21385 \
    --repo-type dataset \
    --local-dir data

# 解压所有压缩文件
for ext in zip tar.gz csv.gz; do
    find data -name "*.${ext}" -execdir sh -c '
        case "$1" in
            *.zip)    unzip -o "$1" ;;
            *.tar.gz) tar -xzf "$1" ;;
            *.csv.gz) gunzip -f "$1" ;;
        esac' _ {} \;
done
```

完成后 `data/EHR-Bench/` 下即包含匹配后的 benchmark JSON 和 `database/patient_*.db`，可直接用于 MCP server（`--data_path data/EHR-Bench`）。

如果需要自己从 MIMIC-IV 原始数据走完整流程（反向匹配 → 生成 db），请参考下面各节。

---

# MIMIC-IV 原始数据准备

本节记录如何下载并准备 MIMIC-IV 原始数据，作为后续反向匹配 `subject_id` 和生成病人 db 的输入。

---

## 1. 数据来源

MIMIC-IV 的主模块（hosp / icu / note）和 ED 模块分别托管在两个 HuggingFace 仓库：

| 仓库 | 下载目标目录 |
|------|------------|
| https://huggingface.co/datasets/Letian2003/MA49234 | `data/MIMIC-IV/mimic_iv` |
| https://huggingface.co/datasets/Letian2003/ry03890 | `data/MIMIC-IV/mimic_iv_ed` |

---

## 2. 下载

从仓库根目录执行：

```bash
# 1) hosp / icu / note
huggingface-cli download Letian2003/MA49234 \
    --repo-type dataset \
    --local-dir data/MIMIC-IV/mimic_iv

# 2) ed
huggingface-cli download Letian2003/ry03890 \
    --repo-type dataset \
    --local-dir data/MIMIC-IV/mimic_iv_ed
```

---

## 3. 解压

将两个目录下所有压缩文件解压（仍从仓库根目录执行）：

```bash
for d in data/MIMIC-IV/mimic_iv data/MIMIC-IV/mimic_iv_ed; do
    find "$d" -name "*.zip"    -execdir unzip -o {} \;
    find "$d" -name "*.tar.gz" -execdir tar -xzf {} \;
    find "$d" -name "*.csv.gz" -exec gunzip -f {} \;
done
```

---

## 4. 归并 ED 模块

将 ED 子目录移入 `mimic_iv/` 下，使其与 `hosp/` / `icu/` / `note/` 并列：

```bash
mv data/MIMIC-IV/mimic_iv_ed/mimic-iv-ed/2.2/ed data/MIMIC-IV/mimic_iv/
```

最终目录结构：

```
data/MIMIC-IV/mimic_iv/
├── hosp/
├── icu/
├── note/
└── ed/
```

完成后，`data/MIMIC-IV/mimic_iv` 即为 MIMIC-IV 根目录，可直接作为下文反向匹配（`MIMIC_DIR`）和病人 db 生成（`--root_path`）脚本的输入。

---

# EHR-Bench 数据准备：反向匹配 MIMIC-IV 病人标识

本文记录如何将 EHR-Bench（由 EHR-R1 论文发布）中去标识化的 benchmark 数据反向匹配回 MIMIC-IV 原始数据集，恢复 `subject_id` 和 `hadm_id`。

---

## 1. 背景

EHR-Bench 数据集基于 MIMIC-IV 构建，但作者在发布时移除了 `subject_id`、`hadm_id` 等病人标识字段（详见 EHR-Bench README）：

> To prevent the leakage of native data information within the MIMIC-IV dataset, we removed information such as subject_id, harm_id, and other details that might link to the original MIMIC-IV data.

然而，benchmark 中保留了精确到秒的时间戳以及人口学信息（性别、年龄），这些信息足以唯一定位原始病人。

---

## 2. 匹配原理

每条 benchmark 数据的 `input` 字段中包含：

- **人口学信息**：`Anchor_Age`、`Gender`（来自 `## Patient Demographics` 段落）
- **带时间戳的临床事件**：`## Transfers [2115-11-02 13:59:00]`、`## EDstays [...]` 等

匹配步骤：

1. **候选人筛选**：用 `(gender, anchor_age)` 从 MIMIC-IV `patients.csv` 中筛选候选 `subject_id`（通常约 2000–3000 人）
2. **时间戳精确匹配**：用 benchmark 中各临床事件段落的时间戳，与 MIMIC-IV 原始表中的时间字段逐一比对，找到唯一匹配

由于时间戳精确到秒，加上人口学约束，绝大多数条目都能唯一确定病人。

---

## 3. 匹配策略（按优先级排序）

脚本依次尝试以下策略，一旦找到唯一匹配即返回：

| 优先级 | Benchmark 段落 | MIMIC-IV 匹配表 | 匹配字段 |
|--------|---------------|-----------------|---------|
| 1 | `## Transfers [ts]` | `transfers.csv` | `intime` |
| 2 | `## EDstays [ts]` | `transfers.csv` / `admissions.csv` | `intime` (eventtype=ED) / `edregtime` |
| 3 | `## Admissions [ts]` | `admissions.csv` | `admittime` |
| 4 | `## Provider Order Entry [ts]` | `poe.csv` | `ordertime` |
| 5 | `## Pharmacy [ts]` | `pharmacy.csv` | `entertime` |
| 6 | `## Prescriptions [ts]` | `prescriptions.csv` | `starttime` |
| 7 | `## Electronic Medicine Administration Record [ts]` | `emar.csv` | `charttime` |
| 8 | 任意段落的前几个时间戳 | `transfers.csv` | `intime`（兜底） |

对于每个策略，脚本从候选 `subject_id` 列表中查找时间戳完全匹配的记录。如果恰好有一个匹配，则确认为该病人；如果有多个匹配或零匹配，则继续尝试下一个策略。

---

## 4. 涉及的文件

### 输入

| 文件 | 说明 |
|------|------|
| `data/EHR-Bench/ehr_bench_decision_making_subset_format.json` | Decision-Making 任务，13,500 条 |
| `data/EHR-Bench/ehr_bench_risk_prediction_subset_format.json` | Risk-Prediction 任务，7,721 条 |

### MIMIC-IV 参考表

均位于 MIMIC-IV 数据集目录下（路径通过脚本中 `MIMIC_DIR` 变量配置）：

- `hosp/patients.csv` — 人口学信息（gender, anchor_age）
- `hosp/transfers.csv` — 转科记录（eventtype, intime, hadm_id）
- `hosp/admissions.csv` — 入院记录（admittime, edregtime, hadm_id）
- `hosp/poe.csv` — 医嘱（ordertime, hadm_id）
- `hosp/pharmacy.csv` — 药房记录（entertime, hadm_id）
- `hosp/prescriptions.csv` — 处方记录（starttime, hadm_id）
- `hosp/emar.csv` — 用药管理记录（charttime, hadm_id）

### 输出

| 文件 | 说明 |
|------|------|
| `data/EHR-Bench/ehr_bench_decision_making_subset_format_matched.json` | 匹配后的 Decision-Making 数据 |
| `data/EHR-Bench/ehr_bench_risk_prediction_subset_format_matched.json` | 匹配后的 Risk-Prediction 数据 |

输出文件与输入格式完全一致，仅在每条记录中填充了 `subject_id` 和 `hadm_id`（部分记录 `hadm_id` 为 `null`，因为 MIMIC-IV 原始数据中该次就诊本身无 `hadm_id`，例如 ED 未住院的情况）。无法匹配的条目被丢弃。

---

## 5. 运行方式

从仓库根目录运行：

```bash
python3 helper/reverse_match.py
```

脚本中的路径变量（均相对仓库根目录）：

```python
MIMIC_DIR = "data/MIMIC-IV/mimic_iv"   # MIMIC-IV 数据根目录
BENCH_DIR = "data/EHR-Bench"           # EHR-Bench 数据目录
```

运行耗时约 5–10 分钟（主要花在加载 `poe.csv`、`emar.csv` 等大表上）。

---

## 6. 匹配结果

| 数据集 | 原始条目 | 匹配成功 | 匹配率 | 丢弃 |
|--------|---------|---------|--------|------|
| Decision-Making | 13,500 | 13,471 | 99.8% | 29 |
| Risk-Prediction | 7,721 | 7,716 | 99.9% | 5 |

匹配成功的条目中：

- **Decision-Making**：13,471 条有 `subject_id`，其中 12,682 条有 `hadm_id`
- **Risk-Prediction**：7,716 条有 `subject_id`，其中 7,011 条有 `hadm_id`

---

## 7. 注意事项

1. **时间戳未做二次偏移**：MIMIC-IV 本身对日期做了随机偏移（de-identification），但 EHR-Bench 中的时间戳与 MIMIC-IV 偏移后的时间完全一致，未做额外处理，因此可以直接匹配。
2. **`hadm_id` 为空的情况**：部分匹配只能定位到 `subject_id` 而无 `hadm_id`，这是因为 MIMIC-IV 原始 `transfers` 表中某些记录（如 ED 就诊未转入住院）本身无关联的 `hadm_id`。
3. **丢弃的条目**：34 条未匹配的数据（占总量 0.16%）多为非急诊入院且缺少足够时间戳特征的病例，不影响 benchmark 的整体使用。

---

# EHR-Bench 数据准备：生成病人 SQLite 数据库

本节记录如何根据匹配后的 benchmark JSON，使用 `helper/patient_event2db.py` 从 MIMIC-IV 原始 CSV 批量生成**每位病人一个 `.db` 文件**，供 MCP server (`src/run_mcp_server.py`) 加载并对 agent 提供工具接口。

---

## 1. 功能概述

`helper/patient_event2db.py` 做的事：

1. 从 `--data_file_path` 指定的 JSON 中收集所有 `subject_id`
2. 对比 `--output_path` 中已有的 `patient_<subject_id>.db`，**自动跳过已生成的病人**
3. 扫描 `--root_path` 下的 `hosp`、`icu`、`note`、`ed` 等子目录中的所有 `.csv` / `.csv.gz`，按 `subject_id` 过滤并分组
4. 做预处理（`preprocess_subject_dict`）：
   - 为 `diagnoses_icd` 补上 `charttime`（取对应 `admissions.dischtime - 1min`）
   - 为 ed `diagnosis` 补上 `charttime`（取对应 `edstays.outtime - 1min`）
   - 将 `note/discharge.csv` 中 `text` 字段中 "Physical Exam" 之前的部分挂到 `admissions.text`
   - **若某病人任一 `admission` 没能匹配到 discharge text，则该病人被整体丢弃**
5. 每个 `subject_id` 写入一个 SQLite `patient_<subject_id>.db`，每张原始 CSV 表作为一张 table（所有列存为 `TEXT`）

---

## 2. 输入 / 输出

### 输入

| 参数 | 示例（相对仓库根目录） | 说明 |
|------|----------------------|------|
| `--root_path` | `data/MIMIC-IV/mimic_iv` | MIMIC-IV 原始数据根目录，下含 `hosp/`、`icu/`、`note/`、`ed/` |
| `--data_file_path` | `data/EHR-Bench/ehr_bench_merged_filtered.json` | 匹配后的 benchmark JSON，每条记录含 `subject_id` |
| `--data_dir_path` | （可选）某目录 | 目录下所有 `.json` 都会被合并读取 `subject_id` |
| `--subject_id` | （可选）单个整数 | 仅为该病人生成 db |
| `--data_dirs` | 默认 `ed hosp icu note` | 要扫描的子目录列表 |

三种 subject 来源 (`--subject_id` / `--data_file_path` / `--data_dir_path`) 互斥，都不指定则处理全部病人。

### 输出

| 参数 | 示例（相对仓库根目录） |
|------|----------------------|
| `--output_path` | `data/EHR-Bench/database` |

输出目录下每位病人生成一个文件：

```
data/EHR-Bench/database/
├── patient_10000108.db
├── patient_10025995.db
└── ...
```

每个 db 内的表名与 MIMIC-IV 的 CSV 文件名一致（如 `admissions`、`diagnoses_icd`、`labevents`、`transfers`、`radiology` 等）。

---

## 3. 运行方式

从仓库根目录运行：

```bash
# 推荐：放后台，log 落盘
nohup python helper/patient_event2db.py \
    --root_path data/MIMIC-IV/mimic_iv \
    --output_path data/EHR-Bench/database \
    --data_file_path data/EHR-Bench/ehr_bench_merged_filtered.json \
    > logs/ehr_bench_db_gen_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
```

增量运行：再次执行同一命令，脚本会根据 `output_path` 下现有的 `patient_*.db` 自动跳过，只处理缺失的 `subject_id`。

只生成单个病人（调试用）：

```bash
python helper/patient_event2db.py \
    --root_path data/MIMIC-IV/mimic_iv \
    --output_path data/EHR-Bench/database \
    --subject_id 10000108
```

---

## 4. 资源与耗时

- **输入规模**：MIMIC-IV 原始 CSV 总计约 90 GB（最大的是 `icu/chartevents.csv` 40 GB、`hosp/labevents.csv` 18 GB）
- **内存占用**：脚本先把**所有目标病人的数据**按 `subject_id` 分组全量驻留内存，再统一写入 db。以 8000+ 病人为例，峰值 RSS 约 30–60 GB（视病人数据量而定）。
- **耗时**：一次全量运行大概需要数小时，主要花在读取大 CSV 上。
- **增量生成**：只为缺失的病人生成（例如只剩 100 人）时，CSV 仍需全量扫描一遍，耗时并不会线性下降——只有写 db 的部分变快。
- **stdout 缓冲**：用 `>` 重定向到文件时，Python 默认块缓冲，启动后几分钟日志可能为空，属正常现象。若需实时日志，可在命令前加 `PYTHONUNBUFFERED=1`，或用 `python -u`。

---

## 5. 与下游的衔接

生成的 db 由 MCP server 通过 `load_ehr` 工具加载，供 agent 查询：

```bash
# MCP server 启动（默认从此目录下读取 patient_<sid>.db）
CUDA_VISIBLE_DEVICES=0 python src/run_mcp_server.py \
    --mode http \
    --host 127.0.0.1 \
    --port 5103 \
    --data_path data/EHR-Bench/database
```

随后通过 `openresearcher_ehr/eval_ehrbench.sh` 跑 agent 评测即可（脚本里 `EHR_MCP_URL` 默认指向 `http://127.0.0.1:5103/mcp`）。

---

## 6. 常见问题

1. **脚本里打印 "Directory not found: .../ed"**：MIMIC-IV 完整版有 `ed/` 子目录，但当前本地数据集只下载了 `hosp/`、`icu/`、`note/`。无 `ed/` 时，`diagnoses_icd` / `diagnosis` 的 `charttime` 补全逻辑会 fallback 到空字符串，不影响主流程。
2. **`admissions.text` 无匹配 discharge**：若某 admission 在 `note/discharge.csv` 中找不到对应 `hadm_id`，脚本会把 `admission['text']` 置为空字符串（保留该病人）。早期版本会直接丢弃该病人，现已改为保留。
3. **列全是 `TEXT`**：建表时所有列都被定义为 `TEXT`（见 `save_to_db` 中的 `columns_def`），数值/时间比较请在 SQL 中显式 `CAST`，或在 agent 工具层做类型转换。
