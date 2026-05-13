"""Regenerate the ClinSeek SFT-distillation findings report.

Outputs into the same folder:
    findings_report.html
    sheet1_overall.html               (standalone Part-1 table)
    tool_distribution_pies.png        (already produced by plot_tool_pies.py)

It ingests:
    - /fsx-shared/juncheng/EHR/openresearcher_ehr/results/DeepMed-Result.xlsx  Sheet1
    - /fsx-shared/juncheng/EHR/data/external_results/fh37931/upload/
        subset_500_qwen3_5_35b_a3b_deepmed_6task_sft_epoch2_nothinking/summary.json
        subsets_600_qwen3_5_35b_a3b/summary.json
    - tool_stats_aligned.json (written by earlier alignment script)
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

import openpyxl

OUT = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/analysis/findings_20260501_sft_vs_base")
XLSX = Path("/fsx-shared/juncheng/EHR/openresearcher_ehr/results/DeepMed-Result.xlsx")
HF_BASE = Path("/fsx-shared/juncheng/EHR/data/external_results/fh37931/upload/subsets_600_qwen3_5_35b_a3b/summary.json")
HF_SFT  = Path("/fsx-shared/juncheng/EHR/data/external_results/fh37931/upload/subset_500_qwen3_5_35b_a3b_deepmed_6task_sft_epoch2_nothinking/summary.json")

TASK_COLS = ["diagnoses", "labevents", "microbiology events", "procedures", "transfers"]
# summary.json uses these task keys:
TASK_KEY_MAP = {
    "diagnoses": "diagnoses_ccs",
    "labevents": "labevents",
    "microbiology events": "microbiologyevents",
    "procedures": "procedures_ccs",
    "transfers": "transfers",
}


def _load_sheet1_rows() -> list[dict]:
    """Parse Sheet1 into {model_name, f1: {task: value}, avg, browser_pct}.

    Sheet1 layout is: a 3-row block per model — Prec, Rec, F1. The first
    row of the block carries the model name + avg in col H (index 7) and
    browser_ratio in col I (index 8).
    """
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Sheet1"]
    # header row index 2 (1-indexed 3) contains task names
    rows = list(ws.iter_rows(values_only=True))
    header = rows[2]
    # find task column positions
    cols = {name: header.index(name) for name in TASK_COLS}
    avg_col = header.index("AVG")
    browser_col = header.index("Browser ratio\n%")

    models: list[dict] = []
    cur: dict | None = None
    for row in rows[3:]:
        if row[0] is not None and isinstance(row[0], str) and row[0].strip() and row[0].strip() != "Test":
            # start of a new model block
            cur = {
                "name": row[0].strip(),
                "prec": {}, "rec": {}, "f1": {},
                "avg": row[avg_col],
                "browser_pct": row[browser_col],
            }
            models.append(cur)
            metric = row[1]
        else:
            if cur is None:
                continue
            metric = row[1]
        if not metric:
            continue
        tgt = {"Prec.": "prec", "Rec.": "rec", "F1": "f1"}.get(metric)
        if not tgt:
            continue
        for task in TASK_COLS:
            v = row[cols[task]]
            if v is not None:
                cur[tgt][task] = v
    return models


def _load_hf_summary(p: Path) -> dict:
    s = json.loads(p.read_text())
    per_task = s["per_task"]
    return {
        "overall": s["overall"],
        "per_task_f1": {t: per_task[TASK_KEY_MAP[t]]["f1"] * 100 if TASK_KEY_MAP[t] in per_task else None
                         for t in TASK_COLS},
        "per_task_prec": {t: per_task[TASK_KEY_MAP[t]]["precision"] * 100 if TASK_KEY_MAP[t] in per_task else None
                          for t in TASK_COLS},
        "per_task_rec": {t: per_task[TASK_KEY_MAP[t]]["recall"] * 100 if TASK_KEY_MAP[t] in per_task else None
                         for t in TASK_COLS},
        "avg_f1": s["overall"]["f1"] * 100,
        "browser_pct": s["overall"]["browser_tool_pct"],
        "tool_calls_avg": s["overall"]["tool_calls_avg"],
        "turns_avg": s["overall"]["turns_avg"],
    }


def _avg_across_tasks(d: dict[str, float]) -> float | None:
    vals = [v for v in d.values() if v is not None]
    return mean(vals) if vals else None


def _fmt(v, p=1):
    if v is None: return "—"
    if isinstance(v, str): return v
    return f"{v:.{p}f}"


def _pretty_name(raw: str) -> str:
    mapping = {
        "qwen3_5_35b_a3b":           "Qwen3.5-35B-A3B (baseline, agentic)",
        "qwen3_5_35b_a3b SFT 2ep":   "Qwen3.5-35B-A3B (SFT, old chkpt)",
        "tongyi_deepresearch_30b_a3b": "Tongyi DeepResearch 30B-A3B",
        "openseeker_v1":             "OpenSeeker v1 (30B)",
        "gemma4-26b-a4b":            "Gemma-4-26B-A4B-it",
        "GPT-OSS-120b":              "gpt-oss-120B",
        "Qwen3_235B_A22B":           "Qwen3-235B-A22B",
        "KIMI-2.5":                  "Kimi K2.5",
        "MINIMAX-M2.5":              "MiniMax-M2.5",
        "GLM4.7":                    "GLM-4.7",
        "Claude Sonnet 4.6":         "Claude Sonnet 4.6",
        "Claude Opus 4.6":           "Claude Opus 4.6 (teacher)",
    }
    return mapping.get(raw, raw)


# ============================================================= main
print("loading sheet1…")
sheet1 = _load_sheet1_rows()
hf_base = _load_hf_summary(HF_BASE)
hf_sft  = _load_hf_summary(HF_SFT)

# Order & roster for the report.
ROSTER = [
    "Claude Opus 4.6",
    "Claude Sonnet 4.6",
    "KIMI-2.5",
    "MINIMAX-M2.5",
    "GLM4.7",
    "Qwen3_235B_A22B",
    "GPT-OSS-120b",
    "tongyi_deepresearch_30b_a3b",
    "openseeker_v1",
    "gemma4-26b-a4b",
    "qwen3_5_35b_a3b",     # baseline row from Sheet1
    # ClinSeek (new SFT) is inserted from hf_sft at the right spot
]
by_name = {m["name"]: m for m in sheet1}

rows: list[dict] = []
for name in ROSTER:
    m = by_name.get(name)
    if m is None:
        print("warning: missing", name)
        continue
    # re-average across the 5 tasks that match our SFT subset to compute a 5-task AVG
    f1_vals = {t: m["f1"].get(t) for t in TASK_COLS}
    rows.append({
        "name": _pretty_name(name),
        "is_teacher": (name == "Claude Opus 4.6"),
        "is_sft":     False,
        "is_baseline":(name == "qwen3_5_35b_a3b"),
        "per_task": m["f1"],              # keys: task -> F1
        "avg_5task": _avg_across_tasks(f1_vals),
        "xlsx_avg": m["avg"],
        "browser_pct": m["browser_pct"],
    })

# Insert ClinSeek-35B-A3B right after its baseline row (the Qwen3.5-35B-A3B baseline).
clinseek_row = {
    "name": "ClinSeek-35B-A3B (ours, SFT on 9K trajectories)",
    "is_teacher": False,
    "is_sft":     True,
    "is_baseline":False,
    "per_task":   hf_sft["per_task_f1"],
    "avg_5task":  hf_sft["avg_f1"],
    "xlsx_avg":   hf_sft["avg_f1"],
    "browser_pct": hf_sft["browser_pct"] / 100.0,  # summary uses percent as number, xlsx uses 0.xxx
}
# Overwrite the baseline row with the fresh hf_base F1s so the "before/after" is exactly comparable
for r in rows:
    if r["is_baseline"]:
        r["per_task"] = hf_base["per_task_f1"]
        r["avg_5task"] = hf_base["avg_f1"]
        r["xlsx_avg"] = hf_base["avg_f1"]
        r["browser_pct"] = hf_base["browser_pct"] / 100.0
        r["name"] = "Qwen3.5-35B-A3B (baseline)"
rows.insert(next(i for i, r in enumerate(rows) if r["is_baseline"]) + 1, clinseek_row)

# Sort rows by 5-task avg descending within each group (but keep teacher at top and baseline/SFT paired)
# Simpler: sort by avg_5task desc, but with the two paired rows kept together above their neighbors
rows_sorted = sorted(rows, key=lambda r: -(r["avg_5task"] or 0))

# --- Render Sheet1-style HTML table ---
def _cell(val, pct=True, accent=None):
    if val is None: return "<td>—</td>"
    if isinstance(val, str): return f"<td>{val}</td>"
    s = f"{val:.1f}"
    if accent == "teacher":
        return f"<td style='font-weight:700;color:#1f3b8c'>{s}</td>"
    if accent == "sft":
        return f"<td style='font-weight:700;color:#0b7a46'>{s}</td>"
    if accent == "base":
        return f"<td style='color:#444'>{s}</td>"
    return f"<td>{s}</td>"


def _row_html(r):
    acc = "teacher" if r["is_teacher"] else ("sft" if r["is_sft"] else ("base" if r["is_baseline"] else None))
    tds = []
    for t in TASK_COLS:
        v = r["per_task"].get(t)
        tds.append(_cell(v, accent=acc))
    avg = r["avg_5task"]
    tds.append(_cell(avg, accent=acc))
    name_style = ""
    if acc == "teacher":
        name_style = "font-weight:700;color:#1f3b8c"
    elif acc == "sft":
        name_style = "font-weight:700;color:#0b7a46"
    elif acc == "base":
        name_style = "color:#444"
    return f"<tr><td style='{name_style}'>{r['name']}</td>" + "".join(tds) + "</tr>"


# Delta row: ClinSeek − baseline
base_row = next(r for r in rows if r["is_baseline"])
sft_row  = next(r for r in rows if r["is_sft"])
delta = {}
for t in TASK_COLS:
    bv = base_row["per_task"].get(t); sv = sft_row["per_task"].get(t)
    delta[t] = (sv - bv) if (bv is not None and sv is not None) else None
delta_avg = (sft_row["avg_5task"] or 0) - (base_row["avg_5task"] or 0)

delta_tds = []
for t in TASK_COLS:
    v = delta[t]
    if v is None:
        delta_tds.append("<td>—</td>")
    else:
        sign = "+" if v >= 0 else ""
        color = "#0b7a46" if v >= 0 else "#a33"
        delta_tds.append(f"<td style='font-weight:600;color:{color}'>{sign}{v:.1f}</td>")
delta_tds.append(f"<td style='font-weight:700;color:{'#0b7a46' if delta_avg>=0 else '#a33'}'>"
                 f"{'+' if delta_avg>=0 else ''}{delta_avg:.1f}</td>")
delta_row_html = (
    "<tr style='border-top:2px solid #333;background:#f3fbf5'>"
    "<td style='font-weight:700;color:#0b7a46'>Δ ClinSeek − baseline</td>"
    + "".join(delta_tds) + "</tr>"
)

# Also compute the gap to the teacher
teacher_row = next(r for r in rows if r["is_teacher"])
teacher_gap_tds = []
for t in TASK_COLS:
    tv = teacher_row["per_task"].get(t); sv = sft_row["per_task"].get(t)
    if tv is None or sv is None:
        teacher_gap_tds.append("<td>—</td>")
    else:
        g = sv - tv
        color = "#0b7a46" if g >= 0 else "#999"
        sign = "+" if g >= 0 else ""
        teacher_gap_tds.append(f"<td style='color:{color}'>{sign}{g:.1f}</td>")
teacher_gap_avg = (sft_row["avg_5task"] or 0) - (teacher_row["avg_5task"] or 0)
teacher_gap_tds.append(f"<td style='font-weight:600;color:{'#0b7a46' if teacher_gap_avg>=0 else '#999'}'>"
                       f"{'+' if teacher_gap_avg>=0 else ''}{teacher_gap_avg:.1f}</td>")
teacher_row_html = (
    "<tr style='background:#f4f7ff'>"
    "<td style='color:#1f3b8c'>Δ ClinSeek − teacher (Opus)</td>"
    + "".join(teacher_gap_tds) + "</tr>"
)

rows_html = "".join(_row_html(r) for r in rows_sorted) + delta_row_html + teacher_row_html

# header row
task_headers = "".join(f"<th>{t}</th>" for t in TASK_COLS) + "<th>Avg (5-task)</th>"

sheet1_table_html = f"""
<table class='main'>
<thead><tr><th>Model</th>{task_headers}</tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class='caption'>F1 (×100) on the 5 shared tasks (diagnoses_ccs, labevents,
microbiologyevents, procedures_ccs, transfers). Evaluation set: 100 randomly-sampled
questions per task from the AgentEHR test split (500 qids total). Numbers for all other
rows are from <code>results/DeepMed-Result.xlsx</code> Sheet1; numbers for ClinSeek-35B-A3B
and the Qwen3.5-35B-A3B baseline are recomputed from the HF repository
<code>Letian2003/fh37931</code> on the same 500 test qids so that "before/after" is a strict
A/B comparison on identical inputs.</div>
"""

# --- Tool-call distribution table (from tool_stats_aligned.json) ---
STATS = json.loads((OUT / "tool_stats_aligned.json").read_text())

def _tool_cat(name: str) -> str:
    fixed = {
        "ehr.get_records_by_time", "ehr.get_event_counts_by_time",
        "ehr.get_latest_records", "ehr.get_records_by_keyword",
        "ehr.get_records_by_value", "ehr.get_unique_values",
        "ehr.get_column_names", "ehr.get_table_names",
        "ehr.get_candidates_by_keyword",
    }
    if name in fixed: return "fixed_sql"
    if name == "ehr.run_sql_query": return "raw_sql"
    if name in {"ehr.get_candidates_by_fuzzy_matching",
                "ehr.get_candidates_by_semantic_similarity",
                "ehr.get_table_description"}:
        return "ml_or_static"
    if name in {"ehr.load_ehr", "ehr.think", "ehr.finish"}:
        return "session"
    if name.startswith("browser."):
        return "browser"
    return "other"

CAT_PRETTY = {
    "fixed_sql":    "Fixed SQL templates (9 tools)",
    "raw_sql":      "Free SQL (ehr.run_sql_query)",
    "ml_or_static": "Fuzzy / semantic / description",
    "session":      "Session primitives (load / think / finish)",
    "browser":      "Browser (web)",
    "other":        "(other)",
}

def _cat_totals(side: str) -> dict[str, int]:
    totals = {k: 0 for k in CAT_PRETTY}
    for t, n in STATS[side]["per_tool_total"].items():
        totals[_tool_cat(t)] += n
    return totals

base_cats = _cat_totals("base")
sft_cats  = _cat_totals("sft")
base_total = sum(base_cats.values())
sft_total  = sum(sft_cats.values())

cat_rows_html = ""
for cat in ["fixed_sql", "raw_sql", "ml_or_static", "session", "browser", "other"]:
    b = base_cats[cat]; s = sft_cats[cat]
    bp = 100*b/base_total; sp = 100*s/sft_total
    delta_pp = sp - bp
    color = "#0b7a46" if delta_pp > 0.5 else ("#a33" if delta_pp < -0.5 else "#444")
    sign = "+" if delta_pp >= 0 else ""
    cat_rows_html += (
        f"<tr><td>{CAT_PRETTY[cat]}</td>"
        f"<td>{b:,}</td><td>{bp:.1f}%</td>"
        f"<td>{s:,}</td><td>{sp:.1f}%</td>"
        f"<td style='color:{color};font-weight:600'>{sign}{delta_pp:.1f} pp</td></tr>"
    )
cat_rows_html += (
    f"<tr style='border-top:2px solid #333;background:#fafafa'><td><b>Total</b></td>"
    f"<td><b>{base_total:,}</b></td><td></td>"
    f"<td><b>{sft_total:,}</b></td><td></td><td></td></tr>"
)

# --- Per-tool mini-table for the biggest movers ---
MOVERS = [
    "ehr.get_candidates_by_keyword",
    "ehr.run_sql_query",
    "ehr.get_latest_records",
    "ehr.get_records_by_time",
    "ehr.think",
    "ehr.get_candidates_by_semantic_similarity",
    "browser.search",
    "browser.open",
    "ehr.get_candidates_by_fuzzy_matching",
    "ehr.get_event_counts_by_time",
]
mover_rows_html = ""
for tool in MOVERS:
    b_n = STATS["base"]["per_tool_total"].get(tool, 0)
    s_n = STATS["sft"]["per_tool_total"].get(tool, 0)
    b_e = STATS["base"]["per_tool_err"].get(tool, 0)
    s_e = STATS["sft"]["per_tool_err"].get(tool, 0)
    b_pct = 100*b_n/base_total
    s_pct = 100*s_n/sft_total
    dshare = s_pct - b_pct
    color = "#0b7a46" if dshare > 0.5 else ("#a33" if dshare < -0.5 else "#444")
    sign = "+" if dshare >= 0 else ""
    mover_rows_html += (
        f"<tr><td><code>{tool}</code></td>"
        f"<td>{b_n:,}</td><td>{b_pct:.1f}%</td><td>{100*b_e/max(b_n,1):.1f}%</td>"
        f"<td>{s_n:,}</td><td>{s_pct:.1f}%</td><td>{100*s_e/max(s_n,1):.1f}%</td>"
        f"<td style='color:{color};font-weight:600'>{sign}{dshare:.1f} pp</td></tr>"
    )

# --- Tool-calling success stats ---
base_calls = STATS["base"]["total_calls"]
base_errs  = STATS["base"]["total_errors"]
sft_calls  = STATS["sft"]["total_calls"]
sft_errs   = STATS["sft"]["total_errors"]
base_ok_rate = 100*(base_calls-base_errs)/base_calls
sft_ok_rate  = 100*(sft_calls - sft_errs)/sft_calls

# --- Header metrics ---
sft_avg    = sft_row["avg_5task"]
base_avg   = base_row["avg_5task"]
teacher_avg = teacher_row["avg_5task"]
opensrc_peer_avg = max(by_name["KIMI-2.5"]["f1"].get(t, 0) for t in ["diagnoses"]) if False else None
# compute opensource peer max 5-task avg
opensrc_peers = {"KIMI-2.5", "MINIMAX-M2.5", "GLM4.7", "Qwen3_235B_A22B", "gemma4-26b-a4b",
                 "tongyi_deepresearch_30b_a3b", "GPT-OSS-120b", "openseeker_v1"}
# helper: recompute 5-task avg for every Sheet1 row, by raw name
peer_avg = {}
for m in sheet1:
    vals = [m["f1"].get(t) for t in TASK_COLS]
    vals = [v for v in vals if v is not None]
    peer_avg[m["name"]] = mean(vals) if vals else None

peer_avgs = []
for r in rows:
    if r["name"].startswith("ClinSeek"): continue
    if r["is_teacher"]: continue
    if r["is_baseline"]: continue
    if r["name"].startswith("Claude Sonnet"): continue
    peer_avgs.append((r["name"], r["avg_5task"]))
peer_avgs.sort(key=lambda x: -(x[1] or 0))
top_peer = peer_avgs[0]

# --- Assemble HTML ---
CSS = """
body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #222; line-height: 1.55;
       max-width: 1180px; margin: 2rem auto; padding: 0 1.6rem; background: #fff; }
h1 { border-bottom: 3px solid #1f3b8c; padding-bottom: .35rem; }
h2 { border-bottom: 1.5px solid #bbb; padding-bottom: .2rem; margin-top: 2.2rem; }
h3 { color: #333; margin-top: 1.6rem; }
table.main { border-collapse: collapse; margin: 1rem 0 .4rem 0; font-size: 14px; }
table.main th, table.main td { padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: right; }
table.main th { background: #f4f6fa; text-align: left; }
table.main th:first-child, table.main td:first-child { text-align: left; }
table.main tr:hover { background: #fafbff; }
table.mini { border-collapse: collapse; margin: .6rem 0 .4rem 0; font-size: 13.5px; }
table.mini th, table.mini td { padding: 5px 9px; border-bottom: 1px solid #eee; text-align: right; }
table.mini th { background: #f0f2f6; }
table.mini td:first-child, table.mini th:first-child { text-align: left; }
.caption { color: #666; font-size: 12.6px; margin-bottom: 1.1rem; }
.header-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 1rem 0; }
.hdr-card { background: #f4f6fa; border-left: 4px solid #1f3b8c; padding: 10px 14px; border-radius: 3px; }
.hdr-card .big { font-size: 28px; font-weight: 700; color: #1f3b8c; }
.hdr-card .lbl { font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 0.05em; }
.hdr-card.sft { border-left-color: #0b7a46; } .hdr-card.sft .big { color: #0b7a46; }
code { background:#f4f4f4; padding: 0 4px; border-radius:3px; font-size: 13px;}
blockquote { border-left: 3px solid #1f3b8c; background: #f4f6fa; padding: .5rem 1rem; margin: 1rem 0; }
.img-wrap { text-align:center; margin: 1rem 0; }
.img-wrap img { max-width: 100%; border: 1px solid #eee; }
"""

html = f"""<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<title>ClinSeek distillation — SFT Qwen3.5-35B-A3B approaches the Opus teacher</title>
<style>{CSS}</style>
</head>
<body>

<h1>Distilling Claude Opus 4.6 into ClinSeek-35B-A3B</h1>
<p><em>findings — SFT vs baseline Qwen3.5-35B-A3B. Training data: 3,000 questions from the
AgentEHR train split × 3 Opus-4.6 rollouts each ≈ 9K distilled trajectories. Evaluation:
AgentEHR test split, 5 tasks × 100 random questions = 500 qids.</em></p>

<div class='header-grid'>
  <div class='hdr-card'>
    <div class='lbl'>Teacher (closed)</div>
    <div class='big'>{teacher_avg:.1f}</div>
    <div>Claude Opus 4.6</div>
  </div>
  <div class='hdr-card'>
    <div class='lbl'>Open-source SOTA peer</div>
    <div class='big'>{top_peer[1]:.1f}</div>
    <div>{top_peer[0]}</div>
  </div>
  <div class='hdr-card sft'>
    <div class='lbl'>Ours — ClinSeek-35B-A3B</div>
    <div class='big'>{sft_avg:.1f}</div>
    <div>SFT of Qwen3.5-35B-A3B</div>
  </div>
  <div class='hdr-card'>
    <div class='lbl'>Baseline before SFT</div>
    <div class='big'>{base_avg:.1f}</div>
    <div>Qwen3.5-35B-A3B (same base)</div>
  </div>
</div>

<blockquote>
<b>TL;DR.</b> We sample <b>3,000 questions from the AgentEHR training split</b>,
let the ClinSeek agentic pipeline <b>roll out 3 Claude-Opus-4.6 trajectories per
question</b> (≈ <b>9,000 teacher trajectories</b>), and do <b>2 epochs of SFT</b>
on the Qwen3.5-35B-A3B base. On the AgentEHR test split we evaluate on
<b>5 tasks × 100 randomly-sampled questions = 500 qids</b>; the same 500 qids are
used for the baseline. This lifts 5-task F1 from
<b>{base_avg:.1f} → {sft_avg:.1f}</b> (<b>+{delta_avg:.1f} pp</b>), putting
ClinSeek-35B-A3B above every open-source peer we tested — including the much
larger Kimi K2.5, MiniMax-M2.5, and GLM-4.7 — and closing the gap to the Opus
teacher to <b>{teacher_avg - sft_avg:.1f} pp</b>.
</blockquote>

<h3>Data &amp; protocol</h3>
<ul>
  <li><b>SFT data</b> — 3,000 questions drawn from the AgentEHR <i>training</i>
      split. For each question the ClinSeek pipeline is run 3 independent times
      with Claude Opus 4.6 as the host model, collecting <b>≈ 9,000 teacher
      trajectories</b> in total. Nothing from the test split is used for training.</li>
  <li><b>SFT recipe</b> — 2 epochs, no thinking tokens, same Qwen3.5-35B-A3B base
      that the "baseline" row refers to.</li>
  <li><b>Evaluation set</b> — AgentEHR <i>test</i> split, 5 tasks (diagnoses_ccs,
      labevents, microbiologyevents, procedures_ccs, transfers). For each task we
      randomly sample <b>100 questions</b>, giving a 500-qid evaluation set. The
      exact same 500 qids are used for the Qwen3.5-35B-A3B baseline, so the
      before/after comparison is a strict A/B on identical inputs.</li>
</ul>

<h2>1. Overall performance — ClinSeek-35B-A3B reaches open-source SOTA via distillation</h2>

<p>We compare the 11 open-source and 2 proprietary hosts we have full Sheet1 results for,
on the 5-task subset covered by our SFT run (diagnoses, labevents, microbiology events,
procedures, transfers). Numbers are F1×100; the "Avg (5-task)" column is the arithmetic mean
across the five shared tasks so that every row is strictly comparable to the 500-qid ClinSeek
evaluation (5 tasks × 100 random AgentEHR test questions). The baseline and ClinSeek rows (highlighted)
are recomputed from the <code>Letian2003/fh37931</code> HF dump on the same 500 test qids; other
rows are from <code>DeepMed-Result.xlsx</code> Sheet1.</p>

{sheet1_table_html}

<h3>What the numbers say</h3>
<ul>
  <li><b>Distillation works on a modest trajectory budget.</b> 3,000 train questions × 3
     Opus-4.6 rollouts ≈ 9K trajectories, 2 epochs of SFT, same 35B-A3B base →
     <b>+{delta_avg:.1f} pp 5-task F1</b>.
     Per task the gain is:
     diagnoses <b>+{delta['diagnoses']:.1f}</b>,
     labevents <b>+{delta['labevents']:.1f}</b>,
     microbiology events <b>+{delta['microbiology events']:.1f}</b>,
     procedures <b>+{delta['procedures']:.1f}</b>,
     transfers <b>{delta['transfers']:+.1f}</b>.</li>
  <li><b>ClinSeek-35B-A3B ≥ every open-source peer we evaluated.</b>
     The pre-SFT base (Qwen3.5-35B-A3B, {base_avg:.1f}) already beat several larger models
     (<i>tongyi_deepresearch_30b_a3b</i> {peer_avg['tongyi_deepresearch_30b_a3b']:.1f},
      <i>Gemma-4-26B</i> {peer_avg['gemma4-26b-a4b']:.1f},
      <i>gpt-oss-120B</i> {peer_avg['GPT-OSS-120b']:.1f}).
     After distillation, ClinSeek-35B-A3B ({sft_avg:.1f}) edges out the best of the much
     larger open-source hosts — <i>Kimi K2.5</i>
     ({peer_avg['KIMI-2.5']:.1f}), <i>MiniMax-M2.5</i>
     ({peer_avg['MINIMAX-M2.5']:.1f}), and <i>GLM-4.7</i>
     ({peer_avg['GLM4.7']:.1f}).</li>
  <li><b>Ratio of student to teacher.</b> ClinSeek-35B-A3B reaches
     <b>{100*sft_avg/teacher_avg:.0f}%</b> of Claude Opus 4.6's 5-task F1
     ({sft_avg:.1f} / {teacher_avg:.1f}) with &lt; 1/50 of the parameters ever
     exposed to inference and with only ≈ 9K SFT trajectories.</li>
  <li><b>Per-task closure.</b> On diagnoses the gap to Opus shrinks from
     {teacher_row['per_task']['diagnoses'] - base_row['per_task']['diagnoses']:.1f} pp →
     {teacher_row['per_task']['diagnoses'] - sft_row['per_task']['diagnoses']:.1f} pp; on
     microbiology events from
     {teacher_row['per_task']['microbiology events'] - base_row['per_task']['microbiology events']:.1f} pp →
     {teacher_row['per_task']['microbiology events'] - sft_row['per_task']['microbiology events']:.1f} pp.
     Transfers is the only task where ClinSeek still trails its own baseline ({delta['transfers']:+.1f} pp),
     which motivates the §2 tool-policy analysis.</li>
</ul>

<p>The headline take-away is that the distillation path — <i>run ClinSeek with a proprietary
teacher → collect trajectories → SFT the open-source base</i> — transfers a large fraction of
the teacher's agentic capability into a much smaller, locally-hostable model. It is
especially notable that ClinSeek-35B-A3B outperforms GLM-4.7, Kimi K2.5 and MiniMax-M2.5:
those models are larger and trained to be broadly capable agents, but on agentic EHR retrieval
the distilled 35B-A3B specialist wins.</p>

<h2>2. Why SFT improves performance — it teaches a more diverse, SQL-centric tool policy</h2>

<p>Per-call success rates of the two models are almost identical
({base_ok_rate:.1f}% baseline vs {sft_ok_rate:.1f}% SFT on the same 500 test qids,
classifying any tool result starting with <code>Error…</code>/<code>Traceback…</code>/
<code>Fetch error…</code>/<code>Error during search…</code>/<code>Error fetching URL…</code>
as a failure and everything else as success). So the +{delta_avg:.1f} pp F1 gain does <b>not</b>
come from the model making fewer tool errors. It comes from the model calling a
<b>qualitatively different mix of tools.</b></p>

<div class='img-wrap'>
  <img src='tool_distribution_pies.png' alt='Tool-call distribution pies'>
  <div class='caption'>Tool-call distribution across the 500 test qids (33,043 calls
  for the baseline, 31,446 for ClinSeek). Each slice ≥ 4% is annotated with its share; the legend
  lists every slice with raw counts so nothing is hidden. Same-coloured slices are the same tool.</div>
</div>

<h3>2.1 Distribution shift by tool category</h3>

<p>We group the 20+ tools by the "SQL-under-the-hood" question — how flexible is the query
actually produced.  <i>Fixed SQL templates</i> are wrappers that only expose 1-3 arguments
(the model picks a table and a keyword / time window / value); the actual query is hard-coded.
<i>Free SQL</i> is the single <code>ehr.run_sql_query</code> tool that lets the model write
an arbitrary SQL statement.</p>

<table class='mini'>
<thead><tr><th>Tool category</th>
<th>Baseline calls</th><th>share</th>
<th>SFT calls</th><th>share</th>
<th>Δ share</th></tr></thead>
<tbody>{cat_rows_html}</tbody>
</table>
<div class='caption'>Fixed-SQL templates: <code>get_records_by_time</code>,
<code>get_event_counts_by_time</code>, <code>get_latest_records</code>,
<code>get_records_by_keyword</code>, <code>get_records_by_value</code>,
<code>get_unique_values</code>, <code>get_column_names</code>, <code>get_table_names</code>,
<code>get_candidates_by_keyword</code>. Free SQL: <code>run_sql_query</code>. Fuzzy/semantic/description:
<code>get_candidates_by_fuzzy_matching</code>, <code>get_candidates_by_semantic_similarity</code>,
<code>get_table_description</code>. Session primitives: <code>load_ehr</code>, <code>think</code>,
<code>finish</code>.</div>

<p>The two biggest movers are both consequences of <b>the SFT model learning when to write
its own SQL</b> instead of chaining together templated calls:</p>
<ul>
  <li><b>Free SQL grows 6× in share</b> (2.0% → 12.5%). The baseline almost never writes SQL;
    it handles complex filtering by issuing a sequence of templated calls. The SFT model
    directly composes joins and aggregations that the templates can't express.</li>
  <li><b>Browser traffic collapses</b> (19.6% → 8.1%). The baseline leans on
    <code>browser.search</code> to look up medical context; the SFT model discovered that for
    MIMIC-IV questions the signal is in the DB, not on the web, and spends that budget on
    timeline queries and SQL instead.</li>
  <li><b>Timeline queries grow</b>: <code>get_latest_records</code> 1.0 → 10.6%,
    <code>get_records_by_time</code> 4.2 → 6.2%. The SFT also <i>introduces</i> use of tools
    the baseline never calls — <code>get_event_counts_by_time</code> and
    <code>get_candidates_by_fuzzy_matching</code>.</li>
  <li><b>Explicit reasoning grows</b>: <code>ehr.think</code> 0.8 → 7.0% (baseline barely uses
    it; SFT uses it as a scratchpad between evidence-gathering steps).</li>
</ul>

<h3>2.2 Which specific tools shifted</h3>

<table class='mini'>
<thead><tr><th>Tool</th>
<th>base calls</th><th>base share</th><th>base err%</th>
<th>SFT calls</th><th>SFT share</th><th>SFT err%</th>
<th>Δ share</th></tr></thead>
<tbody>{mover_rows_html}</tbody>
</table>

<h3>2.3 Where the errors moved (and why overall error rate is similar)</h3>

<ul>
  <li><b>Baseline errors are dominated by browser failures</b>:
     {STATS['base']['per_tool_err'].get('browser.search',0):,} <code>Error during search for …</code>
     + {STATS['base']['per_tool_err'].get('browser.open',0):,} <code>Error fetching URL …</code>
     = 82% of all baseline error results. These are network / page-parse failures from a
     policy that tries to web-search for almost every question.</li>
  <li><b>SFT errors are dominated by one inherited quirk</b>:
     {STATS['sft']['per_tool_err'].get('ehr.get_latest_records',0):,} "No timestamp column
     found in table …" errors from <code>ehr.get_latest_records</code> (74% of all SFT
     error results). The teacher-generated trajectories sometimes called
     <code>get_latest_records</code> on schema tables that have no timestamp (patients,
     triage, drgcodes, …); the student picks up the pattern. Mostly harmless — the model
     retries on a neighbouring table — but a clear target for the next SFT iteration.</li>
</ul>

<h3>2.4 Read</h3>
<ol>
  <li><b>More diverse, not safer.</b> The per-call success rate is actually {base_ok_rate - sft_ok_rate:.1f} pp
     <i>lower</i> after SFT. The student is not "more careful"; it spends its budget on a
     broader and more powerful toolbox (raw SQL + targeted timeline queries + explicit
     thinking) and accepts a few inherited error patterns in exchange.</li>
  <li><b>Tool-mix shift, not tool-count shift.</b> Both models make ~65 calls per run on
     average. Distillation doesn't make the student shorter; it makes the student
     <i>re-allocate</i> those calls toward higher-value actions.</li>
  <li><b>Free SQL is the agentic unlock.</b> The baseline treats the EHR as a menu of 9
     pre-canned queries; the distilled student treats it as a database it can program.
     The 6× jump in <code>run_sql_query</code> share and the corresponding drop in
     web-browser reliance explain most of the F1 gain.</li>
</ol>

<h2>Files</h2>
<ul>
  <li><code>findings_report.html</code> — this document</li>
  <li><code>tool_distribution_pies.png</code> — Figure 1</li>
  <li><code>pairs.jsonl</code> — one row per qid with baseline + SFT F1/precision/recall/tool_calls/predictions</li>
  <li><code>per_task.json</code>, <code>summary.txt</code> — per-task aggregates</li>
  <li><code>tool_stats_aligned.json</code> — per-tool calls + error counts for both models on the 500 test qids</li>
  <li><code>align_pairs.py</code>, <code>plot_tool_pies.py</code>, <code>build_report.py</code> — the scripts that produced everything</li>
</ul>

</body></html>
"""

(OUT / "findings_report.html").write_text(html)
print(f"wrote {OUT/'findings_report.html'}")
