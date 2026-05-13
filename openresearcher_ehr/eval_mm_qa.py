#!/usr/bin/env python3
"""Single-turn QA evaluation on the Multimodal EHR Benchmark.

Unlike the agentic pipeline (deploy_agent_mm.py) which runs multi-round
tool-calling over MCP servers, this script feeds each sample's pre-rendered
``input_text`` (EHR context + question) plus any linked CXR images directly
into vLLM's OpenAI-compatible chat completion endpoint — one turn, no tools.

Output is written in the same ``results.jsonl`` format consumed by
``scorer_mm.py`` (by synthesising an ``ehr.finish`` tool call from the
model's text answer), so scoring works with the existing tooling.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FINAL_ANSWER_RE = re.compile(
    r"final\s*answer\s*[:\-]?\s*(\[[\s\S]*?\])",
    re.IGNORECASE,
)
FINAL_ANSWER_PLAIN_RE = re.compile(
    r"final\s*answer\s*[:\-]\s*([^\[\n][^\n]*)",
    re.IGNORECASE,
)
GEMMA_THOUGHT_PREFIX_RE = re.compile(
    r"^\s*thought\s*\n(.*?)(?=(?:^|\n)\s*final\s*answer\s*[:\-]|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)
META_LABEL_TOKENS = {"thought", "answer", "reasoning", "analysis", "response"}

ANSWER_FORMAT_INSTRUCTION = (
    "\n\nYour response MUST end with a single line in the exact format:\n"
    "Final Answer: ['answer1', 'answer2', ...]\n"
    "where the list contains one or more items that are your answers. "
    "Do not add explanations after the Final Answer line."
)

IMAGE_PATH_REMAP: dict[str, str] = {
    "MedModOriginalLinked_v1": "MedModAgentBench_v3",
    "EHRXQAOriginalLinked_v1": "EHRXQAAgentBench_v3",
}

_MAX_IMAGES_PER_SAMPLE = 4


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def remap_image_path(path: str) -> str:
    for old_prefix, new_prefix in IMAGE_PATH_REMAP.items():
        if path.startswith(old_prefix + "/"):
            return new_prefix + path[len(old_prefix):]
    return path


def resolve_image_path(path: str, bench_roots: list[str]) -> Path | None:
    remapped = remap_image_path(path)
    for root in bench_roots:
        candidate = Path(root) / remapped
        if candidate.is_file():
            return candidate
    return None


def load_image_as_openai_block(
    path: Path, max_edge: int = 1568
) -> dict[str, Any] | None:
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        if max(w, h) > max_edge:
            scale = max_edge / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        }
    except Exception as exc:
        print(f"[mm-qa] WARNING: failed to load image {path}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_prompt(
    item: dict[str, Any],
    bench_roots: list[str],
    image_max_edge: int,
) -> tuple[str | list[dict], list[str]]:
    text = (item.get("input_text") or item.get("question") or "") + ANSWER_FORMAT_INSTRUCTION

    image_blocks: list[dict] = []
    resolved_paths: list[str] = []
    for rel_path in (item.get("image_paths") or [])[:_MAX_IMAGES_PER_SAMPLE]:
        abs_path = resolve_image_path(rel_path, bench_roots)
        if abs_path is None:
            continue
        block = load_image_as_openai_block(abs_path, image_max_edge)
        if block is not None:
            image_blocks.append(block)
            resolved_paths.append(str(abs_path))

    if not image_blocks:
        return text, resolved_paths

    content: list[dict] = [{"type": "text", "text": text}]
    content.extend(image_blocks)
    return content, resolved_paths


# ---------------------------------------------------------------------------
# Benchmark loading
# ---------------------------------------------------------------------------

def load_benchmark(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        if path.suffix == ".jsonl":
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
        else:
            items = json.load(f)
    return items


# ---------------------------------------------------------------------------
# Model name resolution
# ---------------------------------------------------------------------------

def resolve_model_name(base_url: str, requested: str) -> str:
    if requested and requested != "auto":
        return requested
    normalized = base_url.rstrip("/")
    if not normalized.endswith("/v1"):
        normalized = f"{normalized}/v1"
    with urllib.request.urlopen(f"{normalized}/models", timeout=10) as response:
        payload = json.load(response)
    models = payload.get("data") or []
    if not models:
        raise RuntimeError(f"No served models reported by vLLM at {base_url}")
    model_id = models[0].get("id")
    if not model_id:
        raise RuntimeError(f"Unable to resolve served model ID from vLLM at {base_url}")
    return model_id


# ---------------------------------------------------------------------------
# Answer parsing (reused from eval_ehrbench_qa.py)
# ---------------------------------------------------------------------------

def strip_think_blocks(text: str) -> str:
    return THINK_BLOCK_RE.sub("", text or "").strip()


def _coerce_to_list(value: Any) -> list[str]:
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("value") or item.get("text")
                if name is not None:
                    out.append(str(name).strip())
            elif item is not None:
                out.append(str(item))
        return out
    if value is None:
        return []
    return [str(value)]


def parse_answer(
    raw_text: str,
    candidates: list[str] | None = None,
) -> tuple[list[str], str]:
    cleaned = strip_think_blocks(raw_text)
    if not cleaned:
        return [], "empty"

    final_matches = list(FINAL_ANSWER_RE.finditer(cleaned))
    for match in reversed(final_matches):
        snippet = match.group(1).strip()
        parsed = None
        try:
            parsed = json.loads(snippet)
        except Exception:
            try:
                import ast
                parsed = ast.literal_eval(snippet)
            except Exception:
                parsed = None
        if parsed is not None:
            preds = _coerce_to_list(parsed)
            if preds:
                return preds, "final_answer_list"

    plain_matches = list(FINAL_ANSWER_PLAIN_RE.finditer(cleaned))
    for match in reversed(plain_matches):
        raw = match.group(1).strip().rstrip(".").strip()
        for _ in range(2):
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
                raw = raw[1:-1].strip()
        if not raw:
            continue
        if raw.lower() in META_LABEL_TOKENS:
            continue
        return [raw], "final_answer_plain"

    bare = cleaned.strip()
    for _ in range(2):
        if len(bare) >= 2 and bare[0] == bare[-1] and bare[0] in ('"', "'"):
            bare = bare[1:-1].strip()

    for snippet in (bare, cleaned):
        s = snippet.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                preds = _coerce_to_list(parsed)
                if preds:
                    return preds, "json_list"
            except Exception:
                pass

    if candidates:
        cand_map = {c.strip().lower(): c for c in candidates if isinstance(c, str) and c.strip()}
        hits: list[tuple[int, str]] = []
        text_lower = cleaned.lower()
        for key, canon in cand_map.items():
            if not key:
                continue
            idx = text_lower.find(key)
            if idx != -1:
                hits.append((idx, canon))
        if hits:
            hits.sort(key=lambda x: x[0])
            seen: set[str] = set()
            preds = []
            for _, c in hits:
                k = c.lower()
                if k in seen:
                    continue
                seen.add(k)
                preds.append(c)
            return preds, "candidate_match"

    if bare:
        for line in bare.splitlines():
            candidate = line.strip().rstrip(".").strip()
            if not candidate:
                continue
            if candidate.lower() in META_LABEL_TOKENS:
                continue
            return [candidate], "single_line"

    return [bare] if bare else [], "raw"


# ---------------------------------------------------------------------------
# Result record (scorer_mm.py compatible)
# ---------------------------------------------------------------------------

def build_result_record(
    item: dict[str, Any],
    prompt_text: str,
    raw_answer: str,
    reasoning: str | None,
    predictions: list[str],
    parse_mode: str,
    resolved_image_paths: list[str],
    usage: dict[str, Any] | None,
    elapsed: float,
    error: str | None = None,
) -> dict[str, Any]:
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": raw_answer,
    }
    if reasoning:
        assistant_message["reasoning_content"] = reasoning
    assistant_message["tool_calls"] = [
        {
            "id": "call_qa_finish",
            "type": "function",
            "function": {
                "name": "ehr.finish",
                "arguments": json.dumps({"response": predictions}, ensure_ascii=False),
            },
        }
    ]

    completed = error is None
    return {
        "qid": item.get("qid"),
        "run_index": 1,
        "runs_per_question": 1,
        "session_id": f"{item.get('qid', '')}__qa",
        "question": item.get("question", ""),
        "messages": [
            {"role": "user", "content": prompt_text if isinstance(prompt_text, str) else "(multimodal)"},
            assistant_message,
        ],
        "completed": completed,
        "status": "success" if completed else "error",
        "stop_reason": "finish_tool_call" if completed else "error",
        "image_paths": item.get("image_paths", []),
        "subject_id": item.get("subject_id"),
        "hadm_id": item.get("hadm_id"),
        "stay_id": item.get("stay_id"),
        "label": item.get("ground_truth", []),
        "prediction_time": item.get("prediction_time"),
        "task": item.get("task"),
        "source_benchmark": item.get("source_benchmark"),
        "source_split": item.get("source_split"),
        "answer_type": item.get("answer_type"),
        "modalities": item.get("modalities"),
        "multimodal_summary": {
            "num_images": len(resolved_image_paths),
            "resolved_image_paths": resolved_image_paths,
        },
        "_mm_has_image": len(resolved_image_paths) > 0,
        "_mm_has_ehr_subject": item.get("subject_id") is not None,
        "qa_mode": True,
        "parse_mode": parse_mode,
        "predictions": predictions,
        "elapsed_seconds": elapsed,
        "usage": usage or {},
        "error": error,
    }


# ---------------------------------------------------------------------------
# vLLM call
# ---------------------------------------------------------------------------

def call_vllm_once(
    client: OpenAI,
    model_name: str,
    content: str | list[dict],
    temperature: float,
    max_tokens: int,
    enable_thinking: bool | None,
) -> dict[str, Any]:
    extra_body: dict[str, Any] | None = None
    if enable_thinking is not None:
        extra_body = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}

    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": content}],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
        stream=False,
    )
    choice = response.choices[0]
    message = choice.message
    text = message.content or ""
    reasoning = getattr(message, "reasoning_content", None)
    if not reasoning:
        reasoning = getattr(message, "reasoning", None)
    usage = getattr(response, "usage", None)
    usage_dict = {}
    if usage is not None:
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    return {
        "content": text,
        "reasoning": reasoning,
        "finish_reason": getattr(choice, "finish_reason", None),
        "usage": usage_dict,
    }


# ---------------------------------------------------------------------------
# Async execution
# ---------------------------------------------------------------------------

async def run_one(
    executor: ThreadPoolExecutor,
    semaphore: asyncio.Semaphore,
    client: OpenAI,
    model_name: str,
    item: dict[str, Any],
    bench_roots: list[str],
    image_max_edge: int,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool | None,
    max_retries: int,
    retry_sleep: float,
) -> dict[str, Any]:
    content, resolved_paths = build_prompt(item, bench_roots, image_max_edge)
    prompt_text = content if isinstance(content, str) else item.get("input_text", "")

    async with semaphore:
        loop = asyncio.get_event_loop()
        last_error: str | None = None
        raw_answer = ""
        reasoning = None
        usage: dict[str, Any] = {}
        start = time.time()
        for attempt in range(1, max_retries + 1):
            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda: call_vllm_once(
                        client=client,
                        model_name=model_name,
                        content=content,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        enable_thinking=enable_thinking,
                    ),
                )
                raw_answer = result["content"] or ""
                reasoning = result.get("reasoning")
                usage = result.get("usage") or {}
                last_error = None
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[mm-qa] qid={item.get('qid')} attempt {attempt}/{max_retries} "
                    f"failed: {last_error}",
                    file=sys.stderr,
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_sleep)

        elapsed = time.time() - start
        predictions, parse_mode = parse_answer(raw_answer)
        return build_result_record(
            item=item,
            prompt_text=prompt_text,
            raw_answer=raw_answer,
            reasoning=reasoning,
            predictions=predictions,
            parse_mode=parse_mode,
            resolved_image_paths=resolved_paths,
            usage=usage,
            elapsed=elapsed,
            error=last_error,
        )


async def run_all(
    items: list[dict[str, Any]],
    *,
    client: OpenAI,
    model_name: str,
    bench_roots: list[str],
    image_max_edge: int,
    output_path: Path,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool | None,
    max_concurrency: int,
    max_retries: int,
    retry_sleep: float,
) -> None:
    semaphore = asyncio.Semaphore(max_concurrency)
    executor = ThreadPoolExecutor(max_workers=max_concurrency)

    tasks = [
        asyncio.create_task(
            run_one(
                executor=executor,
                semaphore=semaphore,
                client=client,
                model_name=model_name,
                item=item,
                bench_roots=bench_roots,
                image_max_edge=image_max_edge,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                max_retries=max_retries,
                retry_sleep=retry_sleep,
            )
        )
        for item in items
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    done_count = 0
    total = len(items)
    with output_path.open("w", encoding="utf-8") as f:
        for coro in asyncio.as_completed(tasks):
            record = await coro
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            done_count += 1
            qid = record.get("qid")
            preds = record.get("predictions")
            gt = record.get("label")
            n_img = record.get("multimodal_summary", {}).get("num_images", 0)
            status = "OK" if record.get("completed") else f"ERR({record.get('error')})"
            print(
                f"[mm-qa] {done_count}/{total} qid={qid} imgs={n_img} "
                f"status={status} parse={record.get('parse_mode')} "
                f"preds={preds!r}"
            )
    executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-turn QA evaluation on the Multimodal EHR Benchmark using vLLM."
    )
    parser.add_argument(
        "--data_path",
        default="../data/EHR_multimodal_bench/EHR_multimodal_bench_tests/model_ready_combined_test_set.jsonl",
    )
    parser.add_argument(
        "--output_path",
        default="./results/mm_qa/results.jsonl",
    )
    parser.add_argument(
        "--bench_root",
        default="../data/EHR_multimodal_bench/extracted",
        help="Root directory (or colon-separated list) where extracted benchmark images live.",
    )
    parser.add_argument("--image_max_edge", type=int, default=1568)
    parser.add_argument("--vllm_base_url", default=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--vllm_api_key", default=os.environ.get("VLLM_API_KEY", "EMPTY"))
    parser.add_argument("--model_name", default=os.environ.get("VLLM_MODEL_NAME", "auto"))
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max_tokens", type=int, default=32768)
    parser.add_argument("--max_concurrency", type=int, default=8)
    parser.add_argument("--max_retries", type=int, default=2)
    parser.add_argument("--retry_sleep", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start_index", type=int, default=0)
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument("--enable_thinking", dest="enable_thinking", action="store_true")
    thinking.add_argument("--disable_thinking", dest="enable_thinking", action="store_false")
    parser.set_defaults(enable_thinking=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_path = Path(args.data_path).expanduser().resolve()
    output_path = Path(args.output_path).expanduser().resolve()
    bench_roots = [
        str(Path(r).expanduser().resolve())
        for r in args.bench_root.split(":")
        if r.strip()
    ]

    items = load_benchmark(data_path)
    end_index = len(items) if args.limit is None else min(len(items), args.start_index + args.limit)
    items = items[args.start_index:end_index]
    if not items:
        raise SystemExit("No benchmark items selected after applying --start_index / --limit.")

    model_name = resolve_model_name(args.vllm_base_url, args.model_name)

    base_url = args.vllm_base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    client = OpenAI(api_key=args.vllm_api_key, base_url=base_url)

    n_with_images = sum(1 for it in items if it.get("image_paths"))
    print(
        f"[mm-qa] data_path={data_path}\n"
        f"[mm-qa] output_path={output_path}\n"
        f"[mm-qa] bench_roots={bench_roots}\n"
        f"[mm-qa] model={model_name}\n"
        f"[mm-qa] items={len(items)} ({n_with_images} with images) "
        f"concurrency={args.max_concurrency}\n"
        f"[mm-qa] temperature={args.temperature} max_tokens={args.max_tokens} "
        f"enable_thinking={args.enable_thinking} image_max_edge={args.image_max_edge}"
    )

    asyncio.run(
        run_all(
            items=items,
            client=client,
            model_name=model_name,
            bench_roots=bench_roots,
            image_max_edge=args.image_max_edge,
            output_path=output_path,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            enable_thinking=args.enable_thinking,
            max_concurrency=args.max_concurrency,
            max_retries=args.max_retries,
            retry_sleep=args.retry_sleep,
        )
    )

    print(f"[mm-qa] Done. Results written to {output_path}")


if __name__ == "__main__":
    main()
