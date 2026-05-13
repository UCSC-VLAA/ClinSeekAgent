# Tool-call distribution & success rate — Qwen3.5-35B-A3B baseline vs SFT

Sample: the aligned 500 qids (5 tasks × 100). A tool result is classified as
an **error** if it starts with `Error:`, `Traceback`, `Exception:`,
`Failed to`, `Fetch error`, `Error fetching URL`, or `Error during search`.
Any other tool result — including "No records found" — counts as success.

## Run-level aggregates

| Run-level stat | Baseline | SFT |
|---|---:|---:|
| Total tool calls | 33,043 | 31,446 |
| Total error results | 1,478 | 1,755 |
| **Overall success rate** | **95.53%** | **94.42%** |
| Tool calls per run — mean | 66.1 | 62.9 |
| Tool calls per run — median | 55 | 54 |
| Tool calls per run — p90 | 134 | 105 |
| Tool calls per run — max | 200 (capped) | 291 |
| Errors per run — mean | 2.96 | 3.51 |
| Errors per run — median | 0 | 3 |
| Errors per run — p90 | 2 | 6 |
| Errors per run — max | 179 | 10 |

The overall per-call success rates are similar (~95%), but the *shape* is
very different: baseline runs are bimodal (most have 0 errors; a few blow up
— one run logged 179 errors), while the SFT model spreads errors more evenly
across runs (median 3, max 10).

## Tool-call mix (share of each model's total calls)

| Tool | Baseline calls | Baseline share | SFT calls | SFT share |
|---|---:|---:|---:|---:|
| ehr.get_candidates_by_keyword | 16,475 | 49.9% | 13,403 | 42.6% |
| browser.search | 5,622 | 17.0% | 1,173 | 3.7% |
| ehr.get_candidates_by_semantic_similarity | 4,161 | 12.6% | 1,348 | 4.3% |
| ehr.run_sql_query | 649 | 2.0% | 3,932 | 12.5% |
| ehr.get_latest_records | 320 | 1.0% | 3,328 | 10.6% |
| ehr.get_records_by_time | 1,400 | 4.2% | 1,959 | 6.2% |
| ehr.think | 266 | 0.8% | 2,207 | 7.0% |
| browser.open | 870 | 2.6% | 1,048 | 3.3% |
| ehr.get_records_by_keyword | 57 | 0.2% | 509 | 1.6% |
| ehr.load_ehr | 495 | 1.5% | 500 | 1.6% |
| ehr.get_table_names | 464 | 1.4% | 500 | 1.6% |
| ehr.finish | 421 | 1.3% | 498 | 1.6% |
| browser.find | 70 | 0.2% | 355 | 1.1% |
| ehr.get_candidates_by_fuzzy_matching | 0 | 0.0% | 328 | 1.0% |
| ehr.get_records_by_value | 173 | 0.5% | 148 | 0.5% |
| ehr.get_table_description | 1,166 | 3.5% | 0 | 0.0% |
| ehr.get_column_names | 374 | 1.1% | 114 | 0.4% |
| ehr.get_unique_values | 60 | 0.2% | 40 | 0.1% |
| ehr.get_event_counts_by_time | 0 | 0.0% | 54 | 0.2% |

Big shifts (SFT vs baseline):
- Browser usage collapses (`browser.search` 17.0% → 3.7%; `browser.open` 2.6% → 3.3%). Overall web traffic drops from 19.8% to 8.1% of calls.
- Direct SQL (`ehr.run_sql_query`) grows 6× in share (2.0% → 12.5%) and the semantic-similarity candidate search shrinks in share by ~3×.
- Timeline queries surge: `get_latest_records` 1.0% → 10.6%; `get_records_by_time` 4.2% → 6.2%; plus the SFT adds `get_event_counts_by_time` and `get_candidates_by_fuzzy_matching` that the baseline never calls.
- Explicit reasoning turns (`ehr.think`) grow ~9× in share (0.8% → 7.0%).
- Schema probing shrinks: `get_table_description` disappears; `get_column_names` drops ~3×.

## Per-tool error rate

| Tool | Baseline err% | SFT err% |
|---|---:|---:|
| ehr.get_candidates_by_keyword | 0.0% | 0.0% |
| ehr.get_candidates_by_semantic_similarity | 0.0% | 0.0% |
| ehr.run_sql_query | 0.0% | 0.0% |
| ehr.think | 0.0% | 0.0% |
| ehr.load_ehr / get_table_names / finish | 0.0% | 0.0% |
| browser.search | **18.0%** | **0.4%** |
| browser.open | 18.9% | 3.1% |
| ehr.get_latest_records | 13.8% | **39.1%** |
| ehr.get_records_by_time | 15.3% | 18.1% |
| ehr.get_records_by_value | 0.0% | 16.9% |
| ehr.get_records_by_keyword | 0.0% | 1.6% |
| ehr.get_unique_values | 68.3% | 67.5% |

- Baseline's error volume is dominated by **browser calls** (1,043 errors from `browser.search` + 171 from `browser.open` = 82% of all baseline errors). These are `Error during search for …` / `Error fetching URL …` messages.
- SFT's error volume is dominated by **`ehr.get_latest_records`** (1,300 errors = 74% of all SFT errors). The repeated messages are variants of `Error: No timestamp column found in table '{patients,triage,drgcodes,discharge_detail,radiology_detail,poe_detail,emar_detail}'. Cannot find the latest records.` — i.e., the SFT model very confidently calls `get_latest_records` on tables whose schema has no timestamp column and eats the error.

## Read

Distilling from the proprietary teacher taught the model a **different tool
policy**, not a safer one. The SFT model:

1. drops most browser search / semantic-similarity exploration,
2. moves toward direct SQL + targeted timeline queries (`run_sql_query`, `get_latest_records`, `get_records_by_time`, `get_event_counts_by_time`, `get_candidates_by_fuzzy_matching`),
3. uses `ehr.think` as an explicit scratchpad,

but inherits a *failure mode* the baseline didn't have: routinely calling
`get_latest_records` on timestamp-less tables. The overall per-call success
rate is actually 1.1 pp **lower** than the baseline (94.42% vs 95.53%); it's
the *mix* of tools, not the success rate, that drives the +11.9 pp overall F1.
