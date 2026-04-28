"""
Build an HTML viewer that shows, side-by-side, the messages for the same qid
from a "thinking" and a "non-thinking" trajectory file.

Usage:
    python trajectory_compare_viewer.py \
        --thinking    /path/to/results.jsonl \
        --nonthinking /path/to/results.jsonl \
        --qid         diagnoses_ccs_10040082_23686375 \
        --output      trajectory_compare.html
"""

import argparse
import html
import json
from pathlib import Path


def find_record(path: Path, qid: str) -> dict | None:
    with path.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("qid") == qid:
                return d
    return None


def render_tool_calls(tool_calls):
    if not tool_calls:
        return ""
    parts = []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "?")
        args = fn.get("arguments", "")
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False, indent=2)
        else:
            try:
                args_str = json.dumps(
                    json.loads(args), ensure_ascii=False, indent=2
                )
            except Exception:
                args_str = str(args)
        parts.append(
            '<div class="tc">'
            f'<div class="tc-name">🔧 {html.escape(name)}</div>'
            f'<pre class="tc-args">{html.escape(args_str)}</pre>'
            "</div>"
        )
    return '<div class="tc-list">' + "".join(parts) + "</div>"


def render_message(idx: int, m: dict) -> str:
    role = m.get("role", "?")
    content = m.get("content") or ""
    reasoning = m.get("reasoning_content") or ""
    tool_calls = m.get("tool_calls") or []
    name = m.get("name") or ""
    tool_call_id = m.get("tool_call_id") or ""

    tags = []
    if content.strip():
        tags.append(
            f'<span class="tag tag-content">content {len(content)}</span>'
        )
    if reasoning.strip():
        tags.append(
            f'<span class="tag tag-reasoning">reasoning {len(reasoning)}</span>'
        )
    if tool_calls:
        tags.append(
            f'<span class="tag tag-tc">tool_calls {len(tool_calls)}</span>'
        )
    if not tags:
        tags.append('<span class="tag tag-empty">empty</span>')

    header = (
        f'<div class="msg-header">'
        f'<span class="role role-{role}">{role}</span>'
        f'<span class="idx">#{idx}</span>'
        + "".join(tags)
    )
    if name:
        header += f'<span class="meta">name={html.escape(name)}</span>'
    if tool_call_id:
        header += (
            f'<span class="meta">tool_call_id={html.escape(tool_call_id[:12])}…</span>'
        )
    header += "</div>"

    body_parts = []
    if reasoning.strip():
        body_parts.append(
            '<div class="block block-reasoning">'
            '<div class="block-label">reasoning_content</div>'
            f'<pre>{html.escape(reasoning)}</pre>'
            "</div>"
        )
    if content.strip():
        body_parts.append(
            '<div class="block block-content">'
            '<div class="block-label">content</div>'
            f'<pre>{html.escape(content)}</pre>'
            "</div>"
        )
    if tool_calls:
        body_parts.append(
            '<div class="block block-tc">'
            '<div class="block-label">tool_calls</div>'
            + render_tool_calls(tool_calls)
            + "</div>"
        )
    if not body_parts and role == "tool":
        body_parts.append(
            '<div class="block block-content">'
            '<div class="block-label">tool result (empty)</div>'
            "</div>"
        )
    if not body_parts and not content.strip():
        body_parts.append(
            '<div class="block block-empty">'
            "<em>(no content / reasoning / tool_calls)</em>"
            "</div>"
        )

    return f'<div class="msg msg-{role}">{header}{"".join(body_parts)}</div>'


def summarize(record: dict) -> str:
    from collections import Counter

    msgs = record.get("messages", [])
    roles = Counter(m.get("role") for m in msgs)
    assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
    n_asst = len(assistant_msgs)
    n_content = sum(1 for m in assistant_msgs if (m.get("content") or "").strip())
    n_reason = sum(
        1 for m in assistant_msgs if (m.get("reasoning_content") or "").strip()
    )
    n_tc = sum(1 for m in assistant_msgs if m.get("tool_calls"))
    both = sum(
        1
        for m in assistant_msgs
        if (m.get("content") or "").strip() and (m.get("reasoning_content") or "").strip()
    )
    neither = sum(
        1
        for m in assistant_msgs
        if not (m.get("content") or "").strip()
        and not (m.get("reasoning_content") or "").strip()
    )

    def pct(x):
        return f"{x} ({100 * x / n_asst:.1f}%)" if n_asst else f"{x}"

    return (
        f'<div class="summary">'
        f"<div><strong>qid:</strong> {html.escape(str(record.get('qid')))}</div>"
        f"<div><strong>task:</strong> {html.escape(str(record.get('task')))}</div>"
        f"<div><strong>stop_reason:</strong> {html.escape(str(record.get('stop_reason')))}</div>"
        f"<div><strong>completed:</strong> {record.get('completed')}</div>"
        f"<div><strong>messages:</strong> {len(msgs)} (roles: {dict(roles)})</div>"
        f"<div><strong>assistant msgs:</strong> {n_asst}</div>"
        f"<div><strong>&nbsp;&nbsp;with content:</strong> {pct(n_content)}</div>"
        f"<div><strong>&nbsp;&nbsp;with reasoning:</strong> {pct(n_reason)}</div>"
        f"<div><strong>&nbsp;&nbsp;with tool_calls:</strong> {pct(n_tc)}</div>"
        f"<div><strong>&nbsp;&nbsp;content + reasoning:</strong> {pct(both)}</div>"
        f"<div><strong>&nbsp;&nbsp;neither (tc-only):</strong> {pct(neither)}</div>"
        f"</div>"
    )


CSS = """
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f4f6f9;
    color: #1f2933;
}
.header {
    background: #1e293b;
    color: #fff;
    padding: 16px 24px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}
.header h1 { margin: 0 0 6px 0; font-size: 18px; }
.header .sub { font-size: 13px; color: #cbd5e1; }
.container { display: flex; min-height: calc(100vh - 64px); }
.column {
    flex: 1;
    padding: 18px;
    overflow-y: auto;
    border-right: 1px solid #e2e8f0;
    min-width: 0;
}
.column:last-child { border-right: none; }
.column h2 {
    margin: 0 0 14px 0;
    font-size: 16px;
    color: #0f172a;
    padding-bottom: 8px;
    border-bottom: 2px solid #3b82f6;
}
.column.thinking h2 { border-color: #a855f7; }
.column.nonthinking h2 { border-color: #10b981; }
.summary {
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 16px;
    font-size: 12.5px;
    line-height: 1.6;
}
.summary div { margin: 0; }
.msg {
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    margin-bottom: 10px;
    padding: 10px 12px;
    font-size: 12.5px;
}
.msg-system { background: #f1f5f9; }
.msg-user   { background: #eff6ff; border-color: #bfdbfe; }
.msg-assistant { border-color: #c7d2fe; }
.msg-tool   { background: #f0fdf4; border-color: #bbf7d0; }
.msg-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
    font-size: 11.5px;
    color: #475569;
}
.role {
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 10px;
    color: #fff;
    text-transform: uppercase;
    font-size: 10.5px;
    letter-spacing: 0.5px;
}
.role-system    { background: #64748b; }
.role-user      { background: #3b82f6; }
.role-assistant { background: #6366f1; }
.role-tool      { background: #10b981; }
.idx { color: #94a3b8; font-family: monospace; }
.tag {
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10.5px;
    font-family: monospace;
    background: #e2e8f0;
    color: #475569;
}
.tag-content   { background: #dbeafe; color: #1e40af; }
.tag-reasoning { background: #f3e8ff; color: #6b21a8; }
.tag-tc        { background: #fef3c7; color: #92400e; }
.tag-empty     { background: #fee2e2; color: #991b1b; }
.meta { color: #94a3b8; font-family: monospace; font-size: 11px; }
.block { margin-top: 6px; }
.block-label {
    font-size: 10.5px;
    font-weight: 700;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 3px;
}
.block-reasoning .block-label { color: #7c3aed; }
.block-content .block-label   { color: #2563eb; }
.block-tc .block-label        { color: #b45309; }
.block pre {
    white-space: pre-wrap;
    word-wrap: break-word;
    word-break: break-word;
    margin: 0;
    padding: 8px 10px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 4px;
    font-family: "SF Mono", Consolas, monospace;
    font-size: 12px;
    line-height: 1.45;
    max-height: 420px;
    overflow-y: auto;
}
.block-reasoning pre { background: #faf5ff; border-color: #e9d5ff; }
.block-content pre   { background: #eff6ff; border-color: #bfdbfe; }
.block-tc .tc {
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 4px;
    padding: 6px 10px;
    margin-bottom: 6px;
}
.block-tc .tc-name {
    font-family: "SF Mono", Consolas, monospace;
    font-weight: 700;
    color: #92400e;
    font-size: 12px;
    margin-bottom: 3px;
}
.block-tc .tc-args {
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    padding: 6px 8px;
    background: #fff;
    border: 1px solid #fde68a;
    border-radius: 3px;
    font-family: "SF Mono", Consolas, monospace;
    font-size: 11.5px;
    max-height: 260px;
    overflow-y: auto;
}
.toolbar {
    display: flex;
    gap: 10px;
    padding: 10px 24px;
    background: #0f172a;
    color: #cbd5e1;
    font-size: 12px;
    align-items: center;
}
.toolbar label { cursor: pointer; user-select: none; }
.toolbar input { cursor: pointer; }
.hidden { display: none !important; }
"""

JS = """
function toggleRole(role, shown) {
    document.querySelectorAll('.msg-' + role).forEach(function(el){
        el.classList.toggle('hidden', !shown);
    });
}
document.addEventListener('DOMContentLoaded', function(){
    document.querySelectorAll('.toolbar input[type=checkbox]').forEach(function(cb){
        cb.addEventListener('change', function(){
            toggleRole(cb.dataset.role, cb.checked);
        });
    });
});
"""


def build_html(
    qid: str,
    thinking_record: dict | None,
    nonthinking_record: dict | None,
) -> str:
    def render_column(label_cls: str, title: str, record):
        if record is None:
            return (
                f'<div class="column {label_cls}">'
                f"<h2>{html.escape(title)}</h2>"
                f'<div class="summary">qid not found in this file.</div>'
                "</div>"
            )
        msg_html = "".join(
            render_message(i, m) for i, m in enumerate(record.get("messages", []))
        )
        return (
            f'<div class="column {label_cls}">'
            f"<h2>{html.escape(title)}</h2>"
            f"{summarize(record)}"
            f"{msg_html}"
            "</div>"
        )

    toolbar = (
        '<div class="toolbar">'
        "<strong>Show:</strong>"
        '<label><input type="checkbox" data-role="system" checked> system</label>'
        '<label><input type="checkbox" data-role="user" checked> user</label>'
        '<label><input type="checkbox" data-role="assistant" checked> assistant</label>'
        '<label><input type="checkbox" data-role="tool" checked> tool</label>'
        "</div>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trajectory Compare - {html.escape(qid)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>Trajectory Compare — {html.escape(qid)}</h1>
  <div class="sub">Left: thinking mode &nbsp;|&nbsp; Right: non-thinking mode</div>
</div>
{toolbar}
<div class="container">
  {render_column("thinking", "Thinking (2ep)", thinking_record)}
  {render_column("nonthinking", "Non-thinking (3ep)", nonthinking_record)}
</div>
<script>{JS}</script>
</body>
</html>"""


def build_single_html(title: str, record: dict | None, qid: str) -> str:
    toolbar = (
        '<div class="toolbar">'
        "<strong>Show:</strong>"
        '<label><input type="checkbox" data-role="system" checked> system</label>'
        '<label><input type="checkbox" data-role="user" checked> user</label>'
        '<label><input type="checkbox" data-role="assistant" checked> assistant</label>'
        '<label><input type="checkbox" data-role="tool" checked> tool</label>'
        "</div>"
    )
    if record is None:
        body = (
            f'<div class="column thinking"><h2>{html.escape(title)}</h2>'
            f'<div class="summary">qid not found.</div></div>'
        )
    else:
        msg_html = "".join(
            render_message(i, m) for i, m in enumerate(record.get("messages", []))
        )
        body = (
            f'<div class="column thinking" style="max-width:1100px;margin:0 auto;">'
            f"<h2>{html.escape(title)}</h2>"
            f"{summarize(record)}{msg_html}"
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Trajectory - {html.escape(qid)}</title>
<style>{CSS}</style>
</head>
<body>
<div class=\"header\">
  <h1>Trajectory — {html.escape(qid)}</h1>
  <div class=\"sub\">{html.escape(title)}</div>
</div>
{toolbar}
<div class=\"container\" style=\"background:#f4f6f9;\">
  {body}
</div>
<script>{JS}</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thinking", type=Path)
    ap.add_argument("--nonthinking", type=Path)
    ap.add_argument("--single", type=Path,
                    help="Render a single trajectory file (no side-by-side)")
    ap.add_argument("--title", default="Trajectory",
                    help="Title label for --single mode")
    ap.add_argument("--qid", required=True)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.single:
        rec = find_record(args.single, args.qid)
        if rec is None:
            print(f"WARN: qid not found in {args.single}")
        html_out = build_single_html(args.title, rec, args.qid)
    else:
        if not args.thinking or not args.nonthinking:
            raise SystemExit(
                "Provide either --single, or both --thinking and --nonthinking"
            )
        t_rec = find_record(args.thinking, args.qid)
        n_rec = find_record(args.nonthinking, args.qid)
        if t_rec is None:
            print(f"WARN: qid not found in {args.thinking}")
        if n_rec is None:
            print(f"WARN: qid not found in {args.nonthinking}")
        html_out = build_html(args.qid, t_rec, n_rec)

    args.output.write_text(html_out, encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
