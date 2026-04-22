"""Multimodal deploy agent — Bedrock Claude with EHR + Browser + Image tools.

This file is the multimodal twin of `deploy_agent.py`. The text-only pipeline
is never touched. Key differences:

1. User message is built as a list of Anthropic content blocks: the sample's
   pre-rendered `question` text, each `image_paths[*]` attached as a base64
   image block (downscaled to `--image_max_edge`), and each `report_paths[*]`
   inlined as an extra text block. The existing Bedrock generator already
   supports list-typed `content` for user messages (bedrock_generator.py:258).

2. Tool schemas come from `data_utils_mm.COMBINED_TOOL_CONTENT_MM` (EHR +
   browser + 6 Meissa image tools).

3. `image_pool.ImageToolPool` routes `image.*` tool calls to the image MCP
   server (default http://127.0.0.1:5203/mcp).

Shared helpers (normalize_tool_calls, truncate_tool_result, load_query_data,
BrowserPool, BedrockAsyncGenerator, ...) are imported from `deploy_agent.py`
unchanged.
"""
import argparse
import asyncio
import base64
import io
import json
import os
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import dotenv

# --- Re-use unmodified helpers from the text-only deploy_agent. ---------------
from deploy_agent import (
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
)
from ehr_pool import EHRToolPool
from image_pool import ImageToolPool
from data_utils_mm import (
    COMBINED_TOOL_CONTENT_MM,
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
_MAX_IMAGES_PER_SAMPLE = 4


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
    if bench_root:
        roots = [bench_root] if isinstance(bench_root, (str, Path)) else list(bench_root)
        for root in roots:
            if not root:
                continue
            rooted = Path(root) / path
            if rooted.exists():
                return rooted
    if candidate.exists():
        return candidate.resolve()
    return None


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
        f"- patient_ehr_available: {'yes' if (has_subject_id and not is_cohort_scope) else 'no'}"
        + ("  (this row is cohort-scope — ehr.load_ehr is not applicable)"
           if is_cohort_scope else ""),
    ]
    if resolved_images:
        availability_lines.append("- image_paths (use these VERBATIM in image.* calls):")
        for p in resolved_images:
            availability_lines.append(f"    {p}")
    availability_lines.extend([
        "",
        "Only call tools whose preconditions are satisfied:",
        "- call ehr.load_ehr ONLY if patient_ehr_available=yes",
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
    image block is added. If it has no `subject_id` (or is cohort-scope), the
    preamble tells the model the patient EHR is not available and suppresses
    ehr.load_ehr calls.
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

    system_prompt = DEVELOPER_CONTENT_CLAUDE_MM
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content_blocks},
    ]
    tools = json.loads(COMBINED_TOOL_CONTENT_MM)

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
            raw_content = message.get("raw_content")
            reasoning_content = (message.get("reasoning_content") or "").strip()
            tool_calls = message.get("tool_calls") or []
            parse_error = (message.get("parse_error") or "").strip()
            is_openseeker_repo_like = raw_content is not None

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
                "content": raw_content if raw_content is not None else content,
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
                # Salvage: some OSS models (Qwen, GLM) stop calling tools once
                # they are ready to answer — they emit the final answer as
                # plain text and treat that as implicit end-of-turn. Synthesize
                # a matching `ehr.finish` call so downstream scoring can pick
                # the content up as the prediction instead of marking the row
                # incomplete.
                answer_text = (content or "").strip()
                if answer_text:
                    # Heuristic: split on newlines / bullets to mimic the
                    # list-valued `response` the finish tool expects.
                    items: List[str] = []
                    for raw in answer_text.splitlines():
                        raw = raw.strip().lstrip("-*•").strip()
                        if raw:
                            items.append(raw)
                    if not items:
                        items = [answer_text]
                    synth_call = {
                        "id": f"synthetic_finish_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": "ehr.finish",
                            "arguments": json.dumps({"response": items}),
                        },
                    }
                    messages[-1]["tool_calls"] = [synth_call]
                    messages.append({
                        "role": "tool",
                        "content": "Finish (synthesized from plain-text answer)",
                        "tool_call_id": synth_call["id"],
                    })
                    print(
                        f"[qid={qid}] ✅ Round {round_num}: synthesized ehr.finish "
                        f"from {len(items)}-line plain-text answer",
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

                    tool_message = {"role": "tool", "content": result}
                    if is_openseeker_repo_like:
                        tool_message["name"] = function_name
                        tool_message["tool_call_id"] = str(uuid.uuid4())
                    else:
                        tool_message["tool_call_id"] = tool_id
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
                    err_message = {"role": "tool", "content": error_msg}
                    if is_openseeker_repo_like:
                        err_message["name"] = function_name
                        err_message["tool_call_id"] = str(uuid.uuid4())
                    else:
                        err_message["tool_call_id"] = tool_id
                    messages.append(err_message)

            if finish_tool_called:
                print(f"[qid={qid}] ✅ Round {round_num}: ehr.finish called - DONE", flush=True)
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
):
    try:
        question_text, user_blocks, mm_summary = resolve_question_mm(
            item, bench_root=bench_root, image_max_edge=image_max_edge
        )
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
        return {
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
    dotenv.load_dotenv("../.env")

    parser = argparse.ArgumentParser(description="Multimodal OpenResearcher + EHR pipeline")
    parser.add_argument("--backend", type=str, choices=["bedrock"], default="bedrock",
                        help="LLM backend. vLLM is not supported for multimodal runs in this file.")
    parser.add_argument("--model_name_or_path", type=str, default=DEFAULT_BEDROCK_MODEL_ID)
    parser.add_argument("--use_bedrock", action="store_true", default=True)
    parser.add_argument("--bedrock_model_id", type=str, default=None)
    parser.add_argument("--bedrock_region", type=str, default=DEFAULT_BEDROCK_REGION)
    parser.add_argument("--bedrock_api_key", type=str, default=None)

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
    parser.add_argument("--enable_image", action="store_true",
                        help="Enable medical-image MCP tools.")
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
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--runs_per_question", type=int, default=1)
    parser.add_argument("--max_concurrency", type=int, default=MAX_PARALLEL_QUERIES)
    parser.add_argument("--max_tool_result_chars", type=int, default=DEFAULT_MAX_TOOL_RESULT_CHARS)
    parser.add_argument("--enable_thinking", dest="enable_thinking", action="store_true")
    parser.add_argument("--disable_thinking", dest="enable_thinking", action="store_false")
    parser.set_defaults(enable_thinking=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    browser_backend_explicit = "--browser_backend" in os.sys.argv

    global VERBOSE
    VERBOSE = args.verbose

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

    import bedrock_generator as _bgen
    from bedrock_generator import BedrockAsyncGenerator

    # Runtime shim: bedrock_generator.py has a latent NameError at line 555
    # (`use_reasoning_content` is a parameter of chat_completion(), not a
    # local of _chat_completion_anthropic). The text-only pipeline never hits
    # the path where the tool_use branch runs under our invocation pattern, but
    # the multimodal run does. Inject the name into the module globals so the
    # reference always resolves — no edit to bedrock_generator.py.
    _bgen.__dict__.setdefault("use_reasoning_content", True)

    bedrock_api_key = configure_bedrock_auth(args.bedrock_api_key)
    resolved_model_id = resolve_bedrock_model_id(
        args.bedrock_model_id or args.model_name_or_path
    )
    generator = BedrockAsyncGenerator(
        model_id=resolved_model_id,
        region_name=args.bedrock_region,
        max_tokens_default=8192,
        enable_thinking=args.enable_thinking,
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

    browser_pool = BrowserPool(args.search_url, browser_backend=args.browser_backend)

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
        # Accept colon- or comma-separated list of roots; pass the list through
        # to resolve_asset_path which already handles multi-root lookup.
        parts = [p for p in raw_bench_root.replace(",", ":").split(":") if p]
        bench_root = [str(Path(p).resolve()) for p in parts]
        print(f"Benchmark roots: {bench_root}")
    else:
        bench_root = None
        print("No --bench_root / $BENCH_ROOT set — image/report paths must be absolute.")

    data = load_query_data(args.data_path)
    total_task_runs = len(data) * args.runs_per_question
    print(f"Loaded {len(data)} queries from {args.data_path}")
    print(f"Running {args.runs_per_question} run(s) per query ({total_task_runs} total runs)")
    print(f"Running with max concurrency: {concurrency}")
    print(f"Tool result char limit: {args.max_tool_result_chars}")
    print(f"Image max edge: {args.image_max_edge}")

    if hasattr(generator, "_init_tokenizer"):
        await generator._init_tokenizer()

    output_file = os.path.join(args.output_dir, "results.jsonl")

    completed_qids = set()
    existing_lines: List[str] = []
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                    if result.get("completed"):
                        completed_qids.add(result["qid"])
                    existing_lines.append(line)
                except json.JSONDecodeError:
                    existing_lines.append(line)
    if completed_qids:
        print(f"Resuming: {len(completed_qids)} already completed, skipping them")

    with open(output_file, "w", encoding="utf-8") as out_f:
        for line in existing_lines:
            try:
                r = json.loads(line)
                if r.get("completed"):
                    out_f.write(line + "\n")
            except json.JSONDecodeError:
                pass
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

    print(f"\n✅ All multimodal queries processed. Results in {output_file}")

    if ehr_pool_default:
        await ehr_pool_default.close()
    for pool in ehr_pools.values():
        await pool.close()
    if image_pool:
        await image_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
