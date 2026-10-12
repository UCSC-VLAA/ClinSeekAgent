"""Multimodal ClinSeekAgent runner -- EHR + image tools (paper setting: 22 tools).

This file is the multimodal twin of `run_text.py`. Key differences:

1. User message is built as a list of Anthropic content blocks: the sample's
   pre-rendered `question` text, each `image_paths[*]` attached as a base64
   image block (downscaled to `--image_max_edge`), and each `report_paths[*]`
   inlined as an extra text block. The existing Bedrock backend already
   supports list-typed `content` for user messages.

2. Tool schemas come from `prompts_multimodal.get_tools_mm`: the 16 EHR tools,
   plus the 6 image tools with `--enable_image` (the paper setting), plus the
   optional browser tools with `--enable_browser`.

3. `image_tool_pool.ImageToolPool` routes `image.*` tool calls to the image
   MCP server (default http://127.0.0.1:5203/mcp).

Shared helpers (normalize_tool_calls, truncate_tool_result, load_query_data,
BrowserPool, BedrockAsyncGenerator, ...) are imported from `run_text.py`
unchanged.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import dotenv

# --- Re-use unmodified helpers from the text-only agentic runner. -------------
from run_text import (
    BEDROCK_MODEL_ALIASES,
    DEFAULT_BEDROCK_MODEL_ID,
    DEFAULT_BEDROCK_REGION,
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    DEFAULT_VLLM_API_KEY,
    DEFAULT_VLLM_BASE_URL,
    MAX_PARALLEL_QUERIES,
    BrowserPool,
    attach_source_fields,
    configure_bedrock_auth,
    load_query_data,
    mask_optional_secret,
    mask_secret,
    normalize_browser_tool_args,  # noqa: F401
    normalize_tool_calls,
    resolve_bedrock_model_id,
    resolve_qid,
    summarize_conversation_completion,
    truncate_tool_result,
    # single-pass retrieval -- orientation and directive are EHR-only and
    # identical across text and MM, so reuse rather than fork.
    _extract_subject_and_time,
    _estimate_tokens,
    _run_orientation,
    _SINGLE_SHOT_DIRECTIVE,
    _ORIENTATION_TOOLS,
)
from ehr_tool_pool import EHRToolPool
from image_tool_pool import ImageToolPool
from multimodal_manifest_policy import (
    STRICT_POLICY_VERSION,
    validate_agentic_manifest,
)
from prompts_multimodal import (
    COMBINED_TOOL_CONTENT_MM,
    get_developer_content_mm,
    get_tools_mm,
    DEVELOPER_CONTENT_CLAUDE_MM,
)


# Suffix → correct namespace. Some OSS models (Kimi in particular) drop or
# swap the namespace prefix when rendering OpenAI-style tool calls, so we
# keep a lookup that maps the bare tool name back to the right family.
# `COMBINED_TOOL_CONTENT_MM` is the JSON string we ship to Bedrock; parse it
# once at import to build the lookup.
def _build_tool_namespace_lookup() -> Dict[str, str]:
    try:
        tools = json.loads(COMBINED_TOOL_CONTENT_MM) if isinstance(
            COMBINED_TOOL_CONTENT_MM, str
        ) else COMBINED_TOOL_CONTENT_MM
    except Exception:
        return {}
    lookup: Dict[str, str] = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or (t.get("function") or {}).get("name")
        if isinstance(name, str) and "." in name:
            ns, suffix = name.split(".", 1)
            lookup[suffix] = ns
    return lookup


_TOOL_NAMESPACE_BY_SUFFIX: Dict[str, str] = _build_tool_namespace_lookup()


def _fuzzy_match_suffix(bare_name: str) -> Optional[str]:
    """Fallback: suffix-contains or contains-suffix match against registered tools.

    Kimi occasionally emits shortened names like `visualizer` (for
    `image_visualizer`) or `sql_query` (for `run_sql_query`). When the exact
    suffix isn't in the registry, return the first registered suffix whose
    name either contains the bare name or is contained within it. Ties are
    broken deterministically by picking the shortest candidate.
    """
    bare = bare_name.strip()
    if not bare:
        return None
    candidates = []
    for suffix in _TOOL_NAMESPACE_BY_SUFFIX:
        if bare == suffix:
            return suffix
        if bare in suffix or suffix in bare:
            candidates.append(suffix)
    if not candidates:
        return None
    candidates.sort(key=lambda s: (abs(len(s) - len(bare)), len(s)))
    return candidates[0]


def _resolve_tool_namespace(function_name: str) -> str:
    """Return the canonical `namespace.tool` name, fixing mis-namespaced calls.

    Many OSS models sometimes emit `ehr.chest_xray_classifier` (wrong) or a
    bare `chest_xray_classifier` (no namespace). Route by suffix rather than
    rejecting, so the agent can still make forward progress.
    """
    if "." in function_name:
        ns, suffix = function_name.split(".", 1)
        expected = _TOOL_NAMESPACE_BY_SUFFIX.get(suffix)
        if expected and expected != ns:
            return f"{expected}.{suffix}"
        if expected:
            return function_name
        # Suffix unknown — try fuzzy match
        fuzzy = _fuzzy_match_suffix(suffix)
        if fuzzy:
            return f"{_TOOL_NAMESPACE_BY_SUFFIX[fuzzy]}.{fuzzy}"
        return function_name
    expected = _TOOL_NAMESPACE_BY_SUFFIX.get(function_name)
    if expected:
        return f"{expected}.{function_name}"
    fuzzy = _fuzzy_match_suffix(function_name)
    if fuzzy:
        return f"{_TOOL_NAMESPACE_BY_SUFFIX[fuzzy]}.{fuzzy}"
    return function_name

VERBOSE = False


def vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)


# --- Multimodal message assembly ---------------------------------------------
_VALID_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MEDIA_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_MAX_REPORT_CHARS = 4000
# The agent loop hard-codes this per-request cap; named so run_config can
# record the same value the requests actually carry.
DEFAULT_MM_MAX_TOKENS = 8192
_MAX_IMAGES_PER_SAMPLE = 4


def _sha256_file(path: str) -> Optional[str]:
    """Digest of the benchmark manifest, so a run can be tied to its inputs."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def resolve_asset_path(path: str, bench_root) -> Optional[Path]:
    """Resolve a manifest-relative path to an absolute on-disk path.

    `bench_root` accepts either a single root or a list / tuple of roots; each
    root is tried in order. This lets a combined test set that mixes EHRXQA
    and MedMod rows resolve both without rewriting paths.
    """
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    # Released manifests prefix asset paths with the source package name
    # (`EHRXQAOriginalLinked_v1/...`, `MedModOriginalLinked_v1/...`), while the
    # rebuilt roots (`mm_bench/ehrxqa`, `mm_bench/medmod`) start at
    # `mimic-cxr/...`; try both spellings, as run_multimodal_curated does.
    relatives = [path]
    first, sep, rest = path.partition("/")
    if sep and (first.endswith("OriginalLinked_v1") or first.endswith("AgentBench_v3")):
        relatives.append(rest)
    if bench_root:
        roots = [bench_root] if isinstance(bench_root, (str, Path)) else list(bench_root)
        for rel in relatives:
            for root in roots:
                if not root:
                    continue
                rooted = Path(root) / rel
                if rooted.exists():
                    return rooted
    if candidate.exists():
        return candidate.resolve()
    return None


def split_bench_roots(raw_value: str) -> List[str]:
    """Split a BENCH_ROOT value while preserving Windows drive prefixes."""
    roots: List[str] = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        is_windows_drive = (
            len(chunk) >= 3
            and chunk[1] == ":"
            and chunk[0].isalpha()
            and chunk[2] in ("\\", "/")
        )
        if is_windows_drive:
            roots.append(chunk)
        else:
            roots.extend(part for part in chunk.split(":") if part)
    return roots


def _load_image_as_block(
    path: Path, max_edge: int
) -> Optional[Dict[str, Any]]:
    """Load an image from disk, downscale, and wrap as an Anthropic image block."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for multimodal runs: pip install Pillow"
        ) from exc

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if max_edge and longest > max_edge:
                scale = max_edge / float(longest)
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print(f"[mm] failed to load image {path}: {exc}")
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": data,
        },
    }


def _load_report_text(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[mm] failed to read report {path}: {exc}")
        return None
    text = text.strip()
    if len(text) > _MAX_REPORT_CHARS:
        text = text[:_MAX_REPORT_CHARS] + "\n[report truncated]"
    return text


def build_multimodal_user_content(
    question_text: str,
    image_paths: List[str],
    report_paths: List[str],
    bench_root: Optional[str],
    image_max_edge: int,
    has_subject_id: bool = True,
    is_cohort_scope: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Construct the Anthropic-style content-block list for the user turn.

    Auto-attachment is strictly conditional:
    - image blocks are appended only if `image_paths` resolves to at least one
      on-disk file; samples without images get no image block.
    - report text is inlined only if `report_paths` resolves.

    A small `<run_context>` preamble is prepended so the model knows what is
    actually available (images / reports / patient EHR), avoiding hallucinated
    tool calls on rows that lack a subject_id or an image.
    """
    resolved_images: List[str] = []
    dropped_images: List[str] = []
    image_blocks: List[Dict[str, Any]] = []
    for raw_path in (image_paths or [])[:_MAX_IMAGES_PER_SAMPLE]:
        resolved = resolve_asset_path(raw_path, bench_root)
        if resolved is None or resolved.suffix.lower() not in _VALID_IMAGE_SUFFIXES:
            dropped_images.append(raw_path)
            continue
        block = _load_image_as_block(resolved, image_max_edge)
        if block is None:
            dropped_images.append(str(resolved))
            continue
        image_blocks.append(block)
        resolved_images.append(str(resolved))

    resolved_reports: List[str] = []
    report_blocks: List[Dict[str, Any]] = []
    for raw_path in report_paths or []:
        resolved = resolve_asset_path(raw_path, bench_root)
        if resolved is None:
            continue
        text = _load_report_text(resolved)
        if not text:
            continue
        report_blocks.append({
            "type": "text",
            "text": (
                f"\n<linked_radiology_report path=\"{resolved}\">\n"
                f"{text}\n</linked_radiology_report>\n"
            ),
        })
        resolved_reports.append(str(resolved))

    # Preamble describing what's actually attached this turn.
    availability_lines = [
        "<run_context>",
        f"- images_attached: {len(resolved_images)}",
        f"- reports_inlined: {len(resolved_reports)}",
        f"- ehr_database_available: {'yes' if has_subject_id else 'no'}"
        + (
            "  (cohort-scope pseudo-subject database)"
            if is_cohort_scope and has_subject_id
            else ""
        ),
    ]
    if resolved_images:
        availability_lines.append("- image_paths (use these VERBATIM in image.* calls):")
        for p in resolved_images:
            availability_lines.append(f"    {p}")
    availability_lines.extend([
        "",
        "Only call tools whose preconditions are satisfied:",
        "- call ehr.load_ehr ONLY if ehr_database_available=yes",
        "- call image.* tools ONLY if images_attached>0 — use the absolute",
        "  image_paths listed above, NOT a derived path from study_id",
        "- if neither is available, answer from the question text alone",
        "</run_context>",
    ])
    preamble = "\n".join(availability_lines)

    blocks: List[Dict[str, Any]] = [
        {"type": "text", "text": preamble + "\n\n" + question_text},
    ]
    blocks.extend(image_blocks)
    blocks.extend(report_blocks)

    summary = {
        "num_images": len(resolved_images),
        "num_reports": len(resolved_reports),
        "resolved_image_paths": resolved_images,
        "resolved_report_paths": resolved_reports,
        "dropped_image_paths": dropped_images,
        "has_subject_id": bool(has_subject_id),
        "is_cohort_scope": bool(is_cohort_scope),
    }
    return blocks, summary


def resolve_question_mm(
    item: Dict[str, Any], bench_root: Optional[str], image_max_edge: int
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Return (plain_text_question, content_blocks, summary).

    Does NOT force-attach anything. If the sample has no `image_paths`, no
    image block is added. If it has no `subject_id`, the preamble tells the
    model the EHR database is unavailable. Cohort-scope rows use their
    packaged pseudo-subject database.
    """
    question_text = item.get("question") or item.get("query") or ""
    image_paths = item.get("image_paths") or []
    report_paths = item.get("report_paths") or []
    subject_id = item.get("subject_id")
    scope = item.get("scope")
    blocks, summary = build_multimodal_user_content(
        question_text=question_text,
        image_paths=image_paths,
        report_paths=report_paths,
        bench_root=bench_root,
        image_max_edge=image_max_edge,
        has_subject_id=subject_id not in (None, "", 0),
        is_cohort_scope=(scope == "cohort_scope"),
    )
    return question_text, blocks, summary


# --- Multi-tool normalizer (extends the text-only normalize_tool_call_name). --
def normalize_tool_call_name_mm(function_name: str) -> str:
    name = (function_name or "").strip()
    if not name:
        return name
    if name.startswith("browser.") or name.startswith("ehr.") or name.startswith("image."):
        return name
    if name.startswith("image_"):
        suffix = name[len("image_"):].strip("_")
        return f"image.{suffix}" if suffix else "image."
    if name.startswith("browser_"):
        suffix = name[len("browser_"):].strip("_")
        return f"browser.{suffix}" if suffix else "browser.search"
    if name in {"search", "open", "find"}:
        return f"browser.{name}"
    if name.startswith("ehr_"):
        suffix = name[len("ehr_"):].strip("_")
        return f"ehr.{suffix}" if suffix else "ehr."
    if "." not in name:
        # Default-route to EHR to match text-only behavior.
        return f"ehr.{name}"
    return name


def normalize_tool_calls_mm(tool_calls: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for tc in tool_calls or []:
        out = dict(tc)
        function = dict(tc.get("function") or {})
        function["name"] = normalize_tool_call_name_mm(function.get("name", ""))
        out["function"] = function
        normalized.append(out)
    return normalized


# --- Core tool-calling loop ---------------------------------------------------
async def run_one_native_mm(
    *,
    question_text: str,
    user_content_blocks: List[Dict[str, Any]],
    qid: Any,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: Optional[EHRToolPool],
    image_pool: Optional[ImageToolPool],
    max_rounds: int,
    temperature: float,
    max_tool_result_chars: int,
    model_name: str,
) -> List[dict]:
    browser_pool.init_session(qid)
    if ehr_pool:
        await ehr_pool.init_session(qid)
    if image_pool:
        await image_pool.init_session(qid)

    if hasattr(generator, "_init_tokenizer"):
        await generator._init_tokenizer()

    browser_enabled = getattr(browser_pool, "enabled", True)
    image_enabled = image_pool is not None
    system_prompt = get_developer_content_mm(browser_enabled, image_enabled)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content_blocks},
    ]
    tools = get_tools_mm(browser_enabled, image_enabled)

    round_num = 0
    try:
        while round_num < max_rounds:
            round_num += 1
            print(f"\n[qid={qid}] {'='*50}")
            print(f"[qid={qid}] MM Round {round_num}/{max_rounds} | msgs_so_far={len(messages)}")
            print(f"[qid={qid}] {'='*50}", flush=True)

            response = await generator.chat_completion(
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=8192,
            )

            message = response["choices"][0]["message"]
            content = message.get("content") or ""
            reasoning_content = (message.get("reasoning_content") or "").strip()
            tool_calls = message.get("tool_calls") or []
            parse_error = (message.get("parse_error") or "").strip()

            preview_text = content or reasoning_content
            preview_text = preview_text[:2000] if len(preview_text) > 2000 else preview_text
            preview_label = "CONTENT" if content else "REASONING"
            print(
                f"[qid={qid}] Round {round_num} MODEL RESPONSE: "
                f"content_len={len(content)}, reasoning_len={len(reasoning_content)}, "
                f"tool_calls={len(tool_calls)}"
            )
            print(
                f"[qid={qid}] Round {round_num} {preview_label} PREVIEW: {preview_text!r}",
                flush=True,
            )
            if reasoning_content and content:
                print(
                    f"[qid={qid}] Round {round_num} REASONING PREVIEW: "
                    f"{reasoning_content[:2000]!r}",
                    flush=True,
                )
            if parse_error and not tool_calls:
                print(
                    f"[qid={qid}] Round {round_num} PARSE ERROR: {parse_error}",
                    flush=True,
                )

            # Use the mm-aware normalizer so `image.*` names pass through.
            normalized_tool_calls = normalize_tool_calls_mm(tool_calls)

            assistant_message = {
                "role": "assistant",
                "content": content,
                "tool_calls": normalized_tool_calls if normalized_tool_calls else None,
            }
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            if message.get("bedrock_content_blocks"):
                assistant_message["bedrock_content_blocks"] = message["bedrock_content_blocks"]
            messages.append(assistant_message)

            if not tool_calls:
                print(
                    f"[qid={qid}] Round {round_num}: No tool calls detected; stopping",
                    flush=True,
                )
                break

            finish_tool_called = False
            for tc_idx, (raw_tool_call, tool_call) in enumerate(
                zip(tool_calls, normalized_tool_calls)
            ):
                tool_id = tool_call["id"]
                function_name_raw = tool_call["function"]["name"]
                function_name = _resolve_tool_namespace(function_name_raw)
                if function_name != function_name_raw:
                    print(
                        f"[qid={qid}] Round {round_num} TOOL_NAME_FIX: "
                        f"{function_name_raw!r} -> {function_name!r}",
                        flush=True,
                    )
                function_args_raw = tool_call["function"]["arguments"]

                try:
                    if isinstance(function_args_raw, dict):
                        function_args = function_args_raw
                    else:
                        function_args = json.loads(function_args_raw)

                    print(
                        f"[qid={qid}] Round {round_num} TOOL_CALL[{tc_idx}]: "
                        f"{function_name}({json.dumps(function_args, ensure_ascii=False)[:200]})",
                        flush=True,
                    )

                    if function_name.startswith("ehr."):
                        if ehr_pool:
                            actual = function_name.split(".", 1)[1]
                            result = await ehr_pool.call_tool(qid, actual, function_args)
                        else:
                            result = "Error: EHR tools not available. Start with --enable_ehr flag."
                    elif function_name.startswith("browser."):
                        actual = function_name.split(".", 1)[1]
                        result = await browser_pool.call_tool(qid, actual, function_args)
                        if not result:
                            result = f"{function_name} completed"
                    elif function_name.startswith("image."):
                        if image_pool:
                            result = await image_pool.call_tool(qid, function_name, function_args)
                        else:
                            result = "Error: Image tools not available. Start with --enable_image."
                    else:
                        result = f"Unknown tool namespace: {function_name}"

                    original_result_len = len(result) if isinstance(result, str) else None
                    result = truncate_tool_result(result, max_tool_result_chars)

                    tool_message = {
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tool_id,
                    }
                    messages.append(tool_message)

                    result_preview = result[:200] if len(result) > 200 else result
                    if original_result_len is not None and original_result_len > len(result):
                        print(
                            f"[qid={qid}] Round {round_num} TOOL_RESULT_TRUNCATED[{tc_idx}]: "
                            f"{original_result_len} -> {len(result)} chars",
                            flush=True,
                        )
                    print(
                        f"[qid={qid}] Round {round_num} TOOL_RESULT[{tc_idx}]: "
                        f"len={len(result)}, preview={result_preview!r}",
                        flush=True,
                    )

                    actual_finish = (
                        function_name.split(".", 1)[1] if "." in function_name else function_name
                    )
                    if actual_finish == "finish":
                        finish_tool_called = True
                except Exception as exc:
                    error_msg = truncate_tool_result(
                        f"Error executing {function_name}: {exc}", max_tool_result_chars
                    )
                    print(
                        f"[qid={qid}] Round {round_num} TOOL_ERROR[{tc_idx}]: {error_msg}",
                        flush=True,
                    )
                    err_message = {
                        "role": "tool",
                        "content": error_msg,
                        "tool_call_id": tool_id,
                    }
                    messages.append(err_message)

            if finish_tool_called:
                print(f"[qid={qid}] DONE Round {round_num}: ehr.finish called", flush=True)
                break
            continue

        print(f"[qid={qid}] MM finished after {round_num} rounds, msgs={len(messages)}", flush=True)
        return messages
    finally:
        browser_pool.cleanup(qid)
        if ehr_pool:
            await ehr_pool.cleanup(qid)
        if image_pool:
            await image_pool.cleanup(qid)


async def run_one_single_shot_mm(
    *,
    question_text: str,
    user_content_blocks: List[Dict[str, Any]],
    qid: Any,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: Optional[EHRToolPool],
    image_pool: Optional[ImageToolPool],
    evidence_budget_tokens: Optional[int],
    max_tool_result_chars: int,
    temperature: float,
    model_name: str,
) -> Tuple[List[dict], Dict[str, Any]]:
    """Single-pass retrieval (`--retrieval_mode single_shot`) for MM.

    Same three phases as the text-only version (free EHR schema orientation ->
    one retrieval turn -> forced finish), but the first user turn carries the
    attached CXR image blocks, and the retrieval turn keeps the image tools so
    the single plan can inspect the image as well as query the EHR. The image
    itself is always visible; what is constrained is retrieval to one turn.
    """
    browser_pool.init_session(qid)
    if ehr_pool:
        await ehr_pool.init_session(qid)
    if image_pool:
        await image_pool.init_session(qid)
    if hasattr(generator, "_init_tokenizer"):
        await generator._init_tokenizer()

    stats: Dict[str, Any] = {
        "retrieval_mode": "single_shot",
        "n_retrieval_calls": 0,
        "tool_result_tokens": 0,
        "evidence_budget_tokens": evidence_budget_tokens,
        "budget_exceeded": False,
        "orientation_ok": False,
    }
    subject_id, timestamp = _extract_subject_and_time(question_text)

    try:
        # ── Phase 0: EHR schema orientation (free) ───────────────────────────
        orientation = ""
        if ehr_pool and subject_id and timestamp:
            try:
                orientation = await _run_orientation(
                    ehr_pool, qid, subject_id, timestamp, max_tool_result_chars)
                stats["orientation_ok"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"[qid={qid}] MM single-shot orientation failed: {exc}",
                      flush=True)

        # Append the orientation + directive as a trailing text block, leaving
        # the image blocks in place.
        user_blocks = list(user_content_blocks)
        if orientation:
            user_blocks.append({"type": "text",
                                "text": f"\n\n{orientation}{_SINGLE_SHOT_DIRECTIVE}"})

        messages: List[dict] = [
            {"role": "system", "content": get_developer_content_mm(
                getattr(browser_pool, "enabled", True), image_pool is not None)},
            {"role": "user", "content": user_blocks},
        ]

        all_tools = get_tools_mm(
            getattr(browser_pool, "enabled", True), image_pool is not None)

        def _base(name: str) -> str:
            return name.split(".", 1)[1] if "." in name else name
        retrieval_tools = [
            t for t in all_tools
            if t["function"]["name"] != "ehr.finish"
            and _base(t["function"]["name"]) not in _ORIENTATION_TOOLS
        ]
        finish_tool = [t for t in all_tools
                       if t["function"]["name"] == "ehr.finish"]

        # ── Phase 1: the single retrieval turn ───────────────────────────────
        print(f"[qid={qid}] MM single-shot PHASE 1: retrieval", flush=True)
        r1 = await generator.chat_completion(
            messages=messages, tools=retrieval_tools, tool_choice="any",
            temperature=temperature, max_tokens=8192)
        m1 = r1["choices"][0]["message"]
        tool_calls = m1.get("tool_calls") or []
        normalized = normalize_tool_calls_mm(tool_calls)
        stats["n_retrieval_calls"] = len(tool_calls)
        print(f"[qid={qid}] MM single-shot: model issued {len(tool_calls)} "
              f"tool call(s) in one turn", flush=True)

        assistant_turn = {"role": "assistant", "content": m1.get("content", ""),
                          "tool_calls": normalized if normalized else None}
        if m1.get("reasoning_content"):
            assistant_turn["reasoning_content"] = m1["reasoning_content"]
        if m1.get("bedrock_content_blocks"):
            assistant_turn["bedrock_content_blocks"] = m1["bedrock_content_blocks"]
        messages.append(assistant_turn)

        for tc, ntc in zip(tool_calls, normalized):
            tool_id = ntc["id"]
            fname_raw = ntc["function"]["name"]
            # Mirror the agentic loop: a model that merges the two namespaces
            # (`ehr.image_chest_xray_classifier`) must still reach the image
            # server. Without this the call is dispatched to the EHR MCP, which
            # answers "Unknown tool", and the arm silently loses every image
            # tool while still reporting status=success.
            fname = _resolve_tool_namespace(fname_raw)
            if fname != fname_raw:
                print(f"[qid={qid}] MM single-shot TOOL_NAME_FIX: "
                      f"{fname_raw!r} -> {fname!r}", flush=True)
            try:
                args = ntc["function"]["arguments"]
                args = args if isinstance(args, dict) else json.loads(args)
            except Exception:
                args = {}

            over_budget = (evidence_budget_tokens is not None
                           and stats["tool_result_tokens"] >= evidence_budget_tokens)
            if over_budget:
                stats["budget_exceeded"] = True
                result = "[omitted: evidence budget exhausted]"
            else:
                try:
                    if fname.startswith("ehr.") and ehr_pool:
                        result = await ehr_pool.call_tool(
                            qid, fname.split(".", 1)[1], args)
                    elif fname.startswith("image.") and image_pool:
                        # MM convention: image pool takes the full dotted name.
                        result = await image_pool.call_tool(qid, fname, args)
                    elif fname.startswith("browser."):
                        result = await browser_pool.call_tool(
                            qid, fname.split(".", 1)[1], args) or f"{fname} completed"
                    else:
                        result = f"Tool unavailable: {fname}"
                except Exception as exc:  # noqa: BLE001
                    result = f"Error executing {fname}: {exc}"
                result = truncate_tool_result(result, max_tool_result_chars)
                stats["tool_result_tokens"] += _estimate_tokens(result)

            messages.append({"role": "tool", "tool_call_id": tool_id,
                             "content": result})
            print(f"[qid={qid}] MM single-shot TOOL[{fname}] result_len={len(result)}"
                  + ("  [OVER BUDGET]" if over_budget else ""), flush=True)

        # ── Phase 2: answer, finish only ─────────────────────────────────────
        print(f"[qid={qid}] MM single-shot PHASE 2: answer "
              f"(tool_result_tokens={stats['tool_result_tokens']})", flush=True)
        r2 = await generator.chat_completion(
            messages=messages, tools=finish_tool, tool_choice="ehr.finish",
            temperature=temperature, max_tokens=8192)
        m2 = r2["choices"][0]["message"]
        normalized2 = normalize_tool_calls_mm(m2.get("tool_calls") or [])

        answer_turn = {"role": "assistant", "content": m2.get("content", ""),
                       "tool_calls": normalized2 if normalized2 else None}
        if m2.get("reasoning_content"):
            answer_turn["reasoning_content"] = m2["reasoning_content"]
        if m2.get("bedrock_content_blocks"):
            answer_turn["bedrock_content_blocks"] = m2["bedrock_content_blocks"]
        messages.append(answer_turn)

        if normalized2:
            for ntc in normalized2:
                messages.append({"role": "tool", "tool_call_id": ntc["id"],
                                 "content": "Finish (single-shot)"})

        return messages, stats
    finally:
        browser_pool.cleanup(qid)
        if ehr_pool:
            await ehr_pool.cleanup(qid)
        if image_pool:
            await image_pool.cleanup(qid)


async def run_one_query_mm(
    *,
    item: Dict[str, Any],
    qid: Any,
    session_id: Any,
    run_index: int,
    runs_per_question: int,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: Optional[EHRToolPool],
    image_pool: Optional[ImageToolPool],
    bench_root: Optional[str],
    image_max_edge: int,
    max_rounds: int,
    temperature: float,
    max_tool_result_chars: int,
    model_name: str,
    retrieval_mode: str = "agentic",
    evidence_budget_tokens: Optional[int] = None,
):
    try:
        if ehr_pool:
            ehr_pool.configure_session(session_id, item)
        question_text, user_blocks, mm_summary = resolve_question_mm(
            item, bench_root=bench_root, image_max_edge=image_max_edge
        )
        if image_pool:
            # Bind image.* calls to this row's linked assets. The absolute
            # paths come from the summary because those are the spellings the
            # run_context preamble shows the model.
            image_pool.configure_session(
                session_id,
                item,
                resolved_assets=(
                    mm_summary["resolved_image_paths"]
                    + mm_summary["resolved_report_paths"]
                ),
            )
        single_shot_stats = None
        if retrieval_mode == "single_shot":
            messages, single_shot_stats = await run_one_single_shot_mm(
                question_text=question_text,
                user_content_blocks=user_blocks,
                qid=session_id,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                image_pool=image_pool,
                evidence_budget_tokens=evidence_budget_tokens,
                max_tool_result_chars=max_tool_result_chars,
                temperature=temperature,
                model_name=model_name,
            )
        else:
            messages = await run_one_native_mm(
                question_text=question_text,
                user_content_blocks=user_blocks,
                qid=session_id,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                image_pool=image_pool,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tool_result_chars=max_tool_result_chars,
                model_name=model_name,
            )
        completed, stop_reason = summarize_conversation_completion(messages)
        result = {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": question_text,
            "messages": messages,
            "completed": completed,
            "status": "success" if completed else "incomplete",
            "stop_reason": stop_reason,
            "multimodal_summary": mm_summary,
        }
        if single_shot_stats is not None:
            result["single_shot"] = single_shot_stats
        return result
    except Exception as exc:
        print(f"[qid={qid}] ERROR: {exc}")
        traceback.print_exc()
        return {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": item.get("question", item.get("query", "")),
            "messages": [],
            "completed": False,
            "status": "error",
            "stop_reason": "exception",
            "error": str(exc),
        }


async def process_query_item_mm(
    *,
    item: Dict[str, Any],
    task_index: int,
    total_task_runs: int,
    question_index: int,
    total_questions: int,
    run_index: int,
    runs_per_question: int,
    generator: Any,
    browser_pool: BrowserPool,
    ehr_pool: Optional[EHRToolPool],
    image_pool: Optional[ImageToolPool],
    bench_root: Optional[str],
    image_max_edge: int,
    max_rounds: int,
    temperature: float,
    max_tool_result_chars: int,
    semaphore: asyncio.Semaphore,
    out_f: Any,
    output_file: str,
    write_lock: asyncio.Lock,
    model_name: str,
    retrieval_mode: str = "agentic",
    evidence_budget_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    qid = resolve_qid(item)
    session_id = f"{qid}__q_{question_index}__run_{run_index}"
    try:
        async with semaphore:
            preview_q = (item.get("question") or item.get("query") or "")[:100]
            print("\n" + "=" * 80)
            print(
                f"Processing task {task_index}/{total_task_runs} | "
                f"question {question_index}/{total_questions} | "
                f"run {run_index}/{runs_per_question} | "
                f"qid={qid}: {preview_q}..."
            )
            print("=" * 80)
            result = await run_one_query_mm(
                item=item,
                qid=qid,
                session_id=session_id,
                run_index=run_index,
                runs_per_question=runs_per_question,
                generator=generator,
                browser_pool=browser_pool,
                ehr_pool=ehr_pool,
                image_pool=image_pool,
                bench_root=bench_root,
                image_max_edge=image_max_edge,
                max_rounds=max_rounds,
                temperature=temperature,
                max_tool_result_chars=max_tool_result_chars,
                model_name=model_name,
                retrieval_mode=retrieval_mode,
                evidence_budget_tokens=evidence_budget_tokens,
            )
            result = attach_source_fields(result, item)
    except Exception as exc:
        print(f"[qid={qid}] ERROR before completion: {exc}")
        traceback.print_exc()
        result = {
            "qid": qid,
            "run_index": run_index,
            "runs_per_question": runs_per_question,
            "session_id": session_id,
            "question": item.get("question", item.get("query", "")),
            "messages": [],
            "completed": False,
            "status": "error",
            "stop_reason": "exception_before_completion",
            "error": str(exc),
        }
        result = attach_source_fields(result, item)

    async with write_lock:
        out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
        out_f.flush()
        print(
            f"[qid={result['qid']}] task {task_index}/{total_task_runs} | "
            f"run {result.get('run_index', '?')}/{result.get('runs_per_question', '?')} "
            f"written to {output_file}"
        )
    return result


async def main():
    dotenv.load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

    parser = argparse.ArgumentParser(description="ClinSeekAgent multimodal evaluation driver")
    parser.add_argument("--backend", type=str, choices=["bedrock", "vertex", "vllm"], default="bedrock",
                        help="LLM backend. 'vllm' takes any OpenAI-compatible "
                             "endpoint serving a vision-capable model.")
    parser.add_argument("--model_name_or_path", type=str, default=DEFAULT_BEDROCK_MODEL_ID)
    parser.add_argument("--use_bedrock", action="store_true", default=False,
                        help="Deprecated alias for --backend bedrock.")
    parser.add_argument("--bedrock_model_id", type=str, default=None)
    parser.add_argument("--bedrock_region", type=str, default=DEFAULT_BEDROCK_REGION)
    parser.add_argument("--bedrock_api_key", type=str, default=None)
    parser.add_argument("--vertex_model_id", type=str, default=None,
                        help="Vertex AI model ID (overrides model_name_or_path).")
    parser.add_argument("--vertex_project", type=str, default=None,
                        help="GCP project for Vertex AI (defaults to "
                             "ANTHROPIC_VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT).")
    parser.add_argument("--vertex_region", type=str, default=None,
                        help="Vertex AI region, e.g. global / us-east5 "
                             "(defaults to CLOUD_ML_REGION or 'global').")
    parser.add_argument("--api_base_url", type=str, default=DEFAULT_VLLM_BASE_URL,
                        help="OpenAI-compatible API base URL (--backend vllm).")
    parser.add_argument("--api_key", type=str, default=DEFAULT_VLLM_API_KEY,
                        help="API key for the OpenAI-compatible API.")

    # Reasoning / request knobs, mirroring run_text.py.
    parser.add_argument("--thinking_budget_tokens", type=int, default=4096,
                        help="Claude extended-thinking budget for models that "
                             "accept a fixed budget (Claude 4.6 and older).")
    parser.add_argument("--reasoning_effort", type=str, default="high",
                        help="Effort for adaptive-only Claude models and OpenAI "
                             "reasoning models when thinking is enabled.")
    parser.add_argument("--api_max_attempts", type=int, default=3,
                        help="--backend vllm: attempts per request for transient "
                             "errors.")
    parser.add_argument("--api_retry_base_seconds", type=float, default=5.0,
                        help="--backend vllm: initial retry delay.")
    parser.add_argument("--api_retry_max_seconds", type=float, default=60.0,
                        help="--backend vllm: maximum retry delay.")
    parser.add_argument("--api_min_request_interval_seconds", type=float,
                        default=0.0,
                        help="--backend vllm: minimum spacing between request starts.")

    parser.add_argument("--enable_browser", action="store_true",
                        help="Add the optional browser.* web-search tools (not "
                             "part of the paper's tool space).")
    parser.add_argument("--search_url", type=str, default="http://localhost:8001")
    parser.add_argument("--browser_backend", type=str, default="local", choices=["local", "serper"])

    parser.add_argument("--enable_ehr", action="store_true",
                        help="Enable EHR MCP tools.")
    parser.add_argument("--ehr_mcp_url", type=str, default="http://127.0.0.1:5003/mcp",
                        help="Default EHR MCP URL. Used for samples whose "
                             "source_benchmark has no explicit override.")
    parser.add_argument("--ehr_mcp_url_ehrxqa", type=str, default=None,
                        help="Override EHR MCP URL for source_benchmark=ehrxqa samples.")
    parser.add_argument("--ehr_mcp_url_medmod", type=str, default=None,
                        help="Override EHR MCP URL for source_benchmark=medmod samples.")
    parser.add_argument("--disable_browser", action="store_true",
                        help="Deprecated no-op; browser tools are off unless "
                             "--enable_browser.")
    parser.add_argument("--enable_image", action="store_true",
                        help="Enable the 6 medical-image MCP tools (paper setting "
                             "for ClinSeek-Bench-MM).")
    parser.add_argument("--image_mcp_url", type=str, default="http://127.0.0.1:5203/mcp")
    parser.add_argument("--bench_root", type=str, default=None,
                        help="Benchmark root for resolving relative image/report paths. "
                             "Accepts a colon- or comma-separated list of roots; "
                             "they are tried in order.")
    parser.add_argument("--image_max_edge", type=int, default=1568,
                        help="Downscale image longest edge before base64 (Anthropic max).")

    parser.add_argument("--data_path", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="./results")
    parser.add_argument("--max_rounds", type=int, default=200)
    parser.add_argument("--retrieval_mode", choices=["agentic", "single_shot"],
                        default="agentic",
                        help="agentic: iterative loop (default). single_shot: "
                             "Single-Pass Retrieval -- free EHR schema orientation, "
                             "one retrieval turn (EHR+image tools), then forced "
                             "finish.")
    parser.add_argument("--evidence_budget_tokens", type=int, default=None,
                        help="single_shot only: cap on total tool-result tokens.")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="Sampling temperature (paper: 0 where accepted).")
    parser.add_argument("--runs_per_question", type=int, default=1)
    parser.add_argument("--max_concurrency", type=int, default=MAX_PARALLEL_QUERIES)
    parser.add_argument("--max_tool_result_chars", type=int, default=DEFAULT_MAX_TOOL_RESULT_CHARS)
    parser.add_argument("--enable_thinking", dest="enable_thinking", action="store_true")
    parser.add_argument("--disable_thinking", dest="enable_thinking", action="store_false")
    parser.set_defaults(enable_thinking=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    browser_backend_explicit = "--browser_backend" in os.sys.argv

    if args.api_max_attempts < 1:
        raise ValueError("--api_max_attempts must be at least 1")
    if args.api_retry_base_seconds <= 0 or args.api_retry_max_seconds <= 0:
        raise ValueError("API retry delays must be positive")
    if args.api_min_request_interval_seconds < 0:
        raise ValueError("--api_min_request_interval_seconds cannot be negative")

    global VERBOSE
    VERBOSE = args.verbose

    if args.enable_browser and args.disable_browser:
        raise ValueError("--enable_browser conflicts with --disable_browser")
    if not args.enable_browser:
        # No browser backend is ever constructed, so a Serper key is neither
        # needed nor meaningful.
        args.browser_backend = "disabled"
        print("Browser tools off (paper setting); --enable_browser adds them")
    else:
        if (
            not browser_backend_explicit
            and args.browser_backend == "local"
            and os.getenv("SERPER_API_KEY")
        ):
            args.browser_backend = "serper"
            print("SERPER_API_KEY detected; using Serper browser backend")
        if args.browser_backend == "serper" and not os.getenv("SERPER_API_KEY"):
            raise ValueError("SERPER_API_KEY is required when using --browser_backend serper")
    if args.max_concurrency < 1:
        raise ValueError("--max_concurrency must be at least 1")
    if args.runs_per_question < 1:
        raise ValueError("--runs_per_question must be at least 1")
    if args.max_tool_result_chars < 1:
        raise ValueError("--max_tool_result_chars must be at least 1")

    concurrency = min(args.max_concurrency, MAX_PARALLEL_QUERIES)
    os.makedirs(args.output_dir, exist_ok=True)

    selected_backend = "bedrock" if args.use_bedrock else args.backend
    if args.use_bedrock and args.backend != "bedrock":
        raise ValueError(f"--use_bedrock conflicts with --backend {args.backend}")

    if selected_backend == "bedrock":
        from bedrock_backend import BedrockAsyncGenerator

        bedrock_api_key = configure_bedrock_auth(args.bedrock_api_key)
        resolved_model_id = resolve_bedrock_model_id(
            args.bedrock_model_id or args.model_name_or_path
        )
        generator = BedrockAsyncGenerator(
            model_id=resolved_model_id,
            region_name=args.bedrock_region,
            max_tokens_default=DEFAULT_MM_MAX_TOKENS,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        if bedrock_api_key:
            print(f"Using Bedrock bearer token auth: {mask_secret(bedrock_api_key)}")
        else:
            print("Using default AWS credential chain for Bedrock auth")
        print(
            "Using AWS Bedrock: "
            f"{generator.model_id} @ {args.bedrock_region} | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    elif selected_backend == "vertex":
        from vertex_backend import VertexAsyncGenerator

        # An untouched --model_name_or_path still holds the Bedrock default, so
        # treat it as "unset" here and let the backend auto-pick.
        vertex_model = args.vertex_model_id or args.model_name_or_path
        if vertex_model == DEFAULT_BEDROCK_MODEL_ID:
            vertex_model = None

        generator = VertexAsyncGenerator(
            model_id=vertex_model,
            project_id=args.vertex_project,
            region=args.vertex_region,
            max_tokens_default=DEFAULT_MM_MAX_TOKENS,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
        print(
            "Using Google Vertex AI: "
            f"{generator.model_id} @ {generator.region} "
            f"(project={generator.project_id}) | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    elif selected_backend == "vllm":
        from vllm_backend import VLLMOpenAIAsyncGenerator

        # If user didn't override --model_name_or_path, fall back to auto-resolve
        # from the vLLM /v1/models endpoint instead of passing a Bedrock model ID.
        vllm_model = args.model_name_or_path
        if vllm_model == DEFAULT_BEDROCK_MODEL_ID:
            vllm_model = None
        generator = VLLMOpenAIAsyncGenerator(
            model_name=vllm_model,
            base_url=args.api_base_url,
            api_key=args.api_key,
            max_tokens_default=DEFAULT_MM_MAX_TOKENS,
            max_workers=concurrency,
            enable_thinking=args.enable_thinking,
            reasoning_effort=args.reasoning_effort,
            request_max_attempts=args.api_max_attempts,
            retry_base_delay=args.api_retry_base_seconds,
            retry_max_delay=args.api_retry_max_seconds,
            min_request_interval=args.api_min_request_interval_seconds,
        )
        print(
            "Using vLLM OpenAI-compatible API: "
            f"{generator.base_url} | model={generator.model_name or 'auto'} | "
            f"api_key={mask_optional_secret(args.api_key)} | "
            f"thinking={'auto' if args.enable_thinking is None else args.enable_thinking}"
        )
    else:
        raise NotImplementedError(f"Unsupported backend: {selected_backend}")

    browser_pool = BrowserPool(
        args.search_url,
        browser_backend=args.browser_backend,
        enabled=args.enable_browser,
    )

    # --- EHR pools, keyed on source_benchmark. ---------------------------------
    # Default pool handles samples without a registered override.
    ehr_pools: Dict[str, EHRToolPool] = {}
    ehr_pool_default: Optional[EHRToolPool] = None
    if args.enable_ehr:
        ehr_pool_default = EHRToolPool(mcp_url=args.ehr_mcp_url)
        print(f"EHR tools enabled (default): {args.ehr_mcp_url}")
        if args.ehr_mcp_url_ehrxqa:
            ehr_pools["ehrxqa"] = EHRToolPool(mcp_url=args.ehr_mcp_url_ehrxqa)
            print(f"EHR tools enabled (ehrxqa): {args.ehr_mcp_url_ehrxqa}")
        if args.ehr_mcp_url_medmod:
            ehr_pools["medmod"] = EHRToolPool(mcp_url=args.ehr_mcp_url_medmod)
            print(f"EHR tools enabled (medmod): {args.ehr_mcp_url_medmod}")

    image_pool = None
    if args.enable_image:
        image_pool = ImageToolPool(mcp_url=args.image_mcp_url)
        print(f"Image tools enabled: {args.image_mcp_url}")

    raw_bench_root = args.bench_root or os.environ.get("BENCH_ROOT")
    if raw_bench_root:
        # Accept colon- or comma-separated roots. On Windows, use commas when
        # passing multiple roots so drive prefixes like D:\ are preserved.
        parts = split_bench_roots(raw_bench_root)
        bench_root = [str(Path(p).resolve()) for p in parts]
        print(f"Benchmark roots: {bench_root}")
    else:
        bench_root = None
        print("No --bench_root / $BENCH_ROOT set — image/report paths must be absolute.")

    data = load_query_data(args.data_path)
    validate_agentic_manifest(data)
    total_task_runs = len(data) * args.runs_per_question
    print(f"Loaded {len(data)} queries from {args.data_path}")
    print(f"Running {args.runs_per_question} run(s) per query ({total_task_runs} total runs)")
    print(f"Running with max concurrency: {concurrency}")
    print(f"Tool result char limit: {args.max_tool_result_chars}")
    print(f"Image max edge: {args.image_max_edge}")

    # Provenance sidecar, matching run_text.py. Without it a finished run
    # cannot say which tool families, cutoff policy or thinking settings
    # produced it, which is exactly what an ablation needs to record.
    effective_request = {
        "tool_choice": "auto",
        "stream": False,
        "api_worker_threads": getattr(
            getattr(generator, "executor", None), "_max_workers", None
        ),
    }
    model_for_request = getattr(
        generator, "model_name",
        getattr(generator, "model_id", args.model_name_or_path),
    )
    for attr, key in (
        ("_build_extra_body", "extra_body"),
        ("_reasoning_kwargs", "reasoning_kwargs"),
    ):
        if hasattr(generator, attr):
            fn = getattr(generator, attr)
            effective_request[key] = (
                fn() if attr == "_build_extra_body" else fn(model_for_request)
            )
    if hasattr(generator, "_sampling_kwargs"):
        effective_request["sampling_kwargs"] = generator._sampling_kwargs(
            model_for_request, args.temperature
        )
    if hasattr(generator, "_max_token_kwargs"):
        effective_request["max_token_kwargs"] = generator._max_token_kwargs(
            model_for_request, DEFAULT_MM_MAX_TOKENS
        )

    run_config_file = os.path.join(args.output_dir, "run_config.json")
    with open(run_config_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "backend": selected_backend,
                "model": args.model_name_or_path,
                "data_path": os.path.abspath(args.data_path),
                "data_sha256": _sha256_file(args.data_path),
                "output_dir": os.path.abspath(args.output_dir),
                "enable_thinking": args.enable_thinking,
                "thinking_budget_tokens": args.thinking_budget_tokens,
                "reasoning_effort": args.reasoning_effort,
                "api_max_attempts": args.api_max_attempts,
                "api_retry_base_seconds": args.api_retry_base_seconds,
                "api_retry_max_seconds": args.api_retry_max_seconds,
                "api_min_request_interval_seconds":
                    args.api_min_request_interval_seconds,
                "temperature_cli": args.temperature,
                "max_tokens": DEFAULT_MM_MAX_TOKENS,
                "max_rounds": args.max_rounds,
                "max_concurrency": concurrency,
                "runs_per_question": args.runs_per_question,
                "max_tool_result_chars": args.max_tool_result_chars,
                "retrieval_mode": args.retrieval_mode,
                # Tool-space ablation: these three decide the tool list and the
                # system prompt, so they are the fields that identify an arm.
                "browser_backend": args.browser_backend,
                "browser_enabled": bool(args.enable_browser),
                "image_enabled": bool(args.enable_image),
                "tool_count": len(
                    get_tools_mm(bool(args.enable_browser), bool(args.enable_image))
                ),
                "ehr_enabled": bool(args.enable_ehr),
                "ehr_mcp_url": args.ehr_mcp_url if args.enable_ehr else None,
                "ehr_mcp_url_ehrxqa": args.ehr_mcp_url_ehrxqa,
                "ehr_mcp_url_medmod": args.ehr_mcp_url_medmod,
                "image_mcp_url": args.image_mcp_url if args.enable_image else None,
                "image_max_edge": args.image_max_edge,
                "bench_root": bench_root,
                "leakage_policy_version": STRICT_POLICY_VERSION,
                "effective_model_request": effective_request,
            },
            f,
            indent=2,
        )
    print(f"Run config written to {run_config_file}")

    if hasattr(generator, "_init_tokenizer"):
        await generator._init_tokenizer()

    output_file = os.path.join(args.output_dir, "results.jsonl")

    completed_qids = set()
    existing_lines: List[str] = []
    retryable = 0
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    existing_lines.append(line)
                    continue
                # A row is re-run only when the request never landed
                # (status "error": 429s, dead MCP sessions). A row where the
                # model was reached and did not produce a finish call is a
                # datapoint -- keep it, do not retry it into a nicer number.
                # (Keying on `completed` instead would re-run and drop every
                # recorded incomplete row on resume.)
                if result.get("status") == "error":
                    retryable += 1
                    continue
                completed_qids.add(result["qid"])
                existing_lines.append(line)
    if retryable:
        print(f"Resuming: {retryable} errored row(s) will be re-run; "
              f"{len(completed_qids)} kept (incomplete rows kept as-is)")
    elif completed_qids:
        print(f"Resuming: {len(completed_qids)} already in results, skipping them")

    with open(output_file, "w", encoding="utf-8") as out_f:
        # existing_lines already excludes errored rows, so write them all back:
        # filtering again on `completed` here is what deleted incomplete rows.
        for line in existing_lines:
            out_f.write(line + "\n")
        out_f.flush()

        semaphore = asyncio.Semaphore(concurrency)
        write_lock = asyncio.Lock()
        task_index = 1
        skipped = 0

        for run_index in range(1, args.runs_per_question + 1):
            print(
                f"\n{'#' * 80}\n"
                f"Starting MM run batch {run_index}/{args.runs_per_question} "
                f"for {len(data)} query(ies)\n"
                f"{'#' * 80}"
            )
            batch_tasks = []
            for question_index, item in enumerate(data, start=1):
                qid = resolve_qid(item)
                if qid in completed_qids:
                    skipped += 1
                    task_index += 1
                    continue
                # Route to the EHR MCP matching this sample's source benchmark.
                src = (item.get("source_benchmark") or "").lower().strip()
                per_sample_ehr_pool = ehr_pools.get(src, ehr_pool_default)
                batch_tasks.append(
                    asyncio.create_task(
                        process_query_item_mm(
                            item=item,
                            task_index=task_index,
                            total_task_runs=total_task_runs,
                            question_index=question_index,
                            total_questions=len(data),
                            run_index=run_index,
                            runs_per_question=args.runs_per_question,
                            generator=generator,
                            browser_pool=browser_pool,
                            ehr_pool=per_sample_ehr_pool,
                            image_pool=image_pool,
                            bench_root=bench_root,
                            image_max_edge=args.image_max_edge,
                            max_rounds=args.max_rounds,
                            temperature=args.temperature,
                            max_tool_result_chars=args.max_tool_result_chars,
                            semaphore=semaphore,
                            out_f=out_f,
                            output_file=output_file,
                            write_lock=write_lock,
                            model_name=args.model_name_or_path,
                            retrieval_mode=args.retrieval_mode,
                            evidence_budget_tokens=args.evidence_budget_tokens,
                        )
                    )
                )
                task_index += 1
            if batch_tasks:
                print(
                    f"Queued {len(batch_tasks)} task(s) for run batch {run_index}"
                    + (f"; skipped {skipped} already-completed so far" if skipped else "")
                )
                await asyncio.gather(*batch_tasks)

    print(f"\nAll multimodal queries processed. Results in {output_file}")

    # One billable request per round per query, so only the run-level total is
    # meaningful. Written next to the results so a run's cost stays traceable.
    if hasattr(generator, "token_usage"):
        print(generator.usage_summary())
        usage_file = os.path.join(args.output_dir, "usage.json")
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump(
                {"model": getattr(generator, "model_id", args.model_name_or_path),
                 "backend": selected_backend,
                 **generator.token_usage},
                f,
                indent=2,
            )
        print(f"Token usage written to {usage_file}")

    if ehr_pool_default:
        await ehr_pool_default.close()
    for pool in ehr_pools.values():
        await pool.close()
    if image_pool:
        await image_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
