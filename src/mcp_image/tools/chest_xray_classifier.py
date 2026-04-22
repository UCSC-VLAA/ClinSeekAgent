"""chest_xray_classifier tool (torchxrayvision DenseNet).

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/classification.py
(Apache 2.0, credits MedRAX).
"""
import os
import threading
from typing import Annotated, Any, Dict

from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import pick_tool_device, resolve_image_path

_MODEL = None
_MODEL_LOCK = threading.Lock()
_DEVICE = None
_TRANSFORM = None


def _get_device():
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = pick_tool_device("classifier")
    return _DEVICE


def _get_model():
    global _MODEL, _TRANSFORM
    if _MODEL is not None:
        return _MODEL, _TRANSFORM
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL, _TRANSFORM
        import torch  # noqa: F401
        import torchvision
        import torchxrayvision as xrv
        weights = os.environ.get("XRV_DENSENET_WEIGHTS", "densenet121-res224-all")
        model = xrv.models.DenseNet(weights=weights)
        model.eval()
        model = model.to(_get_device())
        _MODEL = model
        _TRANSFORM = torchvision.transforms.Compose([xrv.datasets.XRayCenterCrop()])
        return _MODEL, _TRANSFORM


def _classify(image_path: str) -> Dict[str, Any]:
    import skimage.io
    import torch
    import torchxrayvision as xrv

    model, transform = _get_model()
    img = skimage.io.imread(image_path)
    img = xrv.datasets.normalize(img, 255)
    if len(img.shape) > 2:
        img = img[:, :, 0]
    img = img[None, :, :]
    img = transform(img)
    img = torch.from_numpy(img).unsqueeze(0).to(_get_device())

    with torch.inference_mode():
        preds = model(img).cpu()[0]

    probs = dict(zip(xrv.datasets.default_pathologies, [float(x) for x in preds.numpy()]))
    return {
        "output": probs,
        "metadata": {
            "image_path": image_path,
            "analysis_status": "completed",
            "note": "Probabilities range 0..1; higher = more likely present.",
            "model": "torchxrayvision DenseNet (densenet121-res224-all)",
        },
    }


@mcp.tool(
    name="chest_xray_classifier",
    description=(
        "Analyze a chest X-ray image and return probability (0-1) for 18 pathologies: "
        "Atelectasis, Cardiomegaly, Consolidation, Edema, Effusion, Emphysema, "
        "Enlarged Cardiomediastinum, Fibrosis, Fracture, Hernia, Infiltration, Lung Lesion, "
        "Lung Opacity, Mass, Nodule, Pleural Thickening, Pneumonia, Pneumothorax."
    ),
)
async def chest_xray_classifier(
    image_path: Annotated[str, Field(description="Path to a chest X-ray image (JPG or PNG).")],
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(image_path)
        return _classify(abs_path)
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {"image_path": image_path, "analysis_status": "failed"},
        }
