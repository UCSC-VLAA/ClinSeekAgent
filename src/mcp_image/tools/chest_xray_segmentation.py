"""chest_xray_segmentation tool (torchxrayvision PSPNet).

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/segmentation.py
(Apache 2.0, credits MedRAX).
"""
import os
import threading
import traceback
from typing import Annotated, Any, Dict, List, Optional, Tuple

import numpy as np
from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import new_artifact_path, resolve_image_path


ORGAN_MAP: Dict[str, int] = {
    "Left Clavicle": 0,
    "Right Clavicle": 1,
    "Left Scapula": 2,
    "Right Scapula": 3,
    "Left Lung": 4,
    "Right Lung": 5,
    "Left Hilus Pulmonis": 6,
    "Right Hilus Pulmonis": 7,
    "Heart": 8,
    "Aorta": 9,
    "Facies Diaphragmatica": 10,
    "Mediastinum": 11,
    "Weasand": 12,
    "Spine": 13,
}
PIXEL_SPACING_MM = 0.2

_MODEL = None
_MODEL_LOCK = threading.Lock()
_TRANSFORM = None
_DEVICE = None


def _get_device():
    global _DEVICE
    if _DEVICE is None:
        from mcp_image.tools.base import pick_tool_device
        _DEVICE = pick_tool_device("segmentation")
    return _DEVICE


def _get_model():
    global _MODEL, _TRANSFORM
    if _MODEL is not None:
        return _MODEL, _TRANSFORM
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL, _TRANSFORM
        import torchvision
        import torchxrayvision as xrv
        model = xrv.baseline_models.chestx_det.PSPNet()
        model = model.to(_get_device())
        model.eval()
        _MODEL = model
        _TRANSFORM = torchvision.transforms.Compose(
            [xrv.datasets.XRayCenterCrop(), xrv.datasets.XRayResizer(512)]
        )
        return _MODEL, _TRANSFORM


def _align_mask(mask: np.ndarray, original_shape: Tuple[int, int]) -> np.ndarray:
    import skimage.transform
    orig_h, orig_w = original_shape
    crop = min(orig_h, orig_w)
    top = (orig_h - crop) // 2
    left = (orig_w - crop) // 2
    resized = skimage.transform.resize(mask, (crop, crop), order=0,
                                       preserve_range=True, anti_aliasing=False)
    full = np.zeros(original_shape)
    full[top:top + crop, left:left + crop] = resized
    return full


def _compute_metrics(mask: np.ndarray, original_img: np.ndarray, confidence: float) -> Optional[Dict[str, Any]]:
    import skimage.measure
    if mask.shape != original_img.shape:
        mask = _align_mask(mask, original_img.shape)

    props = skimage.measure.regionprops(mask.astype(int))
    if not props:
        return None
    props = props[0]

    area_cm2 = float(mask.sum()) * (PIXEL_SPACING_MM / 10) ** 2
    img_h, img_w = mask.shape
    cy, cx = props.centroid
    relative = {
        "top": float(cy / img_h),
        "left": float(cx / img_w),
        "center_dist": float(np.sqrt((cy / img_h - 0.5) ** 2 + (cx / img_w - 0.5) ** 2)),
    }
    organ_pixels = original_img[mask > 0]
    mean_intensity = float(organ_pixels.mean()) if len(organ_pixels) else 0.0
    std_intensity = float(organ_pixels.std()) if len(organ_pixels) else 0.0
    bbox = tuple(int(v) for v in props.bbox)
    width = int(bbox[3] - bbox[1])
    height = int(bbox[2] - bbox[0])
    return {
        "area_pixels": int(mask.sum()),
        "area_cm2": area_cm2,
        "centroid": [float(cy), float(cx)],
        "bbox": list(bbox),
        "width": width,
        "height": height,
        "aspect_ratio": float(height / max(1, width)),
        "relative_position": relative,
        "mean_intensity": mean_intensity,
        "std_intensity": std_intensity,
        "confidence_score": float(confidence),
    }


def _save_overlay(original_img: np.ndarray, pred_masks, organ_indices) -> str:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    out_path = new_artifact_path(suffix=".png", prefix="segmentation_")
    plt.figure(figsize=(10, 10))
    plt.imshow(original_img, cmap="gray",
               extent=[0, original_img.shape[1], original_img.shape[0], 0])
    colors = plt.cm.rainbow(np.linspace(0, 1, len(organ_indices)))
    inv_map = {v: k for k, v in ORGAN_MAP.items()}
    for organ_idx, color in zip(organ_indices, colors):
        mask = pred_masks[0, organ_idx].cpu().numpy()
        if mask.sum() == 0:
            continue
        if mask.shape != original_img.shape:
            mask = _align_mask(mask, original_img.shape)
        colored = np.zeros((*original_img.shape, 4))
        colored[mask > 0] = (*color[:3], 0.3)
        plt.imshow(colored, extent=[0, original_img.shape[1], original_img.shape[0], 0])
        plt.plot([], [], color=color, label=inv_map[organ_idx], linewidth=3)
    plt.title("Segmentation Overlay")
    plt.legend(bbox_to_anchor=(1.05, 1), loc="upper left")
    plt.axis("off")
    plt.savefig(out_path, bbox_inches="tight", dpi=200)
    plt.close()
    return str(out_path)


def _run(image_path: str, organs: Optional[List[str]]) -> Dict[str, Any]:
    import skimage.io
    import torch
    import torchxrayvision as xrv

    model, transform = _get_model()

    if organs:
        organs = [o.strip() for o in organs]
        invalid = [o for o in organs if o not in ORGAN_MAP]
        if invalid:
            raise ValueError(f"Invalid organs specified: {invalid}")
        organ_indices = [ORGAN_MAP[o] for o in organs]
    else:
        organ_indices = list(ORGAN_MAP.values())
        organs = list(ORGAN_MAP.keys())

    original_img = skimage.io.imread(image_path)
    if len(original_img.shape) > 2:
        original_img = original_img[:, :, 0]

    img = xrv.datasets.normalize(original_img, 255)
    img = img[None, ...]
    img = transform(img)
    img = torch.from_numpy(img).to(_get_device())

    with torch.no_grad():
        pred = model(img)
    pred_probs = torch.sigmoid(pred)
    pred_masks = (pred_probs > 0.5).float()

    overlay_path = _save_overlay(original_img, pred_masks, organ_indices)

    metrics: Dict[str, Any] = {}
    for idx, name in zip(organ_indices, organs):
        mask = pred_masks[0, idx].cpu().numpy()
        if mask.sum() > 0:
            m = _compute_metrics(mask, original_img, float(pred_probs[0, idx].mean().cpu()))
            if m is not None:
                metrics[name] = m

    return {
        "output": {
            "segmentation_image_path": overlay_path,
            "metrics": metrics,
        },
        "metadata": {
            "image_path": image_path,
            "segmentation_image_path": overlay_path,
            "original_size": list(original_img.shape),
            "model_size": list(img.shape[-2:]),
            "pixel_spacing_mm": PIXEL_SPACING_MM,
            "requested_organs": organs,
            "processed_organs": list(metrics.keys()),
            "analysis_status": "completed",
            "note": "Area values are only accurate when input has proper pixel spacing (DICOM).",
        },
    }


@mcp.tool(
    name="chest_xray_segmentation",
    description=(
        "Segment 14 anatomical structures in a chest X-ray using torchxrayvision PSPNet. "
        "Available organs: Left/Right Clavicle, Left/Right Scapula, Left/Right Lung, "
        "Left/Right Hilus Pulmonis, Heart, Aorta, Facies Diaphragmatica, Mediastinum, "
        "Weasand, Spine. Returns per-organ metrics and writes an overlay PNG."
    ),
)
async def chest_xray_segmentation(
    image_path: Annotated[str, Field(description="Path to the chest X-ray (JPG or PNG).")],
    organs: Annotated[Optional[List[str]], Field(description="Subset of organ names to segment.")] = None,
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(image_path)
        return _run(abs_path, organs)
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {
                "image_path": image_path,
                "analysis_status": "failed",
                "error_traceback": traceback.format_exc(),
            },
        }
