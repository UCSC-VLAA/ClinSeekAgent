"""Image Tool Pool - MCP client for the medical-image MCP server.

Mirrors the structure of `ehr_tool_pool.py` (JSON-RPC 2.0 over HTTP, per-qid session
state, SSE or plain JSON response parsing). Kept deliberately small: this pool
just forwards calls to the image MCP server; all heavy lifting lives in
`src/mcp_image/tools/`.
"""
import asyncio
import json
from typing import Any, Dict

import httpx


class ImageToolPool:
    """HTTP client wrapping the medical-image MCP server."""

    def __init__(self, mcp_url: str = "http://127.0.0.1:5203/mcp"):
        self.mcp_url = mcp_url
        self.sessions: Dict[Any, Dict[str, Any]] = {}
        # Localhost MCP traffic only; no outbound proxy / env lookups.
        self.client = httpx.AsyncClient(timeout=180.0, trust_env=False)

    def _get_or_create_session(self, qid: Any) -> Dict[str, Any]:
        if qid not in self.sessions:
            self.sessions[qid] = {
                "mcp_session_id": None,
                "mcp_initialized": False,
                "mcp_init_lock": asyncio.Lock(),
            }
        return self.sessions[qid]

    async def _init_mcp_session(self, qid: Any) -> str:
        session = self._get_or_create_session(qid)
        if session["mcp_session_id"] and session["mcp_initialized"]:
            return session["mcp_session_id"]

        async with session["mcp_init_lock"]:
            if session["mcp_session_id"] and session["mcp_initialized"]:
                return session["mcp_session_id"]

            init_payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ClinSeekAgent-Image", "version": "1.0.0"},
                },
            }
            try:
                response = await self.client.post(
                    self.mcp_url,
                    json=init_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                response.raise_for_status()
                mcp_session_id = (
                    response.headers.get("mcp-session-id")
                    or response.headers.get("x-session-id", "default-session")
                )

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    init_result = None
                    for line in response.text.split("\n"):
                        if line.startswith("data: "):
                            init_result = json.loads(line[6:])
                            break
                    if init_result is None:
                        raise RuntimeError("MCP init: no data payload in SSE response")
                else:
                    init_result = response.json()
                if "error" in init_result:
                    raise RuntimeError(f"MCP init failed: {init_result['error']}")

                initialized_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                if mcp_session_id:
                    initialized_headers["mcp-session-id"] = mcp_session_id

                initialized_payload = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
                await self.client.post(self.mcp_url, json=initialized_payload,
                                        headers=initialized_headers)

                session["mcp_session_id"] = mcp_session_id
                session["mcp_initialized"] = True
                return mcp_session_id
            except Exception as exc:
                session["mcp_session_id"] = None
                session["mcp_initialized"] = False
                raise RuntimeError(f"Failed to initialize image MCP session: {exc}")

    async def init_session(self, qid: Any) -> dict:
        self._get_or_create_session(qid)
        await self._init_mcp_session(qid)
        return {"status": "ready", "transport": "http", "mcp_url": self.mcp_url}

    async def _call_mcp_tool(self, qid: Any, tool_name: str, tool_args: Dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": tool_args},
        }
        session = self._get_or_create_session(qid)
        await self._init_mcp_session(qid)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session["mcp_session_id"]:
            headers["mcp-session-id"] = session["mcp_session_id"]

        try:
            response = await self.client.post(self.mcp_url, json=payload, headers=headers)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result = None
                for line in response.text.split("\n"):
                    if line.startswith("data: "):
                        result = json.loads(line[6:])
                        break
                if result is None:
                    return "No data in SSE response"
            else:
                result = response.json()

            if "error" in result:
                err = result["error"]
                return f"MCP Error {err.get('code', '')}: {err.get('message', 'Unknown error')}"
            if "result" not in result:
                return f"Invalid MCP response: {result}"

            tool_result = result["result"]
            if isinstance(tool_result, dict):
                if tool_result.get("isError"):
                    return f"Tool Error: {tool_result.get('content', 'Unknown error')}"
                content = tool_result.get("content", tool_result)
                if isinstance(content, list):
                    texts = []
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            texts.append(item["text"])
                        elif isinstance(item, str):
                            texts.append(item)
                    return "\n".join(texts) if texts else str(content)
                return content
            return tool_result
        except httpx.HTTPStatusError as exc:
            return f"HTTP Error {exc.response.status_code}: {exc.response.text}"
        except Exception as exc:
            return f"Error calling image tool '{tool_name}': {exc}"

    async def call_tool(self, qid: Any, tool_name: str, tool_args: Dict[str, Any]) -> str:
        if qid not in self.sessions:
            await self.init_session(qid)

        if tool_name.startswith("image."):
            tool_name = tool_name[len("image."):]

        result = await self._call_mcp_tool(qid, tool_name, tool_args)
        if isinstance(result, (dict, list)):
            return json.dumps(result, indent=2, ensure_ascii=False)
        return str(result)

    async def cleanup(self, qid: Any):
        if qid in self.sessions:
            del self.sessions[qid]

    async def close(self):
        await self.client.aclose()
