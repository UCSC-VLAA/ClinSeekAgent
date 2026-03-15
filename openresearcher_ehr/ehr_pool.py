"""
EHR Tool Pool - MCP client wrapper for EHR tools integration with OpenResearcher.

This module provides a session-based interface to the EHR MCP server,
allowing OpenResearcher agents to query patient EHR databases.
"""
import asyncio
import json
from typing import Dict, Any, Optional
import httpx


class EHRToolPool:
    """Manages HTTP connections to EHR MCP server and tool execution."""

    def __init__(self, mcp_url: str = "http://127.0.0.1:5002/mcp"):
        """
        Initialize EHR tool pool with HTTP transport.

        Args:
            mcp_url: HTTP URL for EHR MCP server (e.g., 'http://127.0.0.1:5002/mcp')
        """
        self.mcp_url = mcp_url
        self.sessions: Dict[Any, Dict[str, Any]] = {}
        self.client = httpx.AsyncClient(timeout=60.0)
        self.mcp_session_id: Optional[str] = None

    async def _init_mcp_session(self) -> str:
        """Initialize MCP session if not already done."""
        if self.mcp_session_id:
            return self.mcp_session_id

        # Send MCP initialize request
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "OpenResearcher-EHR",
                    "version": "1.0.0"
                }
            }
        }

        try:
            response = await self.client.post(
                self.mcp_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"
                }
            )
            response.raise_for_status()

            # Extract session ID from response headers
            self.mcp_session_id = response.headers.get("mcp-session-id") or response.headers.get("x-session-id", "default-session")

            # Parse SSE response if content-type is text/event-stream
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                # Parse SSE format: "event: message\ndata: {json}\n\n"
                text = response.text
                for line in text.split("\n"):
                    if line.startswith("data: "):
                        data_json = line[6:]  # Remove "data: " prefix
                        result = json.loads(data_json)
                        if "error" in result:
                            raise Exception(f"MCP initialization failed: {result['error']}")
                        return self.mcp_session_id
            else:
                result = response.json()
                if "error" in result:
                    raise Exception(f"MCP initialization failed: {result['error']}")

            return self.mcp_session_id

        except Exception as e:
            raise Exception(f"Failed to initialize MCP session: {str(e)}")

    async def init_session(self, qid: Any) -> dict:
        """Initialize session for a query."""
        if qid not in self.sessions:
            # Initialize MCP session if needed
            await self._init_mcp_session()

            self.sessions[qid] = {
                "status": "initialized",
                "loaded_ehrs": set()
            }
        return {"status": "ready", "transport": "http", "mcp_url": self.mcp_url}

    async def _call_mcp_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> Any:
        """
        Call MCP server tool via JSON-RPC 2.0 protocol.

        Args:
            tool_name: Name of the MCP tool to call
            tool_args: Arguments to pass to the tool

        Returns:
            Tool execution result
        """
        # FastMCP uses JSON-RPC 2.0 protocol
        # https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/transports/#http-with-sse
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args
            }
        }

        try:
            # Ensure MCP session is initialized
            await self._init_mcp_session()

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            }
            if self.mcp_session_id:
                headers["mcp-session-id"] = self.mcp_session_id

            response = await self.client.post(
                self.mcp_url,  # Just the base MCP URL
                json=payload,
                headers=headers
            )
            response.raise_for_status()

            # Parse response based on content type
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                # Parse SSE format
                text = response.text
                for line in text.split("\n"):
                    if line.startswith("data: "):
                        data_json = line[6:]
                        result = json.loads(data_json)
                        break
                else:
                    return f"No data in SSE response"
            else:
                result = response.json()

            # Extract from JSON-RPC response
            # JSON-RPC returns: {"jsonrpc": "2.0", "id": 1, "result": {...}} or {"error": {...}}
            if "error" in result:
                error = result["error"]
                return f"MCP Error {error.get('code', '')}: {error.get('message', 'Unknown error')}"

            if "result" not in result:
                return f"Invalid MCP response: {result}"

            tool_result = result["result"]

            # MCP tool result format: {"content": [...], "isError": bool}
            if isinstance(tool_result, dict):
                if tool_result.get("isError"):
                    return f"Tool Error: {tool_result.get('content', 'Unknown error')}"

                content = tool_result.get("content", tool_result)
                # If content is a list of text items, extract text
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

        except httpx.HTTPStatusError as e:
            return f"HTTP Error {e.response.status_code}: {e.response.text}"
        except Exception as e:
            return f"Error calling MCP tool '{tool_name}': {str(e)}"

    async def call_tool(self, qid: Any, tool_name: str, tool_args: Dict[str, Any]) -> str:
        """
        Execute an EHR tool via MCP server.

        Args:
            qid: Query ID (session identifier)
            tool_name: Tool name (can have 'ehr.' prefix)
            tool_args: Tool arguments

        Returns:
            Tool result as string
        """
        if qid not in self.sessions:
            await self.init_session(qid)

        # Remove 'ehr.' prefix if present
        if tool_name.startswith("ehr."):
            tool_name = tool_name[4:]

        # Track loaded EHRs
        if tool_name == "load_ehr" and "subject_id" in tool_args:
            self.sessions[qid]["loaded_ehrs"].add(tool_args["subject_id"])

        try:
            # Call MCP tool via HTTP
            result = await self._call_mcp_tool(tool_name, tool_args)

            # Format result as string
            if isinstance(result, dict):
                return json.dumps(result, indent=2, ensure_ascii=False)
            elif isinstance(result, list):
                return json.dumps(result, indent=2, ensure_ascii=False)
            else:
                return str(result)

        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    async def cleanup(self, qid: Any):
        """Cleanup session (remove from tracking)."""
        if qid in self.sessions:
            del self.sessions[qid]

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
