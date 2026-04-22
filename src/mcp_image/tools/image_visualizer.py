"""image_visualizer tool.

Ported from
https://github.com/Schuture/Meissa/blob/main/environments/continuous_tool_calling/tools/utils.py
(Apache 2.0, credits MedRAX).

Renders a copy of the image with optional title / description to
`$IMAGE_ARTIFACT_DIR` and returns the artifact path plus metadata.
"""
from typing import Annotated, Any, Dict, Optional, Tuple

from pydantic import Field

from mcp_image.fastmcp_app import mcp
from mcp_image.tools.base import new_artifact_path, resolve_image_path


def _render(image_path: str, title: Optional[str], description: Optional[str],
            figsize: Tuple[int, int], cmap: str) -> str:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    import skimage.io

    out_path = new_artifact_path(suffix=".png", prefix="visualizer_")
    plt.figure(figsize=figsize)

    img = skimage.io.imread(image_path)
    if len(img.shape) > 2 and cmap != "rgb":
        img = img[..., 0]

    plt.imshow(img, cmap=None if cmap == "rgb" else cmap)
    plt.axis("off")
    if title:
        plt.title(title, pad=15, fontsize=12)
    if description:
        plt.figtext(0.5, 0.01, description, wrap=True,
                    horizontalalignment="center", fontsize=10)
    plt.subplots_adjust(top=0.95, bottom=0.05, left=0.05, right=0.95)
    plt.savefig(out_path, bbox_inches="tight", dpi=150)
    plt.close()
    return str(out_path)


@mcp.tool(
    name="image_visualizer",
    description=(
        "Render an image with optional title / description to the artifact dir. "
        "Input: Path to image file (JPG or PNG) and optional display parameters. "
        "Output: Dict with the generated image path and metadata."
    ),
)
async def image_visualizer(
    image_path: Annotated[str, Field(description="Path to the image file (JPG or PNG).")],
    title: Annotated[Optional[str], Field(description="Optional title above the image.")] = None,
    description: Annotated[Optional[str], Field(description="Optional caption below the image.")] = None,
    figsize: Annotated[Optional[Tuple[int, int]], Field(description="Figure size (w, h) in inches.")] = (10, 10),
    cmap: Annotated[Optional[str], Field(description="Matplotlib colormap or 'rgb'.")] = "rgb",
) -> Dict[str, Any]:
    try:
        abs_path = resolve_image_path(image_path)
        figsize = tuple(figsize) if figsize else (10, 10)
        viz_path = _render(abs_path, title, description, figsize, cmap or "rgb")
        return {
            "output": {"image_path": viz_path},
            "metadata": {
                "original_image_path": abs_path,
                "title": bool(title),
                "description": bool(description),
                "figsize": list(figsize),
                "cmap": cmap,
                "analysis_status": "completed",
            },
        }
    except Exception as exc:
        return {
            "output": {"error": str(exc)},
            "metadata": {
                "image_path": image_path,
                "analysis_status": "failed",
            },
        }
