#!/bin/bash
# Apply OpenResearcher researcher_v2 SGLang patches for Qwen3.5 MoE VLM support.
#
# What it does:
#   - Adds Qwen3_5MoeTextConfig class + handles dict/namespace text_config in
#     sglang/srt/utils/hf_transformers_utils.py::get_hf_text_config so sglang
#     can load our SFT checkpoint (whose tokenizer_config.json declares
#     TokenizersBackend and whose top-level config is
#     Qwen3_5MoeForConditionalGeneration).
#   - Adds sglang/srt/configs/qwen3_5.py + sglang/srt/models/qwen3_5.py with
#     Qwen3_5MoeForConditionalGeneration wiring so the sglang model registry
#     knows the arch — without this, sglang rollout init dies with
#     "No processor registered for architecture: ['Qwen3_5MoeForConditionalGeneration']".
#
# Also installs `decord`, which sglang's qwen_vl.py processor imports at
# module level. Without decord, the processor's import silently fails (the
# `import_processors` helper eats all ImportErrors), so the arch never
# registers and the error above appears at server-launch time.
#
# Idempotent: running it twice is a no-op. Backups go to *.orig if not
# already present.

set -euo pipefail

VENV="${VENV:-/fsx-shared/juncheng/EHR/venvs/qwen3_5_rl}"
SGLANG_SRT="$VENV/lib/python3.10/site-packages/sglang/srt"

if [ ! -d "$SGLANG_SRT" ]; then
    echo "ERROR: sglang/srt not found at $SGLANG_SRT"
    echo "  Install sglang first: uv pip install --no-deps 'sglang[srt]==0.5.9'"
    exit 1
fi

echo "Applying OpenResearcher sglang patches under $SGLANG_SRT"

for rel in configs/qwen3_5.py models/qwen3_5.py utils/hf_transformers_utils.py; do
    src_url="https://raw.githubusercontent.com/Chtholly17/OpenResearcher/researcher_v2/verl_rl/patches/sglang_files/sglang/srt/$rel"
    dst="$SGLANG_SRT/$rel"
    mkdir -p "$(dirname "$dst")"
    if [ -f "$dst" ] && [ ! -f "$dst.orig" ]; then
        cp -p "$dst" "$dst.orig"
    fi
    echo "  fetching $rel"
    curl -fsSL "$src_url" -o "$dst"
    echo "    $(wc -l <"$dst") lines"
done

echo "Installing decord (required by sglang's qwen_vl.py processor)"
if [ -x "$VENV/bin/uv" ]; then
    VIRTUAL_ENV="$VENV" uv pip install --no-cache decord
else
    VIRTUAL_ENV="$VENV" /usr/local/bin/uv pip install --no-cache decord
fi

echo ""
echo "Smoke test: confirm Qwen3_5MoeForConditionalGeneration is registered"
"$VENV/bin/python" - <<'PY'
from sglang.srt.managers.multimodal_processor import import_processors, PROCESSOR_MAPPING
import_processors("sglang.srt.multimodal.processors")
assert any("Qwen3_5Moe" in c.__name__ for c in PROCESSOR_MAPPING.keys()), \
    "Qwen3_5Moe processor still not registered — patch failed"
print("OK: Qwen3_5MoeForConditionalGeneration processor registered.")
PY
