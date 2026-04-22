"""chest_xray_report_generator tool (CheXpert/MIMIC-CXR ViT-BERT).

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/report_generation.py
(Apache 2.0, credits MedRAX).
"""
import os
import threading
from typing import Annotated, Any, Dict

from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import pick_tool_device, resolve_image_path


_MODELS = None
_MODEL_LOCK = threading.Lock()
_DEVICE = None


def _get_device():
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = pick_tool_device("report_generator")
    return _DEVICE


def _get_models():
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    with _MODEL_LOCK:
        if _MODELS is not None:
            return _MODELS
        from transformers import (
            BertTokenizer,
            ViTImageProcessor,
            VisionEncoderDecoderModel,
        )
        cache_dir = os.environ.get("HF_CACHE_DIR") or os.environ.get("TRANSFORMERS_CACHE")
        dev = _get_device()

        findings_id = "IAMJB/chexpert-mimic-cxr-findings-baseline"
        impression_id = "IAMJB/chexpert-mimic-cxr-impression-baseline"
        f_model = VisionEncoderDecoderModel.from_pretrained(findings_id, cache_dir=cache_dir).eval().to(dev)
        f_tokenizer = BertTokenizer.from_pretrained(findings_id, cache_dir=cache_dir)
        f_proc = ViTImageProcessor.from_pretrained(findings_id, cache_dir=cache_dir)

        i_model = VisionEncoderDecoderModel.from_pretrained(impression_id, cache_dir=cache_dir).eval().to(dev)
        i_tokenizer = BertTokenizer.from_pretrained(impression_id, cache_dir=cache_dir)
        i_proc = ViTImageProcessor.from_pretrained(impression_id, cache_dir=cache_dir)

        _MODELS = {
            "findings": (f_model, f_tokenizer, f_proc),
            "impression": (i_model, i_tokenizer, i_proc),
            "generation_args": {
                "num_return_sequences": 1,
                "max_length": 128,
                "use_cache": True,
            },
        }
        return _MODELS


def _prepare_pixels(image_path: str, processor, model):
    import torch
    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    pixels = processor(image, return_tensors="pt").pixel_values
    expected = model.config.encoder.image_size
    actual = pixels.shape[-1]
    if expected != actual:
        pixels = torch.nn.functional.interpolate(
            pixels, size=(expected, expected), mode="bilinear", align_corners=False
        )
    return pixels.to(_get_device())


def _generate(pixels, model, tokenizer, generation_args):
    import torch  # noqa: F401
    from transformers import GenerationConfig

    cfg = GenerationConfig(
        **{
            **generation_args,
            "bos_token_id": model.config.bos_token_id,
            "eos_token_id": model.config.eos_token_id,
            "pad_token_id": model.config.pad_token_id,
            "decoder_start_token_id": tokenizer.cls_token_id,
        }
    )
    ids = model.generate(pixels, generation_config=cfg)
    return tokenizer.batch_decode(ids, skip_special_tokens=True)[0]


def _run(image_path: str) -> Dict[str, Any]:
    import torch

    models = _get_models()
    f_model, f_tokenizer, f_proc = models["findings"]
    i_model, i_tokenizer, i_proc = models["impression"]

    with torch.inference_mode():
        findings_pixels = _prepare_pixels(image_path, f_proc, f_model)
        findings = _generate(findings_pixels, f_model, f_tokenizer, models["generation_args"])

        impression_pixels = _prepare_pixels(image_path, i_proc, i_model)
        impression = _generate(impression_pixels, i_model, i_tokenizer, models["generation_args"])

    report = (
        "CHEST X-RAY REPORT\n\n"
        f"FINDINGS:\n{findings}\n\n"
        f"IMPRESSION:\n{impression}"
    )
    return {
        "output": {
            "report": report,
            "findings": findings,
            "impression": impression,
        },
        "metadata": {
            "image_path": image_path,
            "analysis_status": "completed",
            "sections_generated": ["findings", "impression"],
        },
    }


@mcp.tool(
    name="chest_xray_report_generator",
    description=(
        "Generate a structured radiology report (FINDINGS + IMPRESSION) for a chest X-ray "
        "using two ViT-BERT models trained on CheXpert / MIMIC-CXR."
    ),
)
async def chest_xray_report_generator(
    image_path: Annotated[str, Field(description="Path to the chest X-ray image (JPG or PNG).")],
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(image_path)
        return _run(abs_path)
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {
                "image_path": image_path,
                "analysis_status": "failed",
                "error": str(exc),
            },
        }
