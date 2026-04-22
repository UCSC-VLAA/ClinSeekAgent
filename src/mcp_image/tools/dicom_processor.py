"""dicom_processor tool.

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/dicom.py
(Apache 2.0, credits MedRAX).

Converts DICOM → PNG + metadata; result image goes to `$IMAGE_ARTIFACT_DIR`.
"""
from typing import Annotated, Any, Dict, Optional

import numpy as np
from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import new_artifact_path, resolve_image_path


def _apply_windowing(img: np.ndarray, center: float, width: float) -> np.ndarray:
    img_min = center - width // 2
    img_max = center + width // 2
    img = np.clip(img, img_min, img_max)
    img = ((img - img_min) / max(width, 1e-6) * 255).astype(np.uint8)
    return img


def _process(dicom_path: str,
             window_center: Optional[float],
             window_width: Optional[float]) -> Dict[str, Any]:
    import pydicom
    from PIL import Image

    dcm = pydicom.dcmread(dicom_path)
    img = dcm.pixel_array.astype(float)

    if window_center is None and hasattr(dcm, "WindowCenter"):
        wc = dcm.WindowCenter
        window_center = wc[0] if isinstance(wc, (list, tuple)) else wc
    if window_width is None and hasattr(dcm, "WindowWidth"):
        ww = dcm.WindowWidth
        window_width = ww[0] if isinstance(ww, (list, tuple)) else ww

    if hasattr(dcm, "RescaleSlope") and hasattr(dcm, "RescaleIntercept"):
        img = img * float(dcm.RescaleSlope) + float(dcm.RescaleIntercept)

    if window_center is not None and window_width is not None:
        img = _apply_windowing(img, float(window_center), float(window_width))
    else:
        span = img.max() - img.min() or 1.0
        img = ((img - img.min()) / span * 255).astype(np.uint8)

    out_path = new_artifact_path(suffix=".png", prefix="dicom_")
    Image.fromarray(img).save(out_path)

    metadata = {
        "PatientID": getattr(dcm, "PatientID", None),
        "StudyDate": getattr(dcm, "StudyDate", None),
        "Modality": getattr(dcm, "Modality", None),
        "PixelSpacing": list(dcm.PixelSpacing) if hasattr(dcm, "PixelSpacing") else None,
        "WindowCenter": window_center,
        "WindowWidth": window_width,
        "BitsStored": getattr(dcm, "BitsStored", None),
        "original_path": dicom_path,
        "output_path": str(out_path),
        "analysis_status": "completed",
    }
    return {"output": {"image_path": str(out_path)}, "metadata": metadata}


@mcp.tool(
    name="dicom_processor",
    description=(
        "Process a DICOM medical image and convert it to a PNG. Handles rescale "
        "slope/intercept and optional window-center / window-width adjustment. "
        "Input: DICOM path and optional window parameters. "
        "Output: Dict with the processed image path and DICOM metadata."
    ),
)
async def dicom_processor(
    dicom_path: Annotated[str, Field(description="Path to the DICOM file.")],
    window_center: Annotated[Optional[float], Field(description="Window center for contrast.")] = None,
    window_width: Annotated[Optional[float], Field(description="Window width for contrast.")] = None,
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(dicom_path)
        return _process(abs_path, window_center, window_width)
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {
                "dicom_path": dicom_path,
                "analysis_status": "failed",
                "error_details": str(exc),
            },
        }
