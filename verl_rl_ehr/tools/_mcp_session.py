"""Process-global EHRToolPool accessor.

Each Ray rollout worker imports this module once; the pool lives for the
lifetime of the worker. MCP sessions are keyed by `agent_data.request_id`
inside `EHRToolPool`, so tool calls within the same rollout reuse the same
`load_ehr` state while different rollouts get isolated sessions.

Env:
  EHR_MCP_URL   default http://127.0.0.1:5103/mcp
"""
import os
import sys
from pathlib import Path
from typing import Optional

# Allow `import openresearcher_ehr.ehr_pool` without installing the EHR harness
# as a package.
_PROJECT_ROOT = Path("/fsx-shared/juncheng/EHR")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from openresearcher_ehr.ehr_pool import EHRToolPool  # noqa: E402


_POOL: Optional[EHRToolPool] = None


def get_pool() -> EHRToolPool:
    global _POOL
    if _POOL is None:
        _POOL = EHRToolPool(mcp_url=os.environ.get("EHR_MCP_URL", "http://127.0.0.1:5103/mcp"))
    return _POOL
