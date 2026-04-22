"""xray_phrase_grounding tool (microsoft/maira-2).

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/grounding.py
(Apache 2.0, credits MedRAX).
"""
import os
import threading
from typing import Annotated, Any, Dict

from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import new_artifact_path, resolve_image_path


_MODEL_BUNDLE = None
_MODEL_LOCK = threading.Lock()
_DEVICE = None


def _get_device():
    global _DEVICE
    if _DEVICE is None:
        from mcp_image.tools.base import pick_tool_device
        _DEVICE = pick_tool_device("grounding")
    return _DEVICE


def _get_model():
    global _MODEL_BUNDLE
    if _MODEL_BUNDLE is not None:
        return _MODEL_BUNDLE
    with _MODEL_LOCK:
        if _MODEL_BUNDLE is not None:
            return _MODEL_BUNDLE
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        model_id = os.environ.get("MAIRA2_MODEL_ID", "microsoft/maira-2")
        cache_dir = os.environ.get("HF_CACHE_DIR") or os.environ.get("TRANSFORMERS_CACHE")
        processor = AutoProcessor.from_pretrained(model_id, cache_dir=cache_dir, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            cache_dir=cache_dir,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16,
        ).to(_get_device())
        model.eval()
        _MODEL_BUNDLE = (processor, model)
        return _MODEL_BUNDLE


def _visualize(image, bboxes, phrase):
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_path = new_artifact_path(suffix=".png", prefix="grounding_")
    plt.figure(figsize=(12, 12))
    plt.imshow(image, cmap="gray")
    for bbox in bboxes:
        x1, y1, x2, y2 = bbox
        w = x2 - x1
        h = y2 - y1
        plt.gca().add_patch(
            plt.Rectangle(
                (x1 * image.width, y1 * image.height),
                w * image.width,
                h * image.height,
                fill=False, color="red", linewidth=2,
            )
        )
    plt.title(f"Located: {phrase}", pad=20)
    plt.axis("off")
    plt.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close()
    return str(out_path)


def _run(image_path: str, phrase: str, max_new_tokens: int) -> Dict[str, Any]:
    import torch
    from PIL import Image

    processor, model = _get_model()
    image = Image.open(image_path)
    if image.mode != "RGB":
        image = image.convert("RGB")

    inputs = processor.format_and_preprocess_phrase_grounding_input(
        frontal_image=image, phrase=phrase, return_tensors="pt"
    )
    inputs = {k: v.to(_get_device()) for k, v in inputs.items()}

    with torch.no_grad():
        output = model.generate(**inputs, max_new_tokens=max_new_tokens, use_cache=True)

    prompt_length = inputs["input_ids"].shape[-1]
    decoded_text = processor.decode(output[0][prompt_length:], skip_special_tokens=True)
    predictions = processor.convert_output_to_plaintext_or_grounded_sequence(decoded_text)

    metadata = {
        "image_path": image_path,
        "original_size": list(image.size),
        "model_input_size": list(inputs["pixel_values"].shape[-2:]),
        "device": str(_get_device()),
        "analysis_status": "completed",
    }

    if not predictions:
        return {
            "output": {"predictions": [], "visualization_path": None, "raw_decoded": decoded_text},
            "metadata": {**metadata, "analysis_status": "completed_no_finding"},
        }

    processed = []
    for pred_phrase, pred_bboxes in predictions:
        if not pred_bboxes:
            continue
        model_bboxes = [list(b) for b in pred_bboxes]
        image_bboxes = [
            list(processor.adjust_box_for_original_image_size(b, width=image.size[0], height=image.size[1]))
            for b in model_bboxes
        ]
        processed.append({
            "phrase": pred_phrase,
            "bounding_boxes": {
                "model_coordinates": model_bboxes,
                "image_coordinates": image_bboxes,
            },
        })

    viz_path = None
    if processed:
        all_bboxes = []
        for p in processed:
            all_bboxes.extend(p["bounding_boxes"]["image_coordinates"])
        viz_path = _visualize(image, all_bboxes, phrase)
    else:
        metadata["analysis_status"] = "completed_no_finding"

    return {
        "output": {"predictions": processed, "visualization_path": viz_path, "raw_decoded": decoded_text},
        "metadata": metadata,
    }


@mcp.tool(
    name="xray_phrase_grounding",
    description=(
        "Ground a medical phrase in a frontal chest X-ray using MAIRA-2. "
        "Returns list of [x1, y1, x2, y2] bounding boxes (normalized + image-coordinate) "
        "and writes an overlay PNG to the artifact dir."
    ),
)
async def xray_phrase_grounding(
    image_path: Annotated[str, Field(description="Path to the frontal chest X-ray (JPG or PNG).")],
    phrase: Annotated[str, Field(description="Medical finding to locate, e.g. 'Pleural effusion'.")],
    max_new_tokens: Annotated[int, Field(description="Max decoder tokens.")] = 300,
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(image_path)
        return _run(abs_path, phrase, int(max_new_tokens))
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {
                "image_path": image_path,
                "analysis_status": "failed",
                "error_details": str(exc),
            },
        }
