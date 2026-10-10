"""Task-category roll-up for ClinSeek-Bench scores (13 text / 6 MM categories).

The paper reports F1 per task category rather than per raw task label. The
mapping lives in `clinseekagent/resources/task_taxonomy.json`; a row's own
`category` field (present in the public manifests) takes precedence over it.
"""
from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "resources" / "task_taxonomy.json"


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict[str, Any]:
    return json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))


def category_order(bench: str) -> List[str]:
    return list(load_taxonomy()[bench]["categories"])


def category_of(bench: str, row: Dict[str, Any]) -> Optional[str]:
    """Category for a result / benchmark row of `bench` ("text" or "mm")."""
    explicit = row.get("category")
    if isinstance(explicit, str) and explicit:
        return explicit
    tax = load_taxonomy()[bench]
    task = row.get("task")
    if task in tax["by_task"]:
        return tax["by_task"][task]
    if bench == "mm":
        return tax.get("by_qid", {}).get(str(row.get("qid")))
    return None


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def group_scores(
    bench: str, rows: Iterable[Dict[str, Any]], score_key: str
) -> Dict[str, Dict[str, float]]:
    """`category -> {"count": n, "f1": mean score}` in the paper's order.

    Rows whose category cannot be resolved are collected under "uncategorized"
    rather than dropped, so a mapping gap is visible in the report.
    """
    buckets: Dict[str, List[float]] = {}
    for row in rows:
        cat = category_of(bench, row) or "uncategorized"
        buckets.setdefault(cat, []).append(float(row.get(score_key) or 0.0))
    order = category_order(bench) + sorted(
        c for c in buckets if c not in category_order(bench))
    return {c: {"count": len(buckets[c]), "f1": mean(buckets[c])}
            for c in order if c in buckets}


def paired_bootstrap_ci(
    deltas: Sequence[float], n_boot: int = 4000, seed: int = 0,
    alpha: float = 0.05,
) -> Tuple[float, float, float]:
    """Mean paired difference and its bootstrap (1 - alpha) interval.

    Questions are resampled with replacement (paper: 4,000 resamples).
    """
    if not deltas:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(
        sum(deltas[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(n_boot))
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return mean(deltas), lo, hi
