"""Protocol smoke test: list tools through the MCP client.

Offline and in-process. Confirms the server built on MCP SDK 2.x still
advertises the latest protocol revision and returns every registered tool.
"""
import asyncio
import json

from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION
from mcp.types.version import LATEST_HANDSHAKE_VERSION

import server


def _listed_names(result) -> set[str]:
    names = {tool.name for tool in result.tools}
    assert names, "tools/list returned no tools"
    assert "pennylane_list_companies" in names
    assert "pennylane_whoami" in names
    assert "pennylane_firm_list_companies" in names
    for tool in result.tools:
        assert tool.input_schema.get("type") == "object", tool.name
    return names


def test_list_tools_on_latest_protocol(monkeypatch):
    """Modern clients negotiate 2026-07-28 and can list and call a tool."""
    monkeypatch.setattr(server, "COMPANIES", {"acme": "test-token"})
    monkeypatch.setattr(server, "DEFAULT_COMPANY", None)

    async def _run():
        async with Client(server.mcp) as client:
            assert client.protocol_version == LATEST_PROTOCOL_VERSION
            listed = await client.list_tools()
            names = _listed_names(listed)
            whoami = next(tool for tool in listed.tools if tool.name == "pennylane_whoami")
            assert set(whoami.input_schema["properties"]) == {"params"}
            assert "company" in whoami.input_schema["$defs"]["CompanyOnly"]["properties"]
            called = await client.call_tool("pennylane_list_companies", {})
            text = called.content[0].text
            assert json.loads(text)["companies"] == ["acme"]
            return names

    names = asyncio.run(_run())
    registered = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert names == registered


def test_legacy_handshake_lists_tools():
    """Earlier clients still complete initialize at the latest handshake revision."""

    async def _run():
        async with Client(server.mcp, mode="legacy") as client:
            assert client.protocol_version == LATEST_HANDSHAKE_VERSION
            return _listed_names(await client.list_tools())

    asyncio.run(_run())
