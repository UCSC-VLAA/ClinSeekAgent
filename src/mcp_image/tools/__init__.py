"""Register FastMCP tools for medical image analysis.

Tools ported from the Meissa repo
(https://github.com/Schuture/Meissa/tree/main/environments/continuous_tool_calling/tools,
Apache 2.0, credits MedRAX). Each submodule attaches one @mcp.tool to the
shared FastMCP instance via import side-effects.
"""
from . import image_visualizer  # noqa: F401
from . import dicom_processor  # noqa: F401
from . import chest_xray_classifier  # noqa: F401
from . import chest_xray_report_generator  # noqa: F401
from . import xray_phrase_grounding  # noqa: F401
from . import chest_xray_segmentation  # noqa: F401
