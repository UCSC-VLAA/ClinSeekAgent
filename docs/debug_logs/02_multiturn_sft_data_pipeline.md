# Multi-turn SFT Data Pipeline & Training Architecture

## 1. Dataset Overview

### Source
- **Dataset**: `Letian2003/DeepMed_trajectory` (HuggingFace)
- **Domain**: Multi-turn EHR (Electronic Health Records) medical conversations with tool calls
- **Task**: Train a model to act as a clinical research assistant that queries EHR databases, uses browser tools, and makes diagnostic/prognostic decisions

### Statistics
| Metric | Value |
|--------|-------|
| Total samples | 2498 |
| Train samples | 2449 (98%) |
| Val samples | 49 (2%) |
| Min turns/sample | 33 |
| Max turns/sample | 187 |
| Mean turns/sample | 81.2 |
| Max token length | ~365K tokens (before truncation) |

### Conversation Structure
Each sample is a multi-turn OpenAI-native chat conversation with roles: `system`, `user`, `assistant`, `tool`. Tool calls are preserved as **structured** `tool_calls` objects on assistant turns, and `role="tool"` responses keep their `tool_call_id` so the tokenizer's chat template can render model-native tool syntax.

```
Turn 0: role=system     content="You are a research assistant with access to both web browsing and clinical EHR tools..."
Turn 1: role=user       content="<task_instruction> Your current task is to act as a diagnostician..."
Turn 2: role=assistant  content="I'll start by loading the patient's EHR..."
                        tool_calls=[{id, type:"function",
                                     function:{name:"ehr.load_ehr",
                                               arguments:{subject_id:"10064835", timestamp:"..."}}},
                                    {id, type:"function",
                                     function:{name:"ehr.get_table_names",
                                               arguments:{subject_id:"10064835"}}}]
Turn 3: role=tool       tool_call_id="toolu_..._01EW..."
                        content="Loading Candidate Tables: - Loading 'microbiologyevents_candidates'..."
Turn 4: role=tool       tool_call_id="toolu_..._0124..."
                        content="Available Tables: admissions, diagnoses_icd, ..."
Turn 5: role=assistant  tool_calls=[{function:{name:"ehr.run_sql_query", arguments:{...}}}]
Turn 6: role=tool       content="subject_id gender anchor_age..."
...
```

The tokenizer's chat template (e.g. Qwen3.5's `chat_template.jinja`) renders these structured fields into the model's **native** tool-call syntax. For Qwen3.5 that is:

```
<|im_start|>assistant
<think>
</think>

I'll start by loading the patient's EHR...
<tool_call>
<function=ehr.load_ehr>
<parameter=subject_id>
10064835
</parameter>
<parameter=timestamp>
2169-05-18 21:53:00
</parameter>
</function>
</tool_call><|im_end|>
<|im_start|>user
<tool_response>
Loading Candidate Tables: ...
</tool_response><|im_end|>
```

The pattern alternates between:
- **Assistant turns**: reasoning + one or more structured `tool_calls` (model learns to generate both the natural-language reasoning and the `<tool_call>…</tool_call>` XML).
- **Tool turns**: tool responses (rendered as `<|im_start|>user\n<tool_response>…</tool_response><|im_end|>` by Qwen3.5; not trained on).

**Why native format matters**: the prior iteration flattened tool calls into `[Tool Call: name(args)]` plain text. That produced a non-native syntax no vLLM tool parser (`hermes`, `qwen3_xml`, `llama3_json`, …) could parse. With the new format, outputs are directly consumed by `--tool-call-parser qwen3_xml` (or the equivalent for the target model) — verified end-to-end against `vllm.entrypoints.openai.tool_parsers` (vLLM 0.11.0, `qwen3_xml`): 200 trajectories, 4055/4055 assistant blocks parsed, 8652/8652 tool calls recovered with correct names/ids.

---

## 2. Data Preparation

### Script: `/fsx-shared/juncheng/EHR/prepare_deepmed_data.py`

The raw HuggingFace dataset ships OpenAI-native messages with `tool_calls`, `tool_call_id`, and `tool` role turns. The preparation script **preserves this structure verbatim** so the tokenizer's chat template can render the model-native tool-call syntax (e.g. `<tool_call>/<tool_response>` for Qwen3.5).

Key transformations:
1. **Arguments coerced to dict**: the source stores `tool_calls[*].function.arguments` as a JSON **string**, but Qwen3.5's chat template iterates `arguments|items`, which requires a dict. The script parses each string with `json.loads`; malformed JSON falls back to `{"_raw": "<original text>"}`.
2. **Assistant turns keep `tool_calls`**: `{id, type:"function", function:{name, arguments:<dict>}}` survives end-to-end.
3. **Tool turns keep `role="tool"` + `tool_call_id`**: the chat template uses the id (and adjacency) to wrap the response in `<tool_response>`.
4. **No flattening, no role rewriting, no merging**: consecutive same-role turns are left alone — the chat template is designed for them.

```python
# Core transformation logic (simplified)
def _coerce_arguments(arguments):
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {"_raw": arguments}
    return parsed if isinstance(parsed, dict) else {"_raw": arguments}


def _normalize_message(m):
    role = m["role"]
    out = {"role": role, "content": m.get("content") or ""}

    if role == "assistant" and m.get("tool_calls"):
        out["tool_calls"] = [{
            "id": tc.get("id", ""),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc["function"]["name"],
                "arguments": _coerce_arguments(tc["function"]["arguments"]),
            },
        } for tc in m["tool_calls"]]
    elif role == "tool" and m.get("tool_call_id"):
        out["tool_call_id"] = m["tool_call_id"]

    return out
```

### Output Format
Saved as **pandas parquet** (NOT HuggingFace datasets parquet - important distinction, see Bug 7 in debugging doc):

```
~/data/deepmed_trajectory/
    train.parquet
    val.parquet
```

Each row has a single column `messages` containing a Python list of OpenAI-style dicts:
```python
[
    {"role": "system", "content": "You are a research assistant..."},
    {"role": "user",   "content": "<task_instruction>..."},
    {"role": "assistant",
     "content": "I'll start by loading the patient's EHR...",
     "tool_calls": [
         {"id": "toolu_bdrk_01EW...", "type": "function",
          "function": {"name": "ehr.load_ehr",
                       "arguments": {"subject_id": "10064835",
                                     "timestamp": "2169-05-18 21:53:00"}}},
         {"id": "toolu_bdrk_0124...", "type": "function",
          "function": {"name": "ehr.get_table_names",
                       "arguments": {"subject_id": "10064835"}}},
     ]},
    {"role": "tool", "tool_call_id": "toolu_bdrk_01EW...",
     "content": "Loading Candidate Tables: ..."},
    {"role": "tool", "tool_call_id": "toolu_bdrk_0124...",
     "content": "Available Tables: admissions, ..."},
    ...
]
```

### Source caveat: no `tools` schema

`Letian2003/DeepMed_trajectory` does **not** ship a `tools` list alongside the messages — all tool descriptions are already embedded in the system prompt text. So the chat template won't emit a `<tools>…</tools>` preamble. At inference time, pass an explicit OpenAI `tools=[…]` schema to the vLLM server (or rely on the system-prompt text plus `--tool-call-parser qwen3_xml`).

---

## 3. Multi-turn SFT Dataset Class

### File: `verl/verl/utils/dataset/multiturn_sft_dataset.py`

The `MultiTurnSFTDataset` class loads the parquet and processes each sample into tokenized tensors.

### `__getitem__()` Flow

```
┌─────────────────────────────────────────────┐
│ 1. Load messages from parquet row           │
│    messages = [{"role": "system", ...}, ...] │
└──────────────────┬──────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────┐
│ 2. Tokenize messages                         │
│    Primary: per-message apply_chat_template  │
│    Fallback: full-conversation tokenization  │
│    (Qwen3.5 uses fallback path)             │
└──────────────────┬──────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────┐
│ 3. Build loss_mask                           │
│    loss_mask[i] = 1 if token i is from      │
│    assistant turn, else 0                    │
│    (Only train on assistant outputs)         │
└──────────────────┬──────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────┐
│ 4. Compute position_ids                      │
│    Simple 1D arange for text-only models     │
│    4D for VL models (Qwen3.5 uses None)     │
└──────────────────┬──────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────┐
│ 5. Handle padding/truncation                 │
│    pad_mode=no_padding: return variable-len  │
│    truncation=left: keep last max_length     │
│    tokens (most recent conversation context) │
└──────────────────┬──────────────────────────┘
                   │
                   v
┌─────────────────────────────────────────────┐
│ Output: {input_ids, position_ids, loss_mask} │
│    All tensors of shape (seq_len,)           │
└─────────────────────────────────────────────┘
```

### Tokenization: Primary vs Fallback Path

**Primary path** (works for most models like Llama, Qwen2):
```python
for i, message in enumerate(messages):
    # Tokenize each message individually
    _input_ids, _loss_mask, ... = self._process_single_message(
        index=i, message=message, full_message=messages, ...
    )
    input_ids.append(_input_ids)
    loss_mask.append(_loss_mask)  # 1 for assistant, 0 for user/system
```
This calls `apply_chat_template(messages=[single_msg])` per message, which fails for Qwen3.5.

**Fallback path** (for Qwen3.5 and other strict templates):
```python
# Tokenize full conversation at once
full_inputs = apply_chat_template(processor, messages=messages, ...)
input_ids = full_inputs["input_ids"][0]
attention_mask = full_inputs["attention_mask"][0]

# Build loss_mask via incremental prefix tokenization
loss_mask = torch.zeros_like(input_ids)
prev_len = 0
for i, message in enumerate(messages):
    if message["role"] == "system":
        continue  # skip system-only prefixes
    prefix = messages[:i + 1]
    try:
        prefix_inputs = apply_chat_template(processor, messages=prefix, ...)
        cur_len = prefix_inputs["input_ids"][0].shape[0]
        if message["role"] == "assistant":
            loss_mask[prev_len:cur_len] = 1  # train on assistant tokens
        prev_len = cur_len
    except Exception:
        continue  # skip problematic prefixes
```

This incrementally tokenizes `messages[:1]`, `messages[:2]`, ..., `messages[:n]` and uses the length differences to identify which tokens belong to each turn.

### Loss Mask Example

```
Tokens:  [SYS] [USR] [AST] [USR] [AST] [USR] [AST] ...
Mask:      0     0     1     0     1     0     1   ...

Loss is computed ONLY where mask=1 (assistant tokens).
System and user tokens provide context but are not trained on.
```

---

## 4. Training Pipeline

### Framework: verl (v0.8.0.dev)

verl uses Hydra for configuration and FSDP for distributed training.

### Architecture Flow

```
┌──────────────────────────────────────────────────────┐
│                  sft_trainer.py                       │
│  - Loads dataset (MultiTurnSFTDataset)               │
│  - Creates DataLoader with SFTTensorCollator         │
│  - Initializes FSDPEngine with model                 │
│  - Training loop: epochs x steps                     │
└───────────────────────┬──────────────────────────────┘
                        │
                        v
┌──────────────────────────────────────────────────────┐
│               FSDPEngine (transformer_impl.py)        │
│  - Wraps model with FSDP (shards across GPUs)        │
│  - forward_backward_batch():                         │
│    1. prepare_micro_batches (split global batch)      │
│    2. For each micro_batch:                          │
│       a. prepare_model_inputs() - handle padding,     │
│          position_ids, attention_mask                 │
│       b. model(**inputs) - forward pass               │
│       c. loss_function() - compute SFT loss          │
│       d. backward() - compute gradients              │
│    3. optimizer.step() + scheduler.step()            │
└──────────────────────────────────────────────────────┘
```

### Training Script

```bash
# examples/sft/deepmed/run_qwen3_5_27b_sft_default.sh
torchrun --standalone --nnodes=1 --nproc_per_node=4 \
     -m verl.trainer.sft_trainer \
    data.train_files=$HOME/data/deepmed_trajectory/train.parquet \
    data.val_files=$HOME/data/deepmed_trajectory/val.parquet \
    data.train_batch_size=128 \
    data.micro_batch_size_per_gpu=2 \
    data.max_length=4096 \
    data.truncation=left \
    data.use_dynamic_bsz=False \
    data.messages_key=messages \
    data.ignore_input_ids_mismatch=True \
    model.path=Qwen/Qwen3.5-27B \
    model.trust_remote_code=True \
    model.use_remove_padding=False \
    trainer.total_epochs=3 \
    trainer.logger=wandb \
    ...
```

### Key Configuration Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| `data.train_batch_size` | 128 | Global batch size across all GPUs |
| `data.micro_batch_size_per_gpu` | 2 | Sequences per GPU per micro-step |
| `data.max_length` | 4096 | Maximum sequence length (truncated) |
| `data.truncation` | left | Keep last 4096 tokens (most recent context) |
| `data.use_dynamic_bsz` | False | Required for Qwen3.5 compatibility |
| `model.use_remove_padding` | False | Required for Qwen3.5 MRoPE compatibility |
| `model.path` | Qwen/Qwen3.5-27B | 27.36B parameter VL model |
| `trainer.total_epochs` | 3 | 19 steps/epoch x 3 = 57 total steps |

### Training Metrics
- **Steps per epoch**: 19 (2449 samples / 128 batch_size)
- **Total steps**: 57 (3 epochs)
- **Time per step**: ~5 min (after first warmup step of ~10 min)
- **Estimated total time**: ~5 hours
- **GPU memory**: ~134 GB / 139.8 GB per GPU (93%)

---

## 5. FSDP Model Preparation (for Qwen3.5)

### Model Loading
```python
# verl/workers/engine/fsdp/transformer_impl.py
auto_class = get_hf_auto_model_class(hf_config)  # -> AutoModelForImageTextToText
module = auto_class.from_pretrained("Qwen/Qwen3.5-27B", ...)
# Loads as Qwen3_5ForConditionalGeneration (VL model, 27.36B params)
```

### FSDP Wrapping
- Strategy: FSDP1 (Full Sharding Data Parallelism)
- dtype: bfloat16
- Gradient checkpointing: enabled
- After FSDP: 25.52 GB allocated, 58 GB reserved per GPU

### Position IDs Handling
Qwen3.5 uses MRoPE (Multi-Resolution Rotary Position Embedding) with 4D position_ids:
- Dimension 0: text positions
- Dimensions 1-3: temporal, height, width (for vision tokens)

For text-only SFT, `position_ids=None` is passed to let the model auto-compute from `cache_position`. This avoids shape mismatches between the dataset's 1D position_ids and the model's 4D expectation.

---

## 6. Loss Function

verl computes cross-entropy loss only on tokens where `loss_mask=1` (assistant tokens):

```python
# Simplified from verl's loss computation
logits = model(input_ids, attention_mask, position_ids)
shift_logits = logits[..., :-1, :]
shift_labels = input_ids[..., 1:]
shift_loss_mask = loss_mask[..., 1:]

loss = F.cross_entropy(shift_logits.view(-1, vocab_size), shift_labels.view(-1), reduction='none')
loss = (loss * shift_loss_mask.view(-1)).sum() / shift_loss_mask.sum()
```

This ensures the model only learns to generate assistant responses, not to reproduce system prompts or user queries.
