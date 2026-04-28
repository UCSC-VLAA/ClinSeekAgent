#!/usr/bin/env python3
"""Single-turn Q&A evaluation on EHR-Bench.

Unlike ``deploy_agent.py`` which runs a multi-round ReAct agent over MCP tools,
this script just feeds each benchmark item's ``question`` field into the vLLM
chat completion endpoint and records the model's answer. Output is written in
the same ``results.jsonl`` format consumed by
``helper/evaluate_results.py`` (by synthesising an ``ehr.finish`` tool call
from the model's text answer), so scoring works with the existing tooling.
"""

from __future__ import annotations

import argparse
import asyncio
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


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FINAL_ANSWER_RE = re.compile(
    r"final\s*answer\s*[:\-]?\s*(\[[\s\S]*?\])",
    re.IGNORECASE,
)

ANSWER_FORMAT_INSTRUCTION = (
    "\n\nYour response MUST end with a single line in the exact format:\n"
    "Final Answer: ['answer1', 'answer2', ...]\n"
    "where the list contains one or more items selected verbatim from the "
    "<candidate_answers> list above. Do not invent answers outside that list. "
)


def build_prompt(item: dict[str, Any]) -> str:
    question = item.get("question") or ""
    return f"{question}{ANSWER_FORMAT_INSTRUCTION}"


def load_benchmark(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
            elif item is not None:
                out.append(str(item))
        return out
    if value is None:
        return []
    return [str(value)]


def parse_answer(
    raw_text: str,
    candidates: list[str] | None,
) -> tuple[list[str], str]:
    """Turn the model's raw text answer into a list of predicted strings.

    Returns ``(predictions, parse_mode)`` where ``parse_mode`` is a short tag
    useful for debugging the parser's choice.
    """
    cleaned = strip_think_blocks(raw_text)
    if not cleaned:
        return [], "empty"

    # Preferred: the model follows the instruction and ends with
    # "Final Answer: ['xxx', 'yyy']". Scan all occurrences and take the last
    # parsable list so trailing text / multiple mentions in reasoning don't
    # derail us.
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

    # Strip a single pair of surrounding quotes, trailing periods, etc.
    bare = cleaned.strip()
    for _ in range(2):
        if len(bare) >= 2 and bare[0] == bare[-1] and bare[0] in ('"', "'"):
            bare = bare[1:-1].strip()

    # Try JSON list first — many decision_making items ask for a JSON-style
    # array answer.
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

    # If candidates are present, prefer exact case-insensitive matches found in
    # the cleaned text. This makes the parse robust to decorative wrappers like
    # "Answer: yes." or bullet lists.
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

    # Fallback: single-line answer.
    first_line = bare.splitlines()[0].strip() if bare else ""
    first_line = first_line.rstrip(".").strip()
    if first_line:
        return [first_line], "single_line"

    return [bare] if bare else [], "raw"


def build_result_record(
    item: dict[str, Any],
    question: str,
    raw_answer: str,
    reasoning: str | None,
    predictions: list[str],
    parse_mode: str,
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
        "subject_id": item.get("subject_id"),
        "hadm_id": item.get("hadm_id"),
        "task": item.get("task"),
        "task_type": item.get("task_type"),
        "ground_truth": item.get("label"),
        "prediction_time": item.get("prediction_time"),
        "latest_event_time": item.get("latest_event_time"),
        "messages": [
            {"role": "user", "content": question},
            assistant_message,
        ],
        "completed": completed,
        "stop_reason": "finish_tool_call" if completed else "error",
        "qa_mode": True,
        "parse_mode": parse_mode,
        "predictions": predictions,
        "elapsed_seconds": elapsed,
        "usage": usage or {},
        "error": error,
    }


def call_vllm_once(
    client: OpenAI,
    model_name: str,
    question: str,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool | None,
) -> dict[str, Any]:
    extra_body: dict[str, Any] | None = None
    if enable_thinking is not None:
        extra_body = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}

    response = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": question}],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=extra_body,
        stream=False,
    )
    choice = response.choices[0]
    message = choice.message
    content = message.content or ""
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
        "content": content,
        "reasoning": reasoning,
        "finish_reason": getattr(choice, "finish_reason", None),
        "usage": usage_dict,
    }


async def run_one(
    executor: ThreadPoolExecutor,
    semaphore: asyncio.Semaphore,
    client: OpenAI,
    model_name: str,
    item: dict[str, Any],
    temperature: float,
    max_tokens: int,
    enable_thinking: bool | None,
    max_retries: int,
    retry_sleep: float,
) -> dict[str, Any]:
    question = build_prompt(item)
    candidates = item.get("candidates")
    if not isinstance(candidates, list):
        candidates = None

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
                        question=question,
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
            except Exception as exc:  # noqa: BLE001 — surface all vLLM errors
                last_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[qa] qid={item.get('qid')} attempt {attempt}/{max_retries} "
                    f"failed: {last_error}",
                    file=sys.stderr,
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_sleep)

        elapsed = time.time() - start
        predictions, parse_mode = parse_answer(raw_answer, candidates)
        return build_result_record(
            item=item,
            question=question,
            raw_answer=raw_answer,
            reasoning=reasoning,
            predictions=predictions,
            parse_mode=parse_mode,
            usage=usage,
            elapsed=elapsed,
            error=last_error,
        )


async def run_all(
    items: list[dict[str, Any]],
    *,
    client: OpenAI,
    model_name: str,
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
            gt = record.get("ground_truth")
            status = "OK" if record.get("completed") else f"ERR({record.get('error')})"
            print(
                f"[qa] {done_count}/{total} qid={qid} status={status} "
                f"preds={preds!r} gt={gt!r}"
            )
    executor.shutdown(wait=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-turn Q&A evaluation on EHR-Bench using vLLM."
    )
    parser.add_argument(
        "--data_path",
        default="../data/EHR-Bench/ehr_bench_sampled_20_per_task.json",
        help="Path to EHR-Bench JSON file (list of items with a 'question' field).",
    )
    parser.add_argument(
        "--output_path",
        default="./results/ehrbench_qa/results.jsonl",
        help="Destination results.jsonl path.",
    )
    parser.add_argument("--vllm_base_url", default=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--vllm_api_key", default=os.environ.get("VLLM_API_KEY", "EMPTY"))
    parser.add_argument(
        "--model_name",
        default=os.environ.get("VLLM_MODEL_NAME", "auto"),
        help="Served model id; 'auto' queries /v1/models.",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max_tokens", type=int, default=32768)
    parser.add_argument("--max_concurrency", type=int, default=2)
    parser.add_argument("--max_retries", type=int, default=2)
    parser.add_argument("--retry_sleep", type=float, default=2.0)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only run the first N items (for smoke tests).",
    )
    parser.add_argument(
        "--start_index",
        type=int,
        default=0,
        help="Skip the first K items before the limit window.",
    )
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument("--enable_thinking", dest="enable_thinking", action="store_true")
    thinking.add_argument("--disable_thinking", dest="enable_thinking", action="store_false")
    parser.set_defaults(enable_thinking=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    data_path = Path(args.data_path).expanduser().resolve()
    output_path = Path(args.output_path).expanduser().resolve()

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

    print(
        f"[qa] data_path={data_path}\n"
        f"[qa] output_path={output_path}\n"
        f"[qa] model={model_name}\n"
        f"[qa] items={len(items)} concurrency={args.max_concurrency} "
        f"temperature={args.temperature} max_tokens={args.max_tokens} "
        f"enable_thinking={args.enable_thinking}"
    )

    asyncio.run(
        run_all(
            items=items,
            client=client,
            model_name=model_name,
            output_path=output_path,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            enable_thinking=args.enable_thinking,
            max_concurrency=args.max_concurrency,
            max_retries=args.max_retries,
            retry_sleep=args.retry_sleep,
        )
    )

    print(f"[qa] Done. Results written to {output_path}")


if __name__ == "__main__":
    main()
