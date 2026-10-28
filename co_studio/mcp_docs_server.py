"""MCP tools for an immutable allowlist of public Studio operation guides."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import stat

from .mcp_readonly import McpPolicyError, _json_copy, _object, _validate

DOCUMENTS = {"history": "history.md", "prompt-versions": "prompt-versions.md",
             "tool-safety": "tool-safety.md", "connectivity": "connectivity.md"}
DOC_ROOT = Path(__file__).resolve().parent / "knowledge_docs"
_COMMON = {"id": {"type": "string", "enum": list(DOCUMENTS)},
           "title": {"type": "string", "maxLength": 120},
           "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}
_DEFINITIONS = [
    {"name": "studio_search_docs", "description": "Search the shipped Studio operation guides; no user files, web, or RAG index.",
     "inputSchema": _object({"query": {"type": "string", "minLength": 1, "maxLength": 120},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 4}}, ["query"]),
     "outputSchema": _object({"source": {"const": "studio-public-guides"},
                              "documents": {"type": "array", "maxItems": 4, "items": _object(_COMMON, list(_COMMON))}},
                             ["source", "documents"])},
    {"name": "studio_read_doc", "description": "Read a shipped Studio guide by fixed document ID (history, prompt-versions, tool-safety, connectivity).",
     "inputSchema": _object({"document_id": {"type": "string", "enum": list(DOCUMENTS)}}, ["document_id"]),
     "outputSchema": _object({"source": {"const": "studio-public-guides"}, **_COMMON,
                              "text": {"type": "string", "maxLength": 8000}}, ["source", *_COMMON, "text"])},
]


def document_tool_definitions() -> list[dict]:
    from copy import deepcopy
    definitions = deepcopy(_DEFINITIONS)
    for item in definitions:
        item["annotations"] = {"readOnlyHint": True, "destructiveHint": False,
                               "idempotentHint": True, "openWorldHint": False}
    return definitions


def read_document(document_id: str) -> dict:
    if not isinstance(document_id, str) or document_id not in DOCUMENTS:
        raise McpPolicyError("Unknown document ID")
    path = DOC_ROOT / DOCUMENTS[document_id]
    if path.resolve() != path or DOC_ROOT.resolve() != DOC_ROOT:
        raise McpPolicyError("Linked guide paths are not supported")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise McpPolicyError("Guide is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(8001)
        if len(raw) > 8000:
            raise McpPolicyError("Guide exceeds 8 KiB")
        text = raw.decode("utf-8")
    finally:
        os.close(fd)
    return {"source": "studio-public-guides", "id": document_id,
            "title": text.splitlines()[0].lstrip("# ")[:120],
            "sha256": hashlib.sha256(raw).hexdigest(), "text": text}


def execute_document_tool(name: str, arguments: dict) -> dict:
    definition = next((item for item in _DEFINITIONS if item["name"] == name), None)
    if definition is None:
        raise McpPolicyError("Tool is not on the public guide allowlist")
    arguments = _json_copy(arguments, 4096, "Arguments")
    _validate(arguments, definition["inputSchema"], "Arguments")
    if name == "studio_read_doc":
        return read_document(arguments["document_id"])
    terms = arguments["query"].casefold().split()
    documents = [read_document(document_id) for document_id in DOCUMENTS]
    scored = [(sum(doc["text"].casefold().count(term) for term in terms), doc) for doc in documents]
    matches = [doc for score, doc in sorted(scored, key=lambda item: (-item[0], item[1]["id"])) if score > 0]
    return {"source": "studio-public-guides", "documents": [
        {key: doc[key] for key in _COMMON} for doc in matches[:int(arguments.get("limit", 4))]]}


async def main():
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool
    server = Server("studio-public-guides", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return [Tool(**definition) for definition in document_tool_definitions()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return execute_document_tool(name, arguments)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
