"""pytest configuration: make the repository's flat module layout importable.

The runners import their siblings as top-level modules (`import run_text`,
`from agentlite...`), exactly as the launch scripts arrange through
PYTHONPATH=clinseekagent:src. Mirror that here so `python -m pytest tests`
works from the repository root without extra environment setup.

Which environment runs which tests: everything runs in the agent environment
(venvs/requirements/bedrock_agent.txt) except tests/test_mcp_temporal_boundaries.py,
which imports the MCP server stack (fastmcp) and runs in the EHR MCP
environment (venvs/requirements/mcp_ehr.txt); it is skipped automatically where
fastmcp is not installed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT / "scripts" / "data_build", ROOT / "src",
             ROOT / "clinseekagent", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
