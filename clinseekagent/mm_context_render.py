#!/usr/bin/env python3
"""Re-render the `<ehr_context>` block of an MM prompt from a published view.

Why this exists
---------------
An MM prompt (`input_text`) carries the patient record inside one
`<ehr_context>` block.  The corpus publishes two row sets per question,
`evidence/<qid>/recent/` and `evidence/<qid>/full/`, so each inference condition
(Curated Input = recent, Full-Record Input = full) needs a prompt rendered from
its own rows.

This module rebuilds the `<ehr_context>` body from the tables of a given view,
in the *same grammar the source prompt already uses*:

    ### <table>
    Rows visible before cutoff: <N>; rows included below: latest <M> of <N>
    <the view's CSV, header included>

`N` -- how many rows are visible at the cutoff -- is a statement about the
temporal filter, so it stays the full view's count.  `M` is how many of them
this prompt actually prints.  Everything outside `<ehr_context>` (instruction,
patient info, linked study ids, question) is left byte-identical, so the two MM
prompts differ in exactly one thing: which rows of the same pool they show.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

RENDER_VERSION = "mm-ehr-context-render-v1"

EHR_CONTEXT_RE = re.compile(r"(<ehr_context>)(.*?)(</ehr_context>)", re.DOTALL)
SECTION_RE = re.compile(r"^### (\S+)[ \t]*$", re.MULTILINE)


def section_tables(input_text: str) -> list[str]:
    """Table names the prompt's `<ehr_context>` prints, in prompt order."""
    m = EHR_CONTEXT_RE.search(str(input_text or ""))
    if not m:
        return []
    return SECTION_RE.findall(m.group(2))


def _csv_body(path: Path) -> tuple[str, int]:
    """`(text without trailing newline, data row count)` for a view CSV."""
    if not path.exists():
        return "", 0
    text = path.read_text(encoding="utf-8").rstrip("\n")
    if not text:
        return "", 0
    return text, max(0, len(text.split("\n")) - 1)


def render_context(
    view_dir: Path,
    tables: list[str],
    visible_rows: dict[str, int],
    csv_bodies: Optional[dict[str, tuple[str, int]]] = None,
) -> str:
    """The `<ehr_context>` body for `view_dir`, in the source prompt's grammar.

    `csv_bodies` maps table -> `(csv text, data row count)` and, when given,
    takes precedence over reading `view_dir`. It lets a caller print a subset
    of a view's rows -- the Full Record truncation in
    `clinseekagent/record_context.py` searches for the widest recency window
    that fits a context budget, and needs to price candidate windows without
    writing each one to disk first.
    """
    blocks: list[str] = []
    for name in tables:
        if csv_bodies is not None and name in csv_bodies:
            body, m = csv_bodies[name]
        else:
            body, m = _csv_body(view_dir / f"{name}.csv")
        n = int(visible_rows.get(name, m))
        if m <= 0:
            blocks.append(f"### {name}\nRows visible before cutoff: {n}")
            continue
        shown = f"latest {m} of {n}" if m < n else f"{m} of {n}"
        blocks.append(
            f"### {name}\nRows visible before cutoff: {n}; "
            f"rows included below: {shown}\n{body}"
        )
    return "\n\n" + "\n\n".join(blocks) + "\n\n"


def rebuild_input_text(
    input_text: str,
    view_dir: Path,
    visible_rows: dict[str, int],
    csv_bodies: Optional[dict[str, tuple[str, int]]] = None,
) -> tuple[Optional[str], str]:
    """Swap `<ehr_context>` for a render of `view_dir`.

    Returns `(new_input_text, status)`. `None` with a status is returned rather
    than a silently-unchanged prompt, so a case whose prompt has no
    `<ehr_context>` block is counted in the corpus manifest instead of shipping
    the other view's rows under the recent view's name.
    """
    text = str(input_text or "")
    if not text:
        return None, "no_input_text"
    m = EHR_CONTEXT_RE.search(text)
    if not m:
        return None, "no_ehr_context_block"
    tables = SECTION_RE.findall(m.group(2))
    if not tables:
        return None, "no_sections"
    body = render_context(view_dir, tables, visible_rows, csv_bodies)
    return text[: m.end(1)] + body + text[m.start(3):], "rebuilt"
