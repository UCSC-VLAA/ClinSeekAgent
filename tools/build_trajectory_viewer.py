#!/usr/bin/env python3
"""Build a self-contained HTML viewer for DeepMed trajectories.

Picks up to 4 diverse 50-60-step trajectories per task, parses each
assistant + tool pair into structured steps, and emits trajectories.html.
The HTML has a two-level selector: a task tab row, and a trajectory
dropdown for each task.
"""

import html
import json
from pathlib import Path

SRC = Path(
    "/root/.cache/huggingface/hub/datasets--Letian2003--DeepMed_trajectory/"
    "snapshots/119d7a9f592a6e758773d271b0e02cbf90860031/AgentEHR_train3k_3ep.jsonl"
)
OUT = Path("/fsx-shared/juncheng/EHR/tools/trajectories.html")

PER_TASK = 4
STEP_LEN_MIN, STEP_LEN_MAX = 50, 60


def load_dataset():
    # Iterate with the file's own newline handling; splitlines() would
    # break on embedded \r inside JSON string values.
    rows = []
    with SRC.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def select_trajectories(rows):
    """For each task, pick up to PER_TASK trajectories with 50-60 assistant
    turns, spread across the F1 distribution so the viewer sees a mix of
    high and low quality rollouts."""
    by_task = {}
    for i, item in enumerate(rows):
        n_ast = sum(1 for m in item["messages"] if m["role"] == "assistant")
        if STEP_LEN_MIN <= n_ast <= STEP_LEN_MAX:
            t = item.get("task")
            by_task.setdefault(t, []).append((i, item))

    selected = {}  # task -> list[(line_idx, item)]
    for task, items in sorted(by_task.items()):
        items.sort(key=lambda t: -(t[1].get("score", {}).get("f1") or 0))
        n = len(items)
        if n == 0:
            continue
        # pick spread indices in the sorted list
        picks = sorted({0, n // 4, n // 2, (3 * n) // 4})[:PER_TASK]
        selected[task] = [items[k] for k in picks if k < n]
    return selected


def parse_args(raw):
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    if not isinstance(parsed, dict):
        return {"_value": parsed}
    return parsed


def group_steps(messages):
    """Walk the messages list and pair assistant turns with their tool
    responses by tool_call_id.

    Returns (system, user_question, steps). A step has:
        - thought:   str  (assistant free-form text for this turn; may be '')
        - calls:     [{id, name, args, response}]
        - terminal:  bool (True when this turn had no tool_calls)
    """
    system = ""
    user_question = ""
    id_to_response = {}
    for m in messages:
        if m["role"] == "tool":
            id_to_response[m.get("tool_call_id", "")] = m.get("content") or ""
        elif m["role"] == "system" and not system:
            system = m.get("content") or ""
        elif m["role"] == "user" and not user_question:
            user_question = m.get("content") or ""

    steps = []
    for m in messages:
        if m["role"] != "assistant":
            continue
        raw_calls = m.get("tool_calls") or []
        calls = []
        for tc in raw_calls:
            fn = tc.get("function", {}) or {}
            calls.append({
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "args": parse_args(fn.get("arguments", {})),
                "response": id_to_response.get(tc.get("id", "")),
            })
        steps.append({
            "thought": (m.get("content") or "").rstrip(),
            "calls": calls,
            "terminal": not calls,
        })
    return system, user_question, steps


# ------------------------------- rendering ------------------------------ #

def tool_family(name):
    if name.startswith("ehr."):
        return "ehr"
    if name.startswith("browser."):
        return "browser"
    return "other"


def render_value(v):
    """Render any JSON value without truncating — long `ehr.think` args etc.
    must be fully visible."""
    if isinstance(v, (dict, list)):
        return (
            f'<pre class="arg-json">'
            f'{html.escape(json.dumps(v, ensure_ascii=False, indent=2))}</pre>'
        )
    s = str(v)
    if "\n" in s or len(s) > 120:
        return f'<pre class="arg-json">{html.escape(s)}</pre>'
    return f'<span class="arg-val">{html.escape(s)}</span>'


def render_args(args):
    if args == {}:
        return '<div class="args-empty">(no arguments)</div>'
    rows = []
    for k, v in args.items():
        rows.append(
            f'<div class="arg-row">'
            f'  <span class="arg-key">{html.escape(str(k))}</span>'
            f'  {render_value(v)}'
            f'</div>'
        )
    return '<div class="args">' + "".join(rows) + "</div>"


def render_response(resp):
    if resp is None:
        return '<div class="resp resp-missing">(no response captured)</div>'
    body = resp.rstrip()
    if not body:
        return '<div class="resp resp-missing">(empty response)</div>'
    too_long = len(body) > 800
    preview = body[:600] + ("…" if too_long else "")
    escaped_full = html.escape(body)
    escaped_preview = html.escape(preview)
    header = (
        '<div class="resp-head">'
        '<span class="resp-label">Tool Response</span>'
        f'<span class="resp-size">{len(body):,} chars</span>'
    )
    if too_long:
        header += (
            '<button class="resp-toggle" type="button"'
            ' onclick="this.closest(\'.resp\').classList.toggle(\'open\')">'
            'expand</button></div>'
            f'<pre class="resp-body preview">{escaped_preview}</pre>'
            f'<pre class="resp-body full">{escaped_full}</pre>'
        )
    else:
        header += "</div>" + f'<pre class="resp-body">{escaped_full}</pre>'
    return f'<div class="resp">{header}</div>'


def render_thought(text):
    if not text.strip():
        return ""
    # No truncation — some `ehr.think`-style free-text turns are long.
    return (
        '<div class="thought">'
        '<div class="role-label">Assistant Message</div>'
        f'<div class="thought-body">{html.escape(text)}</div>'
        "</div>"
    )


def render_step(i, step):
    parts = [f'<section class="step"><div class="step-no">Step {i + 1}</div>']
    # Assistant free-form content: always render when non-empty (even without
    # tool calls, which is the terminal finish turn).
    parts.append(render_thought(step["thought"]))
    for call in step["calls"]:
        fam = tool_family(call["name"])
        parts.append(
            f'<div class="call call-{fam}">'
            f'  <div class="call-head">'
            f'    <span class="call-badge badge-{fam}">{html.escape(fam)}</span>'
            f'    <span class="call-name">{html.escape(call["name"])}</span>'
            f'  </div>'
            f'  {render_args(call["args"])}'
            f'  {render_response(call["response"])}'
            f'</div>'
        )
    if step["terminal"]:
        parts.append('<div class="terminal-tag">End of trajectory</div>')
    parts.append("</section>")
    return "".join(parts)


def render_trajectory(task, index_in_task, line_idx, item):
    _, question, steps = group_steps(item["messages"])
    traj_id = f"{task}-{index_in_task}"
    meta = {
        "task": task,
        "subject_id": item.get("subject_id"),
        "hadm_id": item.get("hadm_id"),
        "prediction_time": item.get("prediction_time"),
        "steps": len(steps),
        "tool_calls": sum(len(s["calls"]) for s in steps),
        "f1": _fmt_num(item.get("score", {}).get("f1")),
        "precision": _fmt_num(item.get("score", {}).get("prec")),
        "recall": _fmt_num(item.get("score", {}).get("rec")),
    }
    labels = item.get("label") or []
    label_names = []
    if isinstance(labels, list):
        for l in labels:
            if isinstance(l, dict):
                label_names.append(l.get("name") or l.get("label") or str(l))
            else:
                label_names.append(str(l))
    predictions = item.get("predictions") or []

    meta_cells = "".join(
        f'<div class="meta-cell"><span class="meta-key">{k}</span>'
        f'<span class="meta-val">{html.escape(str(v)) if v is not None else "—"}</span></div>'
        for k, v in meta.items()
    )

    steps_html = "\n".join(render_step(i, s) for i, s in enumerate(steps))

    return f"""
<article class="traj" data-task="{html.escape(task)}" data-traj="{traj_id}">
  <header class="traj-head">
    <div class="traj-title">
      <span class="traj-task">{html.escape(task)}</span>
      <span class="traj-qid">{html.escape(item.get("qid", ""))}</span>
      <span class="traj-line">source line {line_idx}</span>
    </div>
    <div class="meta-row">{meta_cells}</div>
  </header>

  <section class="question">
    <div class="role-label question-label">User Question</div>
    <pre class="question-body">{html.escape(question.strip())}</pre>
  </section>

  <section class="answers">
    <div class="answer-col">
      <div class="role-label answer-label">Ground Truth ({len(label_names)})</div>
      <ul class="answer-list gt">
        {''.join(f'<li>{html.escape(str(n))}</li>' for n in label_names) or '<li class="empty">—</li>'}
      </ul>
    </div>
    <div class="answer-col">
      <div class="role-label answer-label">Model Prediction ({len(predictions)})</div>
      <ul class="answer-list pred">
        {''.join(f'<li>{html.escape(str(n))}</li>' for n in predictions) or '<li class="empty">—</li>'}
      </ul>
    </div>
  </section>

  <div class="steps">
    {steps_html}
  </div>
</article>
"""


def _fmt_num(x):
    if x is None:
        return None
    try:
        return f"{float(x):.3f}"
    except (TypeError, ValueError):
        return str(x)


# ---------------------------------- CSS --------------------------------- #

CSS = """
:root {
  --bg: #f7f8fa;
  --surface: #ffffff;
  --border: #e4e6eb;
  --text: #1c2128;
  --muted: #6b7280;
  --accent: #2563eb;
  --ehr: #0ea5e9;
  --ehr-bg: #e0f2fe;
  --browser: #10b981;
  --browser-bg: #d1fae5;
  --other: #a855f7;
  --other-bg: #f3e8ff;
  --resp: #f1f5f9;
  --thought: #fffbeb;
  --thought-border: #fbbf24;
  --question: #eff6ff;
  --question-border: #bfdbfe;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0; padding: 0;
  line-height: 1.45;
}
header.page {
  background: #0f172a; color: #f1f5f9;
  padding: 18px 32px;
  position: sticky; top: 0; z-index: 10;
  box-shadow: 0 1px 4px rgba(0,0,0,.15);
}
header.page h1 { margin: 0 0 4px; font-size: 18px; font-weight: 600; letter-spacing: -.01em; }
header.page .sub { color: #94a3b8; font-size: 12.5px; }

.controls {
  position: sticky; top: 58px; z-index: 9;
  background: #1e293b;
  padding: 10px 32px;
  display: flex; align-items: center; gap: 14px;
  flex-wrap: wrap;
}
.tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tab {
  background: #334155; color: #cbd5e1;
  border: none; padding: 7px 12px;
  border-radius: 6px; font-size: 12.5px; font-weight: 500;
  cursor: pointer; white-space: nowrap;
  transition: background .15s, color .15s;
}
.tab:hover { background: #475569; color: #f8fafc; }
.tab.active { background: #2563eb; color: #ffffff; }
.traj-picker-group {
  display: flex; align-items: center; gap: 8px;
  color: #cbd5e1; font-size: 12.5px;
}
.traj-picker-group label { font-weight: 500; }
.traj-picker {
  background: #0f172a; color: #e2e8f0;
  border: 1px solid #475569;
  border-radius: 6px; padding: 6px 10px;
  font-size: 12.5px; font-family: inherit;
  max-width: 420px;
  cursor: pointer;
}
.traj-picker:focus { outline: 2px solid #2563eb; }

main { padding: 22px 32px 80px; max-width: 1200px; margin: 0 auto; }
.traj { display: none; }
.traj.active { display: block; }

.traj-head {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px; margin-bottom: 14px;
}
.traj-title { display: flex; align-items: baseline; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.traj-task {
  font-size: 15px; font-weight: 600; letter-spacing: -.01em;
  padding: 3px 10px; border-radius: 5px;
  background: var(--ehr-bg); color: var(--ehr);
}
.traj-qid { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.traj-line { color: #9ca3af; font-family: ui-monospace, monospace; font-size: 11px; }
.meta-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.meta-cell { display: flex; flex-direction: column; }
.meta-key { font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; margin-bottom: 2px; }
.meta-val { font-size: 13px; color: var(--text); font-variant-numeric: tabular-nums; }

.question {
  background: var(--question); border: 1px solid var(--question-border);
  border-radius: 10px; padding: 14px 18px; margin-bottom: 14px;
}
.role-label {
  font-size: 10.5px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); margin-bottom: 6px;
}
.question-label { color: var(--accent); }
.question-body {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12.5px; line-height: 1.55; color: #1e3a8a;
}

.answers { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 22px; }
.answer-col { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; }
.answer-label { color: var(--muted); }
.answer-list { margin: 0; padding-left: 20px; }
.answer-list li { margin: 2px 0; font-size: 13px; }
.answer-list.gt li { color: #047857; }
.answer-list.pred li { color: #1d4ed8; }
.answer-list .empty { color: var(--muted); list-style: none; margin-left: -16px; }

.step {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 18px 10px; margin-bottom: 10px;
  position: relative;
}
.step-no {
  position: absolute; top: 12px; right: 14px;
  font-size: 10.5px; font-weight: 600; color: var(--muted);
  background: #f1f5f9; padding: 2px 8px; border-radius: 10px;
}

.thought {
  background: var(--thought);
  border-left: 3px solid var(--thought-border);
  padding: 10px 14px;
  border-radius: 0 6px 6px 0;
  margin-bottom: 12px;
}
.thought-body {
  font-size: 13px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word;
  color: #78350f;
}

.terminal-tag {
  display: inline-block;
  margin-top: 4px;
  padding: 3px 10px;
  background: #dcfce7; color: #065f46;
  border-radius: 12px;
  font-size: 11px; font-weight: 600;
}

.call {
  border: 1px solid var(--border);
  border-radius: 8px;
  margin: 8px 0;
  overflow: hidden;
}
.call-head {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 12px;
  background: #f9fafb;
  border-bottom: 1px solid var(--border);
}
.call-badge {
  font-size: 9.5px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .05em; padding: 2px 7px; border-radius: 4px;
}
.badge-ehr { background: var(--ehr-bg); color: var(--ehr); }
.badge-browser { background: var(--browser-bg); color: var(--browser); }
.badge-other { background: var(--other-bg); color: var(--other); }
.call-ehr .call-head { border-left: 3px solid var(--ehr); }
.call-browser .call-head { border-left: 3px solid var(--browser); }
.call-other .call-head { border-left: 3px solid var(--other); }
.call-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px; font-weight: 600; color: var(--text);
}

.args {
  padding: 6px 12px 2px;
  background: #fdfdfe;
}
.arg-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 4px 0;
  border-bottom: 1px dashed #eef0f3;
  font-size: 12.5px;
}
.arg-row:last-child { border-bottom: none; }
.arg-key {
  flex: 0 0 150px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #6366f1; font-weight: 500;
  word-break: break-word;
}
.arg-val {
  flex: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #111827; word-break: break-word;
}
.arg-json {
  flex: 1; margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; background: #f8fafc;
  border: 1px solid #e2e8f0;
  padding: 6px 8px; border-radius: 4px; color: #0f172a;
  white-space: pre-wrap; word-break: break-word;
  max-height: none;
}
.args-empty { color: var(--muted); font-style: italic; font-size: 12.5px; padding: 6px 0 4px; }

.resp {
  border-top: 1px solid var(--border);
  background: var(--resp);
}
.resp-head {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 12px; font-size: 11px;
}
.resp-label { font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: #475569; }
.resp-size { color: var(--muted); font-variant-numeric: tabular-nums; }
.resp-toggle {
  margin-left: auto;
  background: #e2e8f0; color: #334155;
  border: none; padding: 2px 10px; border-radius: 4px;
  font-size: 11px; font-weight: 500; cursor: pointer;
}
.resp-toggle:hover { background: #cbd5e1; }
.resp-body {
  margin: 0; padding: 8px 12px 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; line-height: 1.5; color: #1e293b;
  white-space: pre-wrap; word-break: break-word;
}
.resp .full { display: none; }
.resp.open .preview { display: none; }
.resp.open .full { display: block; }
.resp.open .resp-toggle { color: transparent; position: relative; }
.resp.open .resp-toggle::after {
  content: "collapse"; position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  color: #334155;
}
.resp-missing { padding: 8px 12px; color: var(--muted); font-style: italic; font-size: 12px; }
"""

JS = """
(function () {
  const tabs = document.querySelectorAll('.tab');
  const pickers = document.querySelectorAll('.traj-picker');
  const trajs = document.querySelectorAll('.traj');

  function showTask(task) {
    tabs.forEach(t => t.classList.toggle('active', t.dataset.task === task));
    pickers.forEach(p => p.hidden = p.dataset.task !== task);
    const picker = document.querySelector(`.traj-picker[data-task="${task}"]`);
    const trajId = picker ? picker.value : null;
    trajs.forEach(t => t.classList.toggle('active', t.dataset.traj === trajId));
    window.scrollTo({ top: 0, behavior: 'instant' });
  }

  tabs.forEach(tab => tab.addEventListener('click', () => showTask(tab.dataset.task)));
  pickers.forEach(picker => picker.addEventListener('change', () => {
    const trajId = picker.value;
    trajs.forEach(t => t.classList.toggle('active', t.dataset.traj === trajId));
    window.scrollTo({ top: 0, behavior: 'instant' });
  }));

  const first = tabs[0];
  if (first) showTask(first.dataset.task);
})();
"""


def main():
    rows = load_dataset()
    selected = select_trajectories(rows)

    tasks_ordered = [t for t in ["diagnoses_ccs", "labevents", "microbiologyevents",
                                  "prescriptions", "procedures_ccs", "transfers"]
                     if t in selected]

    tab_html = "".join(
        f'<button class="tab" data-task="{html.escape(t)}">'
        f'{html.escape(t)} <span class="tab-count">({len(selected[t])})</span>'
        f'</button>'
        for t in tasks_ordered
    )

    picker_html_parts = []
    body_parts = []
    for task in tasks_ordered:
        entries = selected[task]
        option_tags = []
        for i, (line_idx, item) in enumerate(entries):
            traj_id = f"{task}-{i}"
            score = _fmt_num(item.get("score", {}).get("f1")) or "—"
            n_steps = sum(1 for m in item["messages"] if m["role"] == "assistant")
            qid = item.get("qid", "")
            label_count = len(item.get("label") or [])
            opt = (
                f'<option value="{html.escape(traj_id)}">'
                f'{i + 1}. {html.escape(qid)} · {n_steps} steps · F1 {score} · {label_count} labels'
                f'</option>'
            )
            option_tags.append(opt)
            body_parts.append(render_trajectory(task, i, line_idx, item))
        picker_html_parts.append(
            f'<select class="traj-picker" data-task="{html.escape(task)}" hidden>'
            f'{"".join(option_tags)}</select>'
        )

    picker_html = (
        '<div class="traj-picker-group">'
        '<label>Trajectory:</label>'
        + "".join(picker_html_parts)
        + "</div>"
    )

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>DeepMed Trajectory Viewer</title>
<style>{CSS}</style>
</head>
<body>
<header class="page">
  <h1>DeepMed EHR Trajectory Viewer</h1>
  <div class="sub">Diverse 50–60 step rollouts from Letian2003/DeepMed_trajectory. Pick a task, then a trajectory.</div>
</header>
<nav class="controls">
  <div class="tabs">{tab_html}</div>
  {picker_html}
</nav>
<main>
{''.join(body_parts)}
</main>
<script>{JS}</script>
</body>
</html>
"""
    OUT.write_text(html_doc)
    print(f"wrote {OUT} ({len(html_doc):,} bytes)")
    for task in tasks_ordered:
        print(f"  {task}: {len(selected[task])} trajectories")


if __name__ == "__main__":
    main()
