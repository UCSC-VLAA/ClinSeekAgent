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

```bash
cd data/EHR-Bench
python3 reverse_match.py
```

脚本中的路径变量：

```python
MIMIC_DIR = "/home/efs/zlt/datasets/MIMIC-IV/mimic_iv"   # MIMIC-IV 数据根目录
BENCH_DIR = "/home/efs/zlt/deepresearch/data/EHR-Bench"   # EHR-Bench 数据目录
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
