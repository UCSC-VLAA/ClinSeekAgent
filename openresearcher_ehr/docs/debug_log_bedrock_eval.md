# Debug Log: Bedrock Evaluation Setup (2026-04-09)

## Summary

Set up and ran `openresearcher_ehr` evaluation using AWS Bedrock for three non-Anthropic models: Kimi K2.5, GPT-OSS-120B, and MiniMax M2.5. Encountered and fixed several issues along the way.

---

## Issue 1: Converse API caused silent model responses

**Symptom:** Kimi K2.5 returned empty responses (`raw_blocks=0`, `stopReason=end_turn`) after 2 tool calls. Conversations ended prematurely with 0% completion rate.

**Root cause:** The initial implementation used the Bedrock **Converse API** for non-Anthropic models. The `_convert_messages_to_converse()` method silently **dropped empty assistant messages** (line `if assistant_content:` skipped empty lists). This broke the alternating `user → assistant → user → assistant` role structure required by the API:

```
Actual messages:                     Converse messages (after conversion):
assistant (toolUse: load_ehr)   →    assistant (toolUse)         ← OK
tool (result)                   →    user (toolResult)           ← OK
assistant (EMPTY response)      →    *** DROPPED ***
user (continuation prompt)      →    user (continue)             ← MERGED with toolResult above!
```

The model saw a mangled conversation and returned empty.

**Fix:** Replaced the Converse API path entirely. Non-Anthropic models on Bedrock (Kimi, GPT-OSS, MiniMax) accept **OpenAI-compatible format** via `invoke_model`. Since `deploy_agent.py` already uses OpenAI-style messages internally, we pass them through directly with minimal cleanup — no format conversion needed.

**Changed:** `bedrock_generator.py` — replaced `_chat_completion_converse()` with `_chat_completion_openai()`.

---

## Issue 2: Model ID not found in `ca-west-1`

**Symptom:** `ValidationException: The provided model identifier is invalid` for `moonshotai.kimi-k2.5`.

**Root cause:** Third-party models (Kimi, GPT-OSS, MiniMax) are not available in `ca-west-1`. They are available in `us-east-1`, `us-west-2`, `us-east-2`.

**Fix:** Pass `BEDROCK_REGION=us-east-1` when launching non-Anthropic model evaluations. Claude models remain in `ca-west-1`.

---

## Issue 3: `content: null` causing `TypeError: object of type 'NoneType' has no len()`

**Symptom:** Crash in `deploy_agent.py` at the line logging response content length.

**Root cause:** OpenAI-format responses set `"content": null` (not `""`) when the model only produces tool calls. Python's `dict.get("content", "")` returns `null`/`None` when the key exists with a null value — the default only applies when the key is **missing**.

**Fix:** Changed `message.get("content", "")` to `message.get("content") or ""` in `deploy_agent.py:493`. Same for `tool_calls`.

---

## Issue 4: Context overflow (253k+ tokens)

**Symptom:** `BadRequestError: You passed 253953 input tokens... context length is only 262144 tokens`.

**Root cause:** Tool results from EHR queries can be massive. Examples from a single conversation:
- `chartevents` table dump: **785,609 chars**
- `labevents` table dump: **194,438 chars**
- Tool results were **97-99% of total conversation size**

The model calls `get_records_by_time` on large tables and gets thousands of rows back. After a few such calls the conversation exceeds the context limit.

**Why this didn't happen with Claude:** Claude tends to use targeted SQL queries (`run_sql_query` with WHERE/LIMIT) rather than dumping entire tables. Also the Claude eval only ran 7 queries before being stopped.

**Fix:** Added a 50k char truncation limit for individual tool results in `deploy_agent.py`:
```python
MAX_TOOL_RESULT_CHARS = 50000
if len(result) > MAX_TOOL_RESULT_CHARS:
    result = result[:MAX_TOOL_RESULT_CHARS] + f"\n... [truncated from {truncated_len:,} to {MAX_TOOL_RESULT_CHARS:,} chars]"
```

Also added a secondary safety net in `bedrock_generator.py` (`_truncate_tool_results`) that truncates old tool results if the serialized request body exceeds 10MB.

---

## Issue 5: Malformed JSON tool arguments poisoning conversation

**Symptom:** `ValidationException: Expecting ',' delimiter` or `Unterminated string` errors from Bedrock API.

**Root cause:** The model sometimes generates invalid JSON in tool call arguments (e.g., unterminated strings). The bad arguments get stored in the assistant message in the conversation history. On the next API call, the entire conversation (including the malformed JSON) is serialized and sent to Bedrock, which rejects it.

**Fix:** Added JSON validation in `_prepare_openai_messages()` in `bedrock_generator.py`. Each tool call's arguments are validated with `json.loads()`; if invalid, they are replaced with `"{}"` before sending to the API.

---

## Issue 6: BioLORD-2023 model not found

**Symptom:** MCP server crashed at startup with `FileNotFoundError: Path .../models/BioLORD-2023 not found`.

**Root cause:** The `candidate_tools.py` module loads a `BioLORD-2023` sentence-transformer model for semantic search. The expected path `models/BioLORD-2023` didn't exist.

**Fix:** Symlinked from existing location: `ln -sf /fsx-shared/juncheng/EHR/models/BioLORD-2023 /fsx-shared/juncheng/DeepMed-eval/models/BioLORD-2023`.

---

## Issue 7: MCP server script hardcoded paths

**Symptom:** MCP server couldn't start — wrong Python path and data path.

**Fix:** Updated `scripts/run/run_mcp_server.sh`:
- Python: `/fsx-shared/juncheng/DeepMed-eval/.venv_mcp/bin/python`
- Data: `/fsx-shared/juncheng/DeepMed-eval/data/EHRAgentBench`

---

## Files Modified

| File | Changes |
|------|---------|
| `bedrock_generator.py` | Added `_chat_completion_openai()` for non-Anthropic models; added `_prepare_openai_messages()` with JSON sanitization; added `_truncate_tool_results()` safety net |
| `deploy_agent.py` | Fixed `None` content handling; replaced retry logic with completion-aware continuation; added 50k char tool result truncation |
| `scripts/run/run_mcp_server.sh` | Updated Python path and data path |

## Current Status (2026-04-09)

Three evaluations running in parallel on 600-query subset:
- `moonshotai.kimi-k2.5` — concurrency 20, `us-east-1`
- `openai.gpt-oss-120b-1:0` — concurrency 20, `us-east-1`
- `minimax.minimax-m2.5` — concurrency 20, `us-east-1`

MCP server running on GPU 0, port 5103.
