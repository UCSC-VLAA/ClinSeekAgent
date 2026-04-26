#!/usr/bin/env python3
"""
Unified evaluation script: scores + tool-call statistics.

Usage:
    python evaluate_results.py --results <results.jsonl> --benchmark <benchmark.json>
    python evaluate_results.py --results <results.jsonl> --benchmark <benchmark.json> --extract-text-answer-without-finish
"""

import argparse
import ast
import json
import math
import re
from collections import Counter, defaultdict, deque
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_RESULTS = (
    "./openresearcher_ehr/results/ehrbench_1800_qwen3_5_35b_a3b_deepmed_6task_sft_epoch2/results.jsonl"
    # "./openresearcher_ehr/results/train_trajectory_3k_4ep_nonthinking_done3ep/results.jsonl"
    # "./openresearcher_ehr/results/subset_500_deepmed_6task_sft_epoch2/results.jsonl"
)
DEFAULT_BENCHMARK = (
    './data/EHR-Bench/ehr_bench_sampled_40_per_task.json'
    # "./data/AgentEHR-Bench/MIMICIVAgentBench/train/mix_training_3k.json"
    # "./data/AgentEHR-Bench/MIMICIVAgentBench/common/subset_500/merged_subsets_500.json"
)


# ---------------------------------------------------------------------------
# QID helpers
# ---------------------------------------------------------------------------
def is_ehr_bench_path(path):
    p = str(path).lower()
    return "ehrbench" in p or "ehr_bench" in p


def build_qid(record, ehr_bench_mode=False):
    if ehr_bench_mode:
        qid = record.get("qid")
        if isinstance(qid, str) and qid:
            return qid
    task = record.get("task")
    subject_id = record.get("subject_id")
    hadm_id = record.get("hadm_id")
    if task is None or subject_id is None:
        raise ValueError(f"Unable to build qid from record: {record}")
    if hadm_id is not None:
        return f"{task}_{subject_id}_{hadm_id}"
    return f"{task}_{subject_id}"


def resolve_task_name_from_record(record):
    task = record.get("task")
    if isinstance(task, str) and task:
        return task
    qid = record.get("qid", "")
    parts = qid.split("_")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return "_".join(parts[:-2]) or "unknown"
    if len(parts) >= 2 and parts[-1].isdigit():
        return "_".join(parts[:-1]) or "unknown"
    return "unknown"


def resolve_task_name(runs, fallback_task="unknown"):
    for run in runs:
        t = run.get("task")
        if isinstance(t, str) and t:
            return t
    return fallback_task if isinstance(fallback_task, str) and fallback_task else "unknown"


# ---------------------------------------------------------------------------
# Prediction extraction
# ---------------------------------------------------------------------------
def parse_json_or_python(value):
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        raise TypeError(f"Unsupported arguments type: {type(value)}")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return ast.literal_eval(value)


def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def dedupe_string_predictions(values):
    deduped, seen = [], set()
    for v in values:
        if not isinstance(v, str):
            continue
        cleaned = re.sub(r"\s+", " ", v).strip().strip('"').strip("'")
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def extract_candidate_names_from_tool_output(text):
    names = []
    single_column_mode = False
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped:
            single_column_mode = False
            continue
        if stripped.startswith("--- Results for keyword "):
            single_column_mode = False
            continue
        if stripped.startswith("Error:") or stripped.startswith("No records found"):
            single_column_mode = False
            continue
        if lower == "candidate":
            single_column_mode = True
            continue
        if "icd_code" in lower and "candidate" in lower:
            single_column_mode = False
            continue
        if single_column_mode:
            if not any(t in lower for t in ("similarity_score", "columns:", "table:", "description:")):
                names.append(stripped)
            continue
        m = re.match(r"^\s*(\S+)\s+(\d{1,2})\s+(.+?)\s+(\d+(?:\.\d+)?)\s*$", line)
        if m:
            names.append(m.group(3).strip())
            continue
        m = re.match(r"^\s*(\S+)\s+(\d{1,2})\s+(.+?)\s*$", line)
        if m:
            names.append(m.group(3).strip())
    return dedupe_string_predictions(names)


def collect_official_candidate_names(messages):
    pending = deque()
    official, seen = [], set()
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            pending.extend(msg.get("tool_calls") or [])
            continue
        if role != "tool":
            continue
        tc = pending.popleft() if pending else None
        tool_name = msg.get("name", "")
        tool_args = ""
        if tc:
            fn = tc.get("function", {})
            tool_name = tool_name or fn.get("name", "")
            tool_args = fn.get("arguments", "")
        if not isinstance(tool_args, str):
            tool_args = json.dumps(tool_args, ensure_ascii=False)
        if "_candidates" not in tool_args:
            continue
        if "get_candidates" not in tool_name and "run_sql_query" not in tool_name:
            continue
        for cand in extract_candidate_names_from_tool_output(msg.get("content", "")):
            key = normalize_text(cand)
            if not key or key in seen:
                continue
            seen.add(key)
            official.append(cand)
    return official


def extract_explicit_string_list_from_text(text):
    snippets = []
    for m in reversed(list(re.finditer(r"<answer>\s*(.*?)\s*</answer>", text or "", re.I | re.DOTALL))):
        snippets.append(m.group(1).strip())
    for m in reversed(list(re.finditer(r"\[[\s\S]{1,2000}?\]", text or "", re.DOTALL))):
        s = m.group(0)
        if '"' not in s and "'" not in s:
            continue
        snippets.append(s)
    for snippet in snippets:
        try:
            parsed = parse_json_or_python(snippet)
        except Exception:
            continue
        if isinstance(parsed, dict):
            for k in ("response", "answer", "answers", "predictions"):
                if isinstance(parsed.get(k), list):
                    parsed = parsed[k]
                    break
        if isinstance(parsed, list):
            cleaned = dedupe_string_predictions(parsed)
            if cleaned:
                return cleaned
    return []


def extract_bullet_list_from_text(text):
    if not text:
        return []
    cue_re = re.compile(r"(final answer|answer should|plausible diagnoses include|list of strings|list like)", re.I)
    bullet_re = re.compile(r"^[-*]\s+(.+?)\s*$")
    lines = text.splitlines()
    collecting, bullets, best = False, [], []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if collecting and bullets:
                best = bullets
                break
            continue
        if cue_re.search(stripped):
            collecting = True
            bullets = []
            continue
        m = bullet_re.match(stripped)
        if collecting and m:
            bullets.append(m.group(1).strip())
            continue
        if collecting and bullets:
            best = bullets
            break
    if not best and bullets:
        best = bullets
    return dedupe_string_predictions(best)


def align_predictions_to_official_candidates(predictions, official):
    if not predictions:
        return []
    by_key = {normalize_text(c): c for c in official if normalize_text(c)}
    aligned, seen = [], set()
    for pred in predictions:
        pk = normalize_text(pred)
        if not pk:
            continue
        canon = by_key.get(pk)
        if canon is None:
            for o in official:
                ok = normalize_text(o)
                if not ok:
                    continue
                if ok in pk or pk in ok:
                    canon = o
                    break
        final = canon or pred
        fk = normalize_text(final)
        if fk in seen:
            continue
        seen.add(fk)
        aligned.append(final)
    return aligned


def extract_official_candidate_mentions(text, official):
    norm = normalize_text(text)
    if not norm or not official:
        return []
    matches = []
    for c in official:
        ck = normalize_text(c)
        if not ck:
            continue
        pos = norm.find(ck)
        if pos != -1:
            matches.append((pos, c))
    matches.sort(key=lambda x: x[0])
    return dedupe_string_predictions([c for _, c in matches])


def extract_fallback_predictions_from_last_assistant(messages):
    last = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            last = msg
            break
    if last is None:
        return [], None
    text = "\n".join(p for p in ((last.get("content") or "").strip(), (last.get("reasoning_content") or "").strip()) if p).strip()
    if not text:
        return [], None
    official = collect_official_candidate_names(messages)
    explicit = extract_explicit_string_list_from_text(text)
    if explicit:
        aligned = align_predictions_to_official_candidates(explicit, official)
        return aligned or explicit, "fallback_explicit_string_list"
    bullets = extract_bullet_list_from_text(text)
    if bullets:
        aligned = align_predictions_to_official_candidates(bullets, official)
        if aligned:
            return aligned, "fallback_bullet_list"
    answer_like = any(cue in normalize_text(text) for cue in ("ehr.finish", "final answer", "thus, the answer", "list of strings", "plausible diagnoses", "use ehr.finish"))
    if not answer_like:
        return [], None
    mentioned = extract_official_candidate_mentions(text, official)
    if mentioned:
        return mentioned, "fallback_official_candidate_mentions"
    return [], None


def extract_finish_predictions_with_source(result, *, allow_text=False):
    messages = result.get("messages", [])
    for msg in reversed(messages):
        for tc in reversed(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            if "finish" not in fn.get("name", "").lower():
                continue
            try:
                args = parse_json_or_python(fn.get("arguments", {}))
            except Exception:
                return [], "finish_parse_error"
            preds = args.get("response", []) if isinstance(args, dict) else args
            if isinstance(preds, list):
                return preds, "finish_tool_call"
            if isinstance(preds, str) and preds.strip():
                return [preds.strip()], "finish_tool_call"
            return [], "finish_tool_call_non_list"
    if not allow_text:
        return [], "no_prediction"
    preds, src = extract_fallback_predictions_from_last_assistant(messages)
    return preds, src or "no_prediction"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def f1_score(predictions, standard_answer):
    pred_set = {p for p in predictions if isinstance(p, str)}
    gt = set()
    if isinstance(standard_answer, str):
        gt.add(standard_answer)
    elif isinstance(standard_answer, list):
        has_atc = any(
            isinstance(a, dict) and a.get("atc_name") for a in standard_answer
        )
        for ans in standard_answer:
            if isinstance(ans, str):
                gt.add(ans)
            elif isinstance(ans, dict):
                if has_atc:
                    atc = ans.get("atc_name")
                    if isinstance(atc, str):
                        gt.add(atc)
                else:
                    name = ans.get("name")
                    if isinstance(name, str):
                        gt.add(name)

    if not pred_set:
        return {"f1": 0.0, "prec": 0.0, "rec": 0.0, "em": 0.0}

    pl = {p.lower() for p in pred_set}
    gl = {g.lower() for g in gt}
    inter = pl & gl
    prec = len(inter) / len(pl)
    rec = len(inter) / len(gl) if gl else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return {"f1": f1, "prec": prec, "rec": rec, "em": 1.0 if pl == gl else 0.0}


def calculate_true_best_at_k(scores, k, metric="f1"):
    vals = sorted(s[metric] for s in scores)
    n = len(vals)
    if n == 0:
        return 0.0
    if k > n:
        k = n
    total = math.comb(n, k)
    return sum(math.comb(i - 1, k - 1) / total * vals[i - 1] for i in range(k, n + 1))


# ---------------------------------------------------------------------------
# Tool-call analysis
# ---------------------------------------------------------------------------
def normalize_tool_name(name):
    name = (name or "unknown").strip()
    m = re.match(r"^[A-Za-z0-9_.]+", name)
    if m:
        name = m.group(0)
    if name.startswith("ehr_"):
        return "ehr." + name[4:]
    if name.startswith("browser_"):
        return "browser." + name[8:]
    return name


def count_tool_calls(record):
    total = 0
    browser = 0
    for msg in record.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            name = normalize_tool_name(tc.get("function", {}).get("name", "unknown"))
            total += 1
            if name.startswith("browser."):
                browser += 1
    return total, browser


def count_turns(record):
    return sum(1 for msg in record.get("messages", []) if msg.get("role") == "assistant")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_benchmark(path, ehr_bench_mode=False):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {build_qid(item, ehr_bench_mode=ehr_bench_mode): item for item in data}


def load_results(path):
    grouped = defaultdict(list)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            grouped[r["qid"]].append(r)
    return grouped


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
def evaluate(results_path, benchmark_path, *, allow_text=False):
    ehr_bench_mode = is_ehr_bench_path(benchmark_path) or is_ehr_bench_path(results_path)
    bm = load_benchmark(benchmark_path, ehr_bench_mode=ehr_bench_mode)
    results = load_results(results_path)

    # Per-task accumulators
    task_scores = defaultdict(list)       # task -> list of per-qid avg f1
    task_prec = defaultdict(list)
    task_rec = defaultdict(list)
    task_tool_counts = defaultdict(list)  # task -> list of tool-call counts
    task_browser_counts = defaultdict(list)
    task_turn_counts = defaultdict(list)  # task -> list of conversation turn counts
    task_total = defaultdict(int)         # task -> benchmark count
    task_completed = defaultdict(int)
    task_runs = defaultdict(int)          # task -> total result runs
    task_source = defaultdict(Counter)
    bm_by_task = defaultdict(list)

    # Per-category accumulators (e.g., task_type: risk_prediction / decision_making)
    cat_scores = defaultdict(list)
    cat_prec = defaultdict(list)
    cat_rec = defaultdict(list)
    cat_tool_counts = defaultdict(list)
    cat_browser_counts = defaultdict(list)
    cat_turn_counts = defaultdict(list)
    cat_total = defaultdict(int)
    cat_completed = defaultdict(int)
    cat_runs = defaultdict(int)
    cat_source = defaultdict(Counter)

    for qid, item in bm.items():
        t = item.get("task", "unknown")
        bm_by_task[t].append(qid)
        task_total[t] += 1
        cat = item.get("task_type")
        if isinstance(cat, str) and cat:
            cat_total[cat] += 1

    for qid, item in bm.items():
        runs = results.get(qid, [])
        t = resolve_task_name(runs, item.get("task"))
        gt = item["label"]
        cat = item.get("task_type")
        cat = cat if isinstance(cat, str) and cat else None

        if not runs:
            continue

        task_completed[t] += 1
        task_runs[t] += len(runs)
        if cat:
            cat_completed[cat] += 1
            cat_runs[cat] += len(runs)
        run_f1s = []
        run_precs = []
        run_recs = []
        for run in runs:
            preds, src = extract_finish_predictions_with_source(run, allow_text=allow_text)
            task_source[t][src] += 1
            if cat:
                cat_source[cat][src] += 1
            sc = f1_score(preds, gt)
            run_f1s.append(sc["f1"])
            run_precs.append(sc["prec"])
            run_recs.append(sc["rec"])

            tc_total, tc_browser = count_tool_calls(run)
            task_tool_counts[t].append(tc_total)
            task_browser_counts[t].append(tc_browser)
            task_turn_counts[t].append(count_turns(run))
            if cat:
                cat_tool_counts[cat].append(tc_total)
                cat_browser_counts[cat].append(tc_browser)
                cat_turn_counts[cat].append(count_turns(run))

        avg_run_f1 = sum(run_f1s) / len(run_f1s)
        avg_run_prec = sum(run_precs) / len(run_precs)
        avg_run_rec = sum(run_recs) / len(run_recs)
        task_scores[t].append(avg_run_f1)
        task_prec[t].append(avg_run_prec)
        task_rec[t].append(avg_run_rec)
        if cat:
            cat_scores[cat].append(avg_run_f1)
            cat_prec[cat].append(avg_run_prec)
            cat_rec[cat].append(avg_run_rec)

    category_aggs = {
        "scores": cat_scores,
        "prec": cat_prec,
        "rec": cat_rec,
        "tools": cat_tool_counts,
        "browsers": cat_browser_counts,
        "turns": cat_turn_counts,
        "total": cat_total,
        "completed": cat_completed,
        "runs": cat_runs,
        "sources": cat_source,
    }

    return (task_scores, task_prec, task_rec, task_tool_counts, task_browser_counts,
            task_turn_counts, task_total, task_completed, task_runs, task_source,
            category_aggs)


def main():
    parser = argparse.ArgumentParser(description="Evaluate results: scores + tool-call stats.")
    parser.add_argument("--results", default=DEFAULT_RESULTS)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    parser.add_argument("--extract-text-answer-without-finish", action="store_true")
    parser.add_argument("--output", default=None, help="Optional JSON output path.")
    args = parser.parse_args()

    results_path = Path(args.results)
    if results_path.is_dir():
        results_path = results_path / "results.jsonl"
    if not results_path.exists():
        raise SystemExit(f"Results file not found: {results_path}")

    (scores, precs, recs, tools, browsers, turns, totals, completed, runs, sources,
     category_aggs) = evaluate(
        str(results_path), args.benchmark, allow_text=args.extract_text_answer_without_finish,
    )

    all_tasks = sorted(set(totals) | set(scores))

    # Header
    print(f"{'Task':<22s} {'Total':>5s} {'Done':>5s} {'Runs':>5s} {'Prec':>7s} {'Rec':>7s} {'F1':>7s} {'ToolAvg':>8s} {'Brows%':>7s} {'TurnAvg':>8s}")
    print("-" * 89)

    g_scores, g_precs, g_recs, g_tools, g_browsers, g_turns = [], [], [], [], [], []

    zero_runs_printed = 0
    zero_runs_skipped = 0
    ZERO_RUNS_LIMIT = 6
    for t in all_tasks:
        n_total = totals[t]
        n_done = completed.get(t, 0)
        n_runs = runs.get(t, 0)
        s = scores.get(t, [])
        p = precs.get(t, [])
        r = recs.get(t, [])
        tc = tools.get(t, [])
        bc = browsers.get(t, [])
        tn = turns.get(t, [])

        avg_f1 = sum(s) / len(s) if s else 0.0
        avg_p = sum(p) / len(p) if p else 0.0
        avg_r = sum(r) / len(r) if r else 0.0
        avg_tc = sum(tc) / len(tc) if tc else 0.0
        avg_tn = sum(tn) / len(tn) if tn else 0.0
        browser_pct = sum(bc) / sum(tc) * 100 if sum(tc) else 0.0

        g_scores.extend(s)
        g_precs.extend(p)
        g_recs.extend(r)
        g_tools.extend(tc)
        g_browsers.extend(bc)
        g_turns.extend(tn)

        if n_runs == 0:
            if zero_runs_printed >= ZERO_RUNS_LIMIT:
                zero_runs_skipped += 1
                continue
            zero_runs_printed += 1

        print(f"{t:<22s} {n_total:>5d} {n_done:>5d} {n_runs:>5d} {avg_p*100:>7.1f} {avg_r*100:>7.1f} {avg_f1*100:>7.1f} {avg_tc:>8.1f} {browser_pct:>6.1f}% {avg_tn:>8.1f}")

        # prediction source breakdown (only if interesting)
        src = sources.get(t, {})
        non_finish = {k: v for k, v in src.items() if k != "finish_tool_call"}
        if non_finish:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(non_finish.items()))
            print(f"  └─ sources: finish={src.get('finish_tool_call', 0)}, {parts}")

    if zero_runs_skipped:
        print(f"  └─ ... {zero_runs_skipped} more task(s) with Runs=0 hidden")

    # Overall
    print("-" * 89)
    avg_f1 = sum(g_scores) / len(g_scores) if g_scores else 0.0
    avg_p = sum(g_precs) / len(g_precs) if g_precs else 0.0
    avg_r = sum(g_recs) / len(g_recs) if g_recs else 0.0
    avg_tc = sum(g_tools) / len(g_tools) if g_tools else 0.0
    avg_tn = sum(g_turns) / len(g_turns) if g_turns else 0.0
    browser_pct = sum(g_browsers) / sum(g_tools) * 100 if sum(g_tools) else 0.0
    n_total = sum(totals.values())
    n_done = sum(completed.values())
    n_runs = sum(runs.values())
    print(f"{'Overall':<22s} {n_total:>5d} {n_done:>5d} {n_runs:>5d} {avg_p*100:>7.1f} {avg_r*100:>7.1f} {avg_f1*100:>7.1f} {avg_tc:>8.1f} {browser_pct:>6.1f}% {avg_tn:>8.1f}")

    # Per-category breakdown (e.g. task_type: risk_prediction / decision_making)
    cat_totals = category_aggs["total"]
    if cat_totals:
        print("-" * 89)
        print("By task_type:")
        for cat in sorted(cat_totals):
            c_total = cat_totals[cat]
            c_done = category_aggs["completed"].get(cat, 0)
            c_runs = category_aggs["runs"].get(cat, 0)
            cs = category_aggs["scores"].get(cat, [])
            cp = category_aggs["prec"].get(cat, [])
            cr = category_aggs["rec"].get(cat, [])
            ctc = category_aggs["tools"].get(cat, [])
            cbc = category_aggs["browsers"].get(cat, [])
            ctn = category_aggs["turns"].get(cat, [])
            c_avg_f1 = sum(cs) / len(cs) if cs else 0.0
            c_avg_p = sum(cp) / len(cp) if cp else 0.0
            c_avg_r = sum(cr) / len(cr) if cr else 0.0
            c_avg_tc = sum(ctc) / len(ctc) if ctc else 0.0
            c_avg_tn = sum(ctn) / len(ctn) if ctn else 0.0
            c_browser_pct = sum(cbc) / sum(ctc) * 100 if sum(ctc) else 0.0
            print(f"{cat:<22s} {c_total:>5d} {c_done:>5d} {c_runs:>5d} {c_avg_p*100:>7.1f} {c_avg_r*100:>7.1f} {c_avg_f1*100:>7.1f} {c_avg_tc:>8.1f} {c_browser_pct:>6.1f}% {c_avg_tn:>8.1f}")
            c_src = category_aggs["sources"].get(cat, {})
            c_non_finish = {k: v for k, v in c_src.items() if k != "finish_tool_call"}
            if c_non_finish:
                parts = ", ".join(f"{k}={v}" for k, v in sorted(c_non_finish.items()))
                print(f"  └─ sources: finish={c_src.get('finish_tool_call', 0)}, {parts}")

    # Optional JSON output
    if args.output:
        payload = {}
        for t in all_tasks:
            s = scores.get(t, [])
            p = precs.get(t, [])
            r = recs.get(t, [])
            tc = tools.get(t, [])
            bc = browsers.get(t, [])
            tn = turns.get(t, [])
            payload[t] = {
                "total": totals[t],
                "completed": completed.get(t, 0),
                "runs": runs.get(t, 0),
                "precision": sum(p) / len(p) if p else 0.0,
                "recall": sum(r) / len(r) if r else 0.0,
                "f1": sum(s) / len(s) if s else 0.0,
                "tool_calls_avg": sum(tc) / len(tc) if tc else 0.0,
                "turns_avg": sum(tn) / len(tn) if tn else 0.0,
                "browser_tool_pct": sum(bc) / sum(tc) * 100 if sum(tc) else 0.0,
                "prediction_sources": dict(sources.get(t, {})),
            }
        cat_payload = {}
        for cat in sorted(category_aggs["total"]):
            cs = category_aggs["scores"].get(cat, [])
            cp = category_aggs["prec"].get(cat, [])
            cr = category_aggs["rec"].get(cat, [])
            ctc = category_aggs["tools"].get(cat, [])
            cbc = category_aggs["browsers"].get(cat, [])
            ctn = category_aggs["turns"].get(cat, [])
            cat_payload[cat] = {
                "total": category_aggs["total"][cat],
                "completed": category_aggs["completed"].get(cat, 0),
                "runs": category_aggs["runs"].get(cat, 0),
                "precision": sum(cp) / len(cp) if cp else 0.0,
                "recall": sum(cr) / len(cr) if cr else 0.0,
                "f1": sum(cs) / len(cs) if cs else 0.0,
                "tool_calls_avg": sum(ctc) / len(ctc) if ctc else 0.0,
                "turns_avg": sum(ctn) / len(ctn) if ctn else 0.0,
                "browser_tool_pct": sum(cbc) / sum(ctc) * 100 if sum(ctc) else 0.0,
                "prediction_sources": dict(category_aggs["sources"].get(cat, {})),
            }
        payload = {"per_task": payload, "per_task_type": cat_payload}
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON written to {out}")


if __name__ == "__main__":
    main()
