"""Shared utilities for image-tool MCP handlers."""
import os
import uuid
from pathlib import Path
from typing import List


def split_roots(raw_value: str) -> List[str]:
    """Split BENCH_ROOT while preserving Windows drive prefixes."""
    roots: List[str] = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        is_windows_drive = (
            len(chunk) >= 3
            and chunk[1] == ":"
            and chunk[0].isalpha()
            and chunk[2] in ("\\", "/")
        )
        if is_windows_drive:
            roots.append(chunk)
        else:
            roots.extend(part for part in chunk.split(":") if part)
    return roots


def get_bench_roots() -> List[Path]:
    """Return the list of benchmark roots used to resolve relative image paths.

    `$BENCH_ROOT` may be a single path or a colon-/comma-separated list (so
    the EHRXQA and MedMod roots can be tried in order).
    """
    value = os.environ.get("BENCH_ROOT", "").strip()
    if not value:
        return [Path.cwd().resolve()]
    parts = split_roots(value)
    return [Path(p).resolve() for p in parts]


def get_bench_root() -> Path:
    """Compatibility shim: return the first bench root."""
    return get_bench_roots()[0]


def get_artifact_dir() -> Path:
    """Return the directory where generated artifacts (overlays, PNGs) are written.

    Default is `./tmp/mm_artifacts` relative to CWD; override via
    `$IMAGE_ARTIFACT_DIR` to place artifacts elsewhere.
    """
    value = os.environ.get(
        "IMAGE_ARTIFACT_DIR",
        "tmp/mm_artifacts",
    )
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def pick_tool_device(tool_tag: str):
    """Choose a torch.device for the given tool.

    Priority:
      1. `IMAGE_TOOL_DEVICE_<TAG>` (per-tool override).
      2. `IMAGE_TOOL_DEVICE`       (global override).
      3. auto: `cuda` if available, else `cpu`.
    """
    import torch
    key = f"IMAGE_TOOL_DEVICE_{tool_tag.upper()}"
    value = os.environ.get(key) or os.environ.get("IMAGE_TOOL_DEVICE")
    if value:
        return torch.device(value)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def resolve_image_path(image_path: str) -> str:
    """Resolve `image_path` to an absolute filesystem path.

    Accepts absolute paths, paths relative to any `$BENCH_ROOT` entry, or
    paths already on disk relative to the current working directory.
    """
    if not image_path:
        raise FileNotFoundError("Empty image path")
    candidate = Path(image_path)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)
    roots = get_bench_roots()
    for root in roots:
        rooted = root / image_path
        if rooted.exists():
            return str(rooted)
    if candidate.exists():
        return str(candidate.resolve())
    raise FileNotFoundError(
        f"Image not found at '{image_path}' "
        f"(bench_roots={[str(r) for r in roots]})"
    )


def new_artifact_path(suffix: str = ".png", prefix: str = "artifact_") -> Path:
    """Allocate a new unique artifact path under `$IMAGE_ARTIFACT_DIR`."""
    return get_artifact_dir() / f"{prefix}{uuid.uuid4().hex[:8]}{suffix}"
