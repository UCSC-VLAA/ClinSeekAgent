# findings_20260501 — ClinSeek vs Reasoning analysis

This folder contains the full analysis comparing our clinical-evidence-seeking
agent **ClinSeek** (multi-turn tool use on the raw EHR + CXR images) against a
**reasoning-mode** baseline (single-turn prompt on a rule-based pre-rendered
EHR context), across 13 host models on text-only and multimodal clinical
tasks.

## Quick start

Open `findings_report.html` in a browser. It contains:
1. Main result (ClinSeek vs Reasoning per model, text-only L2 table).
2. Agentic wins — risk_prediction (L3 heatmap) + multimodal (L4 table + per-model lift).
3. Agentic failures — decision_making L3 losers + failure-mode analysis.

All tables + the figure are also available as standalone files (see
`table1_ehr_L2.html`, `table3_mm_L4_multimodal.html`, `images/`). Every
HTML table and the figure also have a matching raw CSV under `data/`.

## Folder layout

```
findings_20260501/
├── findings_report.html              ← main narrative (OPEN THIS)
├── table1_ehr_L2.html                ← standalone Table 1 (text-only L2)
├── table3_mm_L4_multimodal.html      ← standalone Table 3 (multimodal L4)
├── README.md                         ← this file
│
├── images/                           ← all generated figures
│   └── fig_ehrbench_L3_delta.png     ← text-only L3 Δ heatmap with L2 bands
│
├── data/                             ← raw CSVs behind every table and figure
│   ├── table1_ehr_L2.csv             ← raw numbers behind Table 1
│   ├── fig_ehrbench_L3_delta.csv     ← raw numbers behind the heatmap (long format)
│   ├── table3_mm_L4_multimodal.csv   ← raw numbers behind Table 3
│   ├── mm_overall_lift.csv           ← per-host MM overall lift (§2.2 inline table)
│   └── ehrbench_decision_losers.csv  ← 4 decision-making L3s where every host loses (§3.3)
│
├── scripts/                          ← all code that produces the report
│   ├── build_report.py               ← top-level regenerator (tables + figure + CSVs + cases + HTML)
│   ├── plot_fig_from_csv.py          ← regenerate the heatmap from data/fig_ehrbench_L3_delta.csv alone
│   ├── _dump_trajectories.py         ← emits cases/<slug>/{trajectory,reasoning}.md + CXR jpgs
│   ├── analyze_trajectory.py         ← Claude Opus 4.6 on Bedrock; picks vital tool calls
│   └── _run_analyzer.sh              ← runs analyze_trajectory.py on all 9 cases in parallel
│
└── cases/                            ← 9 case studies, one subfolder each
    ├── README.md                     ← guide to what lives in each case folder
    ├── case1_lengthofstay/           ← ClinSeek wins (text-only risk_prediction)
    ├── case2_mm_phenotyping/         ← ClinSeek wins (multimodal) — includes CXR image
    ├── case3_pyxis/                  ← ClinSeek loss (decision_making)
    ├── case4_inpatient_mortality/    ← ClinSeek wins (text-only risk_prediction)
    ├── case5_ed_hospitalization/     ← ClinSeek wins (text-only risk_prediction)
    ├── case6_mm_decompensation/      ← ClinSeek wins (multimodal) — includes CXR image
    ├── case7_mm_mortality/           ← ClinSeek wins (multimodal) — includes CXR image
    ├── case8_labevents/              ← ClinSeek loss (decision_making)
    └── case9_next_event/             ← ClinSeek loss (decision_making)
```

## What each subfolder is for

- **`images/`** — every generated figure. Currently just the L3 delta
  heatmap; future figures land here too. Scripts write here via
  `IMAGES = OUT / "images"`.

- **`data/`** — one CSV per table / figure in the report. All F1 values
  are in percentage points. Every file here is a direct regeneration
  target of `build_report.py`; the heatmap CSV is also consumed
  standalone by `scripts/plot_fig_from_csv.py` to rebuild the figure
  without touching the live `results/` tree.

- **`scripts/`** — every Python and shell script. Each script is
  self-contained: it locates sibling scripts via its own `$(dirname)` and
  writes outputs to the right subfolder (`images/`, `data/`,
  `cases/<slug>/`, or the top level for the HTML + tables).

- **`cases/`** — the qualitative evidence. One subfolder per case; every
  subfolder contains the complete chain of inputs and outputs for that
  case (raw trajectory → reasoning reply → analyzer JSON → rendered
  `case.md`). See `cases/README.md` for the per-file breakdown.

## Regenerating from scratch

```bash
cd scripts/
python _dump_trajectories.py     # cases/<slug>/{trajectory,reasoning}.md + CXR jpgs
./_run_analyzer.sh               # cases/<slug>/vital_analysis.json (Claude Opus on Bedrock)
python build_report.py           # data/*.csv, images/fig_*.png, table*.html,
                                 # cases/<slug>/case.md, findings_report.html
```

To rebuild only the heatmap from the stored CSV (no access to the live
`results/` tree needed):

```bash
python scripts/plot_fig_from_csv.py   # reads data/fig_ehrbench_L3_delta.csv → images/fig_ehrbench_L3_delta.png
```

The underlying benchmark results live at
`/fsx-shared/juncheng/EHR/openresearcher_ehr/results/hierarchical/` — the
scripts read from there and write into this folder only.

## Glossary

| Term | Meaning |
|---|---|
| **ClinSeek** | Our proposed clinical evidence-seeking agent — multi-turn MCP tool calls over raw EHR + CXR image/report tools (multimodal). |
| **Reasoning mode** | One-shot baseline — a rule-based extractor pre-renders the EHR timeline into the user prompt; the model answers in one turn. |
| **host model** | The underlying LLM driving either paradigm (Claude Opus 4.6, Sonnet 4.6, Qwen3.5-35B-A3B, GLM-4.7, Kimi K2.5, Qwen3-VL-235B, MiniMax M2.5, gpt-oss-120b, Gemma-4-26B-A4B-it, MedGemma-27B, EHR-R1-8B, EHR-R1-72B). |
| **Δ** | `ClinSeek F1 − Reasoning F1`, in percentage points. Positive = ClinSeek wins. |
| **L1 / L2 / L3 / L4** | Hierarchical task grouping: modality → overall-task → semantic-task → fine task. |
