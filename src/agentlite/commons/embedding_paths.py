"""Embedding model path resolution: use shared path if exists, else local models/Embeddings."""

import os

_EMBEDDINGS_BASE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "models", "Embeddings")

BGE_M3_DEFAULT = "/sfs/data/ShareModels/Embeddings/bge-m3"
BGE_M3_LOCAL = os.path.join(_EMBEDDINGS_BASE, "bge-m3")


def get_bge_m3_path() -> str:
    """Return bge-m3 path: shared if exists, else local."""
    return BGE_M3_DEFAULT if os.path.exists(BGE_M3_DEFAULT) else BGE_M3_LOCAL


def resolve_bge_m3_path(path: str = None) -> str:
    """Resolve bge-m3 path: if None or default path doesn't exist, use get_bge_m3_path()."""
    if path is None:
        return get_bge_m3_path()
    if path == BGE_M3_DEFAULT and not os.path.exists(path):
        return get_bge_m3_path()
    return path
