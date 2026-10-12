# Resources

This file records the external artifacts needed to reproduce or inspect the
ClinSeekAgent release. Data, model, and result artifacts are not stored in
this Git repository.

## Artifact Inventory

| Category | Location | Access |
| --- | --- | --- |
| Code | this GitHub repository | public |
| Benchmark manifests | [`UCSC-VLAA/ClinSeek-Bench`](https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Bench) | Hugging Face dataset |
| Model weights | [`UCSC-VLAA/ClinSeek-35B-A3B`](https://huggingface.co/UCSC-VLAA/ClinSeek-35B-A3B) | Hugging Face model |
| Evaluation results | [`UCSC-VLAA/ClinSeek-Evaluation-Results`](https://huggingface.co/datasets/UCSC-VLAA/ClinSeek-Evaluation-Results) | Hugging Face dataset |
| SFT training trajectories | not released | — |
| Patient databases, CXR images, reports | rebuilt locally from MIMIC-IV / MIMIC-CXR (PhysioNet credentialed access) | see the data preparation guides |
| AgentEHR-Bench | [`BlueZeros/AgentEHR-Bench`](https://huggingface.co/datasets/BlueZeros/AgentEHR-Bench) | `scripts/download_agentehr_bench.sh` |
| EHR-Bench (upstream of ClinSeek-Bench-Text) | [`BlueZeros/EHR-Bench`](https://huggingface.co/datasets/BlueZeros/EHR-Bench) | needed only to restore `input` / `question` |

## ClinSeek-Bench

```text
ClinSeek-Bench/
+-- README.md
+-- ClinSeek-Bench_text.json         # 1,800 rows, ClinSeek-Bench-Text
+-- ClinSeek-Bench_multimodal.jsonl  # 989 rows, ClinSeek-Bench-MM
```

`ClinSeek-Bench_text.json` keeps `qid`, `subject_id`, `hadm_id`,
`prediction_time`, `latest_event_time`, `task`, `task_type`, `category`,
`instruction`, `candidates`, `label`, `output`, `task_info`,
`target_hadm_id`, `target_stay_id`, `source_file`, `source_idx`. It does not
redistribute the upstream EHR-Bench serialization (`input`) or the curated
prompt built from it (`question`); every row maps 1:1 to
`BlueZeros/EHR-Bench` through `source_file` + `source_idx`, and
`scripts/data_build/restore_ehr_bench_inputs.py` restores both fields.

`ClinSeek-Bench_multimodal.jsonl` keeps the question, labels, task category,
patient identifiers, prediction time, linked CXR study / image ids, image and
report paths, and the trusted scope metadata (`scope`, `modalities`,
`study_ids`, `dicom_ids`, `snapshot_spec`, `leakage_policy_version`). The
rendered EHR context of the Curated Input setting (`input_text`) is produced
locally by `scripts/data_build/materialize_multimodal_strict_manifest.py`.

| Split | Questions | Categories | Local assets you build |
| --- | ---: | ---: | --- |
| ClinSeek-Bench-Text | 1,800 | 13 | per-patient SQLite DBs (`scripts/patient_event2db.py`) |
| ClinSeek-Bench-MM | 989 | 6 | EHRXQA / MedMod patient DBs, linked CXR images and reports (`scripts/data_build/`) |

## Code Entry Points

| Purpose | Entry point |
| --- | --- |
| EHR MCP server / image MCP server | `scripts/run_ehr_mcp.sh`, `scripts/run_image_mcp.sh` |
| ClinSeekAgent, text / multimodal | `scripts/run_text_eval.sh`, `scripts/run_mm_eval.sh` (`clinseekagent/run_text.py`, `run_multimodal.py`) |
| Curated Input / Full-Record Input | `clinseekagent/run_text_curated.py`, `run_multimodal_curated.py` (`--corpus-root --view`) |
| Curated Input corpus | `scripts/clean_data/build_evidence_corpus.py` |
| All four inference modes | `scripts/run_four_modes.sh`, `scripts/score_four_modes.sh` |
| Forced final-answer round | `scripts/force_final_answer.py` |
| Scoring | `clinseekagent/scoring/score_text.py`, `score_multimodal.py`, `report_by_category.py`; gate: `scripts/check_results_complete.py` |
| Local model serving | `scripts/run_vllm_server.sh` |
| AgentEHR-Bench | `scripts/download_agentehr_bench.sh`, `scripts/build_agentehr_sample.py`, `scripts/run_agentehr_bench.sh` |
| SFT | `verl/examples/sft/clinseek/prepare_clinseek_data.py`, `scripts/train_sft.sh`, `verl/examples/sft/clinseek/convert_dist_ckpt_to_hf.py` |

## Download Commands

```bash
hf download UCSC-VLAA/ClinSeek-Bench --repo-type dataset --local-dir data/ClinSeek-Bench
hf download UCSC-VLAA/ClinSeek-35B-A3B --local-dir models/ClinSeek-35B-A3B
hf download UCSC-VLAA/ClinSeek-Evaluation-Results --repo-type dataset \
  --local-dir results/ClinSeek-Evaluation-Results
```

## Release Boundary

The following stay outside Git:

- raw MIMIC-IV tables, MIMIC-CXR images and reports;
- generated patient SQLite databases and rendered EHR context;
- model weights and training checkpoints;
- training trajectories and raw tool observations;
- experiment logs and local infrastructure paths.
