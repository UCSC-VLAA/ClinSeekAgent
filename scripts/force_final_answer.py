"""Forced final-answer round for rows that ended without a usable answer.

Some reasoning models (in the paper: GLM-5.2 and Kimi K2.6) spend the whole
`max_tokens` budget inside reasoning and never emit an answer:

  * agentic arm  -> last assistant turn is reasoning-only, no tool call;
                    row is `status=incomplete / no_final_answer`
  * curated arm  -> the reply is a truncated reasoning trace with no
                    <answer> block; the salvage heuristic then turns a random
                    reasoning sentence into the "prediction"

This script adds exactly ONE extra round to those rows: the conversation so far
(with the truncated reasoning replayed as assistant text) plus a user turn that
asks for the final answer, with tools restricted to `ehr.finish` and the tool
choice forced. No retrieval, no new evidence. Rows that already had an answer
are copied through unchanged. The source directory is never modified; a full
results.jsonl is written to --dst and every patched row carries a
`forced_final` block so the intervention is auditable.

The paper applies this one answer-only round to the GLM-5.2 and Kimi K2.6 runs
(both inference settings); every other model is scored as run.

    python scripts/force_final_answer.py \
        --src outputs/<run> --dst outputs/<run>_forcedfinal \
        --backend vllm --model <served-model-name> --api_base_url http://host:port/v1

`--backend` / model / endpoint flags mirror `clinseekagent/run_text.py`; use the
same model and endpoint the source run used. For Claude on Bedrock/Vertex the
forced tool choice is sent as `auto` while extended thinking is on (Claude does
not accept forced tool use with thinking); the round still offers only
`ehr.finish`.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clinseekagent"))

import dotenv  # noqa: E402

dotenv.load_dotenv(ROOT / ".env")

from prompts_text import EHR_ONLY_TOOL_CONTENT_FULL  # noqa: E402
from run_text import normalize_tool_calls  # noqa: E402
from run_text_curated import (  # noqa: E402
    _extract_answer_block,
    _extract_embedded_response_json,
)

FORCE_AGENTIC = (
    "Your previous turn ended without a final answer. Do not retrieve any more "
    "data and do not continue reasoning at length. Based only on the evidence "
    "already gathered and your reasoning above, call ehr.finish now with your "
    "final answer. If candidate answers were provided, each item must match a "
    "candidate verbatim."
)
FORCE_CURATED = (
    "Your reasoning above was cut off at the output limit before you gave an "
    "answer. Do not restart the analysis. Based on your reasoning so far, call "
    "ehr.finish now with your final answer (a list with one item per answer). "
    "If <candidate_answers> were provided, each item must match a candidate "
    "verbatim; for a yes/no task answer exactly \"yes\" or \"no\"."
)
FINISH_TOOL = [t for t in json.loads(EHR_ONLY_TOOL_CONTENT_FULL)
               if t["function"]["name"] == "ehr.finish"]


def is_curated(row: dict) -> bool:
    return "record_context" in row


def needs_fix(row: dict) -> bool:
    if row.get("status") == "error":
        return False  # infra failure: belongs to the normal resume path
    if is_curated(row):
        text = row["messages"][1].get("content") or ""
        return not _extract_answer_block(text) and \
            not _extract_embedded_response_json(text)
    return row.get("status") == "incomplete"


_IMAGE_CACHE: dict[str, list[dict]] = {}


def _image_blocks(row: dict) -> list[dict]:
    """Re-attach the images a multimodal curated row was originally shown.

    The curated runner sends images out of band (`invoker.invoke(..., images)`),
    so they are not in `messages`; only the resolved paths are recorded in
    `multimodal_summary`. Same resize as the original run (`image_max_edge`).
    Blocks use the Anthropic image shape, which the Bedrock/Vertex generators
    pass through and the OpenAI-compatible generator converts to `image_url`.
    """
    paths = ((row.get("multimodal_summary") or {}).get("resolved_image_paths")
             or [])
    if not paths:
        return []
    from run_multimodal_curated import _load_image_b64  # lazy: needs PIL
    blocks = []
    for pth in paths:
        if pth not in _IMAGE_CACHE:
            loaded = _load_image_b64(Path(pth), IMAGE_MAX_EDGE)
            _IMAGE_CACHE[pth] = [] if loaded is None else [{
                "type": "image",
                "source": {"type": "base64", "media_type": loaded[1],
                           "data": loaded[0]}}]
        blocks.extend(_IMAGE_CACHE[pth])
    return blocks


IMAGE_MAX_EDGE = 1568


def build_messages(row: dict, clip: int | None) -> list[dict]:
    msgs = copy.deepcopy(row["messages"])
    if is_curated(row):
        msgs = msgs[:2]
        imgs = _image_blocks(row)
        if imgs:
            msgs[0] = {"role": "user",
                       "content": [{"type": "text", "text": msgs[0]["content"]}] + imgs}
        msgs[1] = {"role": "assistant", "content": msgs[1].get("content") or ""}
        force = FORCE_CURATED
    else:
        # The backend does not replay reasoning_content, so a reasoning-only
        # final turn would reach the model empty. Surface it as text.
        last = msgs[-1]
        if last.get("role") == "assistant" and not last.get("tool_calls"):
            if not (last.get("content") or "").strip():
                last["content"] = (last.get("reasoning_content") or "").strip() \
                    or "(no answer given)"
        force = FORCE_AGENTIC
    if clip:  # context-overflow fallback: shrink tool results, oldest included
        for m in msgs:
            if m.get("role") == "tool" and isinstance(m.get("content"), str) \
                    and len(m["content"]) > clip:
                m["content"] = m["content"][:clip] + "\n...[clipped for final-answer round]"
    msgs.append({"role": "user", "content": force})
    return msgs


async def fix_row(row: dict, gen, sem, max_tokens: int) -> dict:
    async with sem:
        err = None
        for clip in (None, 4000, 1000, 300):
            try:
                msgs = build_messages(row, clip)
                resp = await gen.chat_completion(
                    messages=msgs, tools=FINISH_TOOL, tool_choice="ehr.finish",
                    temperature=0.0, max_tokens=max_tokens)
                m = resp["choices"][0]["message"]
                calls = normalize_tool_calls(m.get("tool_calls") or [])
                calls = [c for c in calls if "finish" in c["function"]["name"]]
                out = dict(row)
                info = {"applied": True, "clip_tool_results": clip,
                        "original_status": row.get("status"),
                        "original_stop_reason": row.get("stop_reason"),
                        "answered": bool(calls)}
                if is_curated(row):
                    info["original_finish_args"] = \
                        row["messages"][1]["tool_calls"][0]["function"]["arguments"]
                    base = [row["messages"][0],
                            {"role": "assistant",
                             "content": row["messages"][1].get("content") or ""}]
                else:
                    base = list(row["messages"])
                turn = {"role": "assistant", "content": m.get("content", ""),
                        "tool_calls": calls or None}
                if m.get("reasoning_content"):
                    turn["reasoning_content"] = m["reasoning_content"]
                new = base + [msgs[-1], turn]
                for c in calls:
                    new.append({"role": "tool", "tool_call_id": c["id"],
                                "content": "Finish (forced final round)"})
                if not calls and is_curated(row):
                    # keep the row scoreable: empty prediction, not a junk one
                    sid = "forced_final_empty"
                    new[-1]["tool_calls"] = [{
                        "id": sid, "type": "function",
                        "function": {"name": "ehr.finish",
                                     "arguments": json.dumps({"response": []})}}]
                    new.append({"role": "tool", "tool_call_id": sid,
                                "content": "Finish (forced final round, no answer)"})
                out["messages"] = new
                if calls and not is_curated(row):
                    out.update(completed=True, status="success",
                               stop_reason="finish_tool_call")
                out["forced_final"] = info
                return out
            except Exception as exc:  # noqa: BLE001
                err = exc
                text = str(exc).lower()
                if not any(k in text for k in ("context", "too long", "maximum",
                                               "token", "length")):
                    break
        out = dict(row)
        out["forced_final"] = {"applied": False,
                               "error": f"{type(err).__name__}: {err}"[:500]}
        return out


def make_generator(args):
    """Same backends and request settings as clinseekagent/run_text.py."""
    if args.backend == "bedrock":
        from bedrock_backend import BedrockAsyncGenerator
        from run_text import configure_bedrock_auth, resolve_bedrock_model_id

        configure_bedrock_auth(None)
        return BedrockAsyncGenerator(
            model_id=resolve_bedrock_model_id(args.model),
            region_name=args.bedrock_region,
            max_tokens_default=args.max_tokens,
            max_workers=args.concurrency,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
    if args.backend == "vertex":
        from vertex_backend import VertexAsyncGenerator

        return VertexAsyncGenerator(
            model_id=args.model,
            project_id=args.vertex_project,
            region=args.vertex_region,
            max_tokens_default=args.max_tokens,
            enable_thinking=args.enable_thinking,
            thinking_budget_tokens=args.thinking_budget_tokens,
            reasoning_effort=args.reasoning_effort,
        )
    from vllm_backend import VLLMOpenAIAsyncGenerator

    return VLLMOpenAIAsyncGenerator(
        model_name=args.model,
        base_url=args.api_base_url,
        api_key=args.api_key or os.environ.get("VLLM_API_KEY") or "EMPTY",
        max_tokens_default=args.max_tokens,
        max_workers=args.concurrency,
        enable_thinking=args.enable_thinking,
        reasoning_effort=args.reasoning_effort,
        request_max_attempts=args.api_max_attempts,
        min_request_interval=args.api_min_request_interval_seconds,
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True, help="run directory with results.jsonl")
    ap.add_argument("--dst", required=True, help="output run directory")
    ap.add_argument("--backend", choices=["bedrock", "vertex", "vllm"], default="vllm")
    ap.add_argument("--model", required=True,
                    help="model id for the backend (the source run's model)")
    ap.add_argument("--bedrock_region", default=os.environ.get("BEDROCK_REGION", "us-east-1"))
    ap.add_argument("--vertex_project", default=None)
    ap.add_argument("--vertex_region", default=None)
    ap.add_argument("--api_base_url", default=os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:4000/v1"))
    ap.add_argument("--api_key", default=None)
    ap.add_argument("--api_max_attempts", type=int, default=5)
    ap.add_argument("--api_min_request_interval_seconds", type=float, default=0.0)
    ap.add_argument("--enable_thinking", dest="enable_thinking", action="store_true",
                    help="reasoning on (default, as in the source runs)")
    ap.add_argument("--disable_thinking", dest="enable_thinking", action="store_false")
    ap.set_defaults(enable_thinking=True)
    ap.add_argument("--thinking_budget_tokens", type=int, default=4096)
    ap.add_argument("--reasoning_effort", default="high")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--max_tokens", type=int, default=8192)
    ap.add_argument("--limit", type=int, default=0, help="smoke: fix only N rows")
    args = ap.parse_args()
    global IMAGE_MAX_EDGE

    src, dst = Path(args.src), Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(l) for l in open(src / "results.jsonl")]
    src_cfg = json.load(open(src / "run_config.json")) if (src / "run_config.json").exists() else {}
    IMAGE_MAX_EDGE = int(src_cfg.get("image_max_edge") or IMAGE_MAX_EDGE)
    out_path = dst / "results.jsonl"
    done: dict[str, dict] = {}
    if out_path.exists():  # resume: keep rows whose forced round succeeded
        for l in open(out_path):
            r = json.loads(l)
            ff = r.get("forced_final")
            if ff is None or ff.get("applied"):
                done[r["qid"]] = r
    side = dst / "forced_rows.jsonl"  # incremental; survives a crash
    if side.exists():
        for l in open(side):
            r = json.loads(l)
            if (r.get("forced_final") or {}).get("applied"):
                done[r["qid"]] = r
    todo = [r for r in rows if needs_fix(r) and r["qid"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    todo_ids = {r["qid"] for r in todo}
    print(f"{src.name}: rows={len(rows)} need_fix={sum(needs_fix(r) for r in rows)} "
          f"already_done={len(done)} this_pass={len(todo)}", flush=True)

    gen = make_generator(args)
    sem = asyncio.Semaphore(args.concurrency)
    fixed: dict[str, dict] = {}
    n = 0
    for fut in asyncio.as_completed([fix_row(r, gen, sem, args.max_tokens) for r in todo]):
        r = await fut
        fixed[r["qid"]] = r
        with open(side, "a") as sf:
            sf.write(json.dumps(r, ensure_ascii=False) + "\n")
        n += 1
        ff = r["forced_final"]
        print(f"[{n}/{len(todo)}] {r['qid']} applied={ff.get('applied')} "
              f"answered={ff.get('answered')} {ff.get('error', '')}", flush=True)

    tmp = dst / "results.jsonl.tmp"
    with open(tmp, "w") as f:
        for r in rows:
            q = r["qid"]
            if args.limit and q not in todo_ids and q not in done:
                if needs_fix(r):
                    continue  # smoke pass: leave unfixed rows out
            f.write(json.dumps(fixed.get(q) or done.get(q) or r, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)
    cfg = json.load(open(src / "run_config.json")) if (src / "run_config.json").exists() else {}
    cfg["forced_final_round"] = {
        "source_run": str(src), "backend": args.backend, "model": args.model,
        "max_tokens": args.max_tokens, "enable_thinking": args.enable_thinking,
        "rows_patched": sum(1 for r in list(fixed.values()) + list(done.values())
                            if (r.get("forced_final") or {}).get("applied")),
        "rows_failed": sum(1 for r in fixed.values()
                           if not r["forced_final"].get("applied")),
        "token_usage": getattr(gen, "token_usage", None),
    }
    json.dump(cfg, open(dst / "run_config.json", "w"), indent=2)
    print(json.dumps(cfg["forced_final_round"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
