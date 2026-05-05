# `cases/` — per-case evidence bundles

Each subfolder is one case study (9 in total). Every case folder contains
the complete evidence chain for that case, so it can be audited or
re-rendered without touching the rest of the report.

## Naming convention

```
cases/
├── case1_lengthofstay/
├── case2_mm_phenotyping/
├── case3_pyxis/
├── case4_inpatient_mortality/
├── case5_ed_hospitalization/
├── case6_mm_decompensation/
├── case7_mm_mortality/
├── case8_labevents/
└── case9_next_event/
```

Slug prefix (`case<N>_...`) matches the ordering in `findings_report.html`.
`mm_` in the slug = multimodal case (includes a linked chest-X-ray image).
All other cases are text-only.

## What lives in each `cases/<slug>/` folder

| File | Generator | What it is |
|---|---|---|
| `case.md` | `scripts/build_report.py` | **The published case study.** Reads all other files in this folder and produces the summary: tool-call histogram, vital calls with full tool results, final agentic `ehr.think` synthesis, analyzer's "why reasoning went wrong" block, and the full reasoning-mode reply. This is the file linked from `findings_report.html`. |
| `trajectory.md` | `scripts/_dump_trajectories.py` | Full ClinSeek trajectory: every tool call in order with args and (truncated) tool results, plus the final `ehr.finish` payload. Fed verbatim to the analyzer. |
| `reasoning.md` | `scripts/_dump_trajectories.py` | Full reasoning-mode run: the rendered user prompt the model saw and the assistant's full reply. Fed verbatim to the analyzer for win-cases. |
| `vital_analysis.json` | `scripts/analyze_trajectory.py` (Claude Opus 4.6 on Bedrock) | Structured analyzer output. For win-cases contains `vital_calls[]` + `reasoning_failure_analysis`. For loss-cases contains `misleading_calls[]`, `could_have_saved_it[]`, and `root_cause_or_summary`. |
| `analyzer.log` | `scripts/_run_analyzer.sh` | stdout / stderr of the analyzer run — useful if the JSON ever fails to parse. |
| `*.jpg` / `*.png` | `scripts/_dump_trajectories.py` (MM cases only) | The linked chest-X-ray image(s) the multimodal case pipeline saw. These are the same files the `image.chest_xray_*` tools received as input. |

## How the files fit together

```
1. _dump_trajectories.py
     reads: results/.../messages
     writes: cases/<slug>/{trajectory.md, reasoning.md, *.jpg}

2. _run_analyzer.sh → analyze_trajectory.py
     reads: cases/<slug>/{trajectory.md, reasoning.md}
     writes: cases/<slug>/{vital_analysis.json, analyzer.log}

3. build_report.py
     reads: cases/<slug>/vital_analysis.json + raw results.jsonl
     writes: cases/<slug>/case.md  (and the top-level findings_report.html)
```

## Regenerating a single case

```bash
cd scripts/
# 1. refresh trajectories (all 9 cases)
python _dump_trajectories.py
# 2. re-run Claude-Opus vital-call analyzer (all 9)
./_run_analyzer.sh
# 3. re-render case markdowns + top-level report
python build_report.py
```

If you only want to iterate on the rendered summary you can re-run step 3
alone — the underlying `trajectory.md` / `reasoning.md` / `vital_analysis.json`
won't change.

## Case-type guide

| Group | Cases | Meaning |
|---|---|---|
| **ClinSeek wins (text-only risk_prediction)** | case1, case4, case5 | `case.md` starts with "✅ ClinSeek / ❌ Reasoning" header; `vital_analysis.json` lists `vital_calls` + `reasoning_failure_analysis`. |
| **ClinSeek wins (multimodal)** | case2, case6, case7 | Same shape as above, plus a linked CXR jpg in the folder. |
| **ClinSeek losses (decision_making)** | case3, case8, case9 | `case.md` starts with "❌ ClinSeek / ✅ Reasoning" header; `vital_analysis.json` lists `misleading_calls`, `could_have_saved_it`, and a `root_cause_or_summary`. |
