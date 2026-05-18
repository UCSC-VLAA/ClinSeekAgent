"""Standalone HTTP MCP server exposing medical-image analysis tools.

Mirrors `src/run_mcp_server.py` so the existing pipeline can reach it via the
same JSON-RPC 2.0 transport. Tools are registered via import side effects in
`mcp_image.tools`.

Environment variables:
- BENCH_ROOT: root directory used to resolve relative image paths from a
  prepared benchmark tree.
- IMAGE_ARTIFACT_DIR: where generated overlays / processed images are saved.
- HF_HOME / TRANSFORMERS_CACHE: honored by transformers / torchxrayvision.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent
# Let `import mcp_image` work when the script is invoked directly.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_image.fastmcp_app import mcp  # noqa: E402
from mcp_image import tools  # noqa: F401, E402 (triggers tool registration)


def get_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EHR Image MCP Tool Server")
    parser.add_argument("--mode", type=str, default="http", choices=["studio", "http"])
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5203)
    parser.add_argument(
        "--bench-root",
        type=str,
        default=None,
        help=(
            "Override $BENCH_ROOT for this process. Used to resolve relative "
            "image paths from the benchmark manifests."
        ),
    )
    parser.add_argument(
        "--artifact-dir",
        type=str,
        default=None,
        help=(
            "Directory where generated overlays and processed images are saved. "
            "Defaults to ./tmp/mm_artifacts (relative to CWD)."
        ),
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Skip eager model loading at startup (fall back to lazy per-call load).",
    )
    return parser.parse_args()


def _warmup_models() -> None:
    """Eagerly load every GPU-backed model so the first tool call is not slow.

    Each loader respects its own `IMAGE_TOOL_DEVICE_*` env var, so warming them
    here also validates GPU pinning before the agent sends traffic.
    """
    from mcp_image.tools import (
        chest_xray_classifier,
        chest_xray_report_generator,
        chest_xray_segmentation,
        xray_phrase_grounding,
    )

    steps = (
        ("classifier", chest_xray_classifier._get_model),
        ("report_generator", chest_xray_report_generator._get_models),
        ("segmentation", chest_xray_segmentation._get_model),
        ("grounding", xray_phrase_grounding._get_model),
    )
    for tag, loader in steps:
        print(f"[warmup] loading {tag} ...", flush=True)
        try:
            loader()
            print(f"[warmup] {tag} ready", flush=True)
        except Exception as exc:
            print(f"[warmup] {tag} FAILED: {type(exc).__name__}: {exc}", flush=True)
            # Fall through — the tool will report the same error when called.


async def main() -> None:
    args = get_parser()
    if args.bench_root:
        os.environ["BENCH_ROOT"] = str(Path(args.bench_root).resolve())
    if args.artifact_dir:
        os.environ["IMAGE_ARTIFACT_DIR"] = str(Path(args.artifact_dir).resolve())

    if not args.skip_warmup:
        _warmup_models()

    if args.mode == "studio":
        mcp.run()
    else:
        await mcp.run_async(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    asyncio.run(main())
