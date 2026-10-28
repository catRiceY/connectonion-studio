"""Offline MCP protocol fixture, not a production knowledge source or RAG server.

Only fixed in-package explanatory records are exposed; no filesystem, network,
process execution, credentials, real Agent records, or model calls are used.
"""
import asyncio
from copy import deepcopy

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from .mcp_readonly import tool_definitions, validate_arguments


_GUIDES = {
    "history": {"id": "history", "title": "History and execution records",
                "text": "History shows saved sessions, turns, and execution events. Missing historical facts remain unknown; a tool event is not a separate Agent run."},
    "prompt-versions": {"id": "prompt-versions", "title": "Base prompt versions",
                        "text": "Saving a base prompt creates a revision. Restart applies it to a new Agent process. Existing sessions may retain their earlier system instructions, so launch and actual prompt versions are tracked separately."},
    "mcp-scope": {"id": "mcp-scope", "title": "Read-only MCP step 1 scope",
                  "text": "This fixture validates initialize, tool discovery, and structured stdio tool calls. Local allowlists and schemas enforce the boundary; readOnlyHint is not permission. It is not connected to Agents, approvals, real documents, or RAG."},
}


def create_server() -> Server:
    server = Server("studio-readonly-fixture", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return [Tool(**definition) for definition in tool_definitions()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        arguments = validate_arguments(name, arguments)
        if name == "studio_list_guides":
            query = arguments.get("query", "").casefold()
            matches = [guide for guide in _GUIDES.values()
                       if query in f'{guide["title"]} {guide["text"]}'.casefold()]
            return {"source": "bundled-fixture", "guides": [
                {"id": guide["id"], "title": guide["title"]}
                for guide in matches[:arguments.get("limit", 3)]]}
        return {"source": "bundled-fixture", **deepcopy(_GUIDES[arguments["guide_id"]])}

    return server


async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
