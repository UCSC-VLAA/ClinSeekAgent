"""Model context windows and the prompt budget derived from them.

The Curated Input / Full-Record Input runners render a patient record into a
single prompt. The Full-Record view pastes the patient's entire pre-cutoff
history (median ~52k tokens, tail in the millions), so the prompt ceiling has
to be an explicit, checkable number rather than an afterthought.

Token counting
--------------
`CHARS_PER_TOKEN = 1.60` is calibrated, not a guess: measured against the
provider-reported input token counts of five rendered ClinSeek-Bench prompts
spanning 7k-2.0M characters, the ratio ran 1.61-1.87. Clinical records rendered
as `## <Section> [timestamp]` blocks with one `- Key: Value` line per field are
dense in ids, timestamps and punctuation, so they tokenize far worse than prose
-- an English-text rule of thumb (~4 characters per token, as used by
`run_text._estimate_tokens`) underestimates by more than 2x and produces
prompts that the provider rejects for exceeding the context window. The
conservative end of the measured range is used, because the two directions of
error are not symmetric: overestimating truncates a few rows slightly harder
than necessary, underestimating gets the request rejected and loses the row.

This module is the one place the ratio lives.
"""
from __future__ import annotations

CHARS_PER_TOKEN = 1.60


def approx_tokens(text: str) -> int:
    """Estimated token count for `text` at the clinical-CSV ratio."""
    return int(len(text) / CHARS_PER_TOKEN)


#: Matched as substrings against the model id, first hit wins, so the more
#: specific ids must come first. Values are *input* context windows.
#:
#: These are deliberately conservative published figures rather than the
#: largest a deployment might allow (several Claude models offer a larger
#: window as an opt-in tier). Getting this wrong in the generous direction
#: produces a hard 400 mid-run; getting it wrong in the conservative direction
#: only truncates more than necessary, and every row records exactly how much.
#: Pass --context-window to claim more once you have confirmed it. In practice
#: the runners clamp to DEFAULT_MAX_CONTEXT_WINDOW (100k, the paper setting).
CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic
    ("claude-opus-5", 1_000_000),
    ("claude-opus-4", 200_000),
    ("claude-sonnet-5", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-fable-5", 200_000),
    ("claude-mythos-5", 200_000),
    ("claude", 200_000),
    # ClinSeek-35B-A3B and its Qwen3.5 base as served by
    # scripts/run_vllm_server.sh (--max-model-len 196608).
    ("clinseek", 196_608),
    ("qwen3.5", 196_608),
    # Moonshot
    ("kimi-k2", 256_000),
    # OpenAI. Order matters -- these are substring matches, so "gpt-5.6" must
    # precede the generic "gpt-5" or a 5.6 model would inherit 5.x's window.
    ("gpt-5.6", 1_000_000),
    ("gpt-5.5", 400_000),
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    # Google
    ("gemini-3.1-pro", 1_000_000),
    ("gemini-3", 1_000_000),
    # Z.ai
    ("glm-5", 256_000),
    # Others served through the Bedrock OpenAI-compatible path
    ("minimax-m2", 200_000),
    ("qwen3-vl", 256_000),
    ("glm-4", 128_000),
)


#: Hard ceiling on the prompt window for the record-based inference modes
#: (paper setting: a 100k-token window for Curated Input and Full-Record Input).
#: Models with a larger window would otherwise receive far longer -- and far
#: more expensive -- Full-Record prompts, and the two record modes would no
#: longer be compared at the same window across models. Every caller passes
#: through this clamp; raise it deliberately with --max-context-window N.
DEFAULT_MAX_CONTEXT_WINDOW = 100_000


class UnknownContextWindow(ValueError):
    """Raised when a model id matches no registry entry and no override given."""


def resolve_context_window(
    model_id: str,
    override: int | None = None,
    cap: int | None = DEFAULT_MAX_CONTEXT_WINDOW,
) -> int:
    """Input context window to budget against for `model_id`, in tokens.

    Raises rather than guessing. A silently wrong window makes the truncated
    stratum indistinguishable from a bug, and the number is load-bearing for
    the Full Record results -- it decides how many cases get cut. Pass
    `override` (`--context-window`) for a model not listed, or to claim a
    larger tier you have confirmed for your deployment.

    `cap` clamps the result. The model's real ceiling and the ceiling we are
    willing to pay for are different numbers, and the expensive one should not
    win by default; `None` disables the clamp.
    """
    if override is not None:
        if override <= 0:
            raise ValueError(f"context window must be positive, got {override}")
        window = int(override)
    else:
        name = (model_id or "").lower()
        window = next((w for needle, w in CONTEXT_WINDOWS if needle in name), 0)
        if not window:
            raise UnknownContextWindow(
                f"no context window known for model {model_id!r}. Add it to "
                f"CONTEXT_WINDOWS in clinseekagent/context_limits.py, or pass "
                f"--context-window N to state it for this run."
            )
    if cap is not None and window > cap:
        print(f"[context] {model_id} accepts {window:,} tok; capped to "
              f"{cap:,} for this run (--max-context-window)", flush=True)
        window = int(cap)
    return window


def history_budget(
    context_window: int,
    *,
    max_tokens: int,
    thinking_budget: int = 0,
    overhead_tokens: int = 0,
    safety_frac: float = 0.05,
) -> int:
    """Tokens left for the patient record after everything else is reserved.

    The budget applies to the *whole* prompt, not the history block: the task
    instruction plus an inlined candidate vocabulary runs to several thousand
    tokens on some tasks, so the caller measures that overhead first and the
    history gets the remainder.

    `safety_frac` absorbs the ~5% error in the char/token estimate. Without it
    a prompt measured at exactly the limit lands just over it and 400s, which
    costs a whole row rather than a few hundred tokens of headroom.
    """
    if context_window <= 0:
        raise ValueError(f"context window must be positive, got {context_window}")
    reserved = max_tokens + max(0, thinking_budget) + max(0, overhead_tokens)
    budget = int(context_window * (1.0 - safety_frac)) - reserved
    if budget <= 0:
        raise ValueError(
            f"no room for the patient record: context_window={context_window}, "
            f"max_tokens={max_tokens}, thinking_budget={thinking_budget}, "
            f"overhead_tokens={overhead_tokens}, safety_frac={safety_frac}"
        )
    return budget
