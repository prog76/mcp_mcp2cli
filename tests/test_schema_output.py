#!/usr/bin/env python3
"""Tests for mcp2cli.client schema formatting + tool-list capture.

Covers the outputSchema passthrough for ``describe``:
  - Tools advertised WITH an outputSchema (full-schema compounds like /mcp/full)
    surface it in the describe output.
  - Tools whose outputSchema was stripped at the proxy (browser compound,
    ``schema: minimal`` -> MountedServer omits it from tools/list) do NOT
    surface it. The server-side ``schema: minimal`` gate is the single place
    that controls this; here we only verify the library reflects tools/list
    faithfully.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp2cli import client


class ToolFake:
    """Minimal stand-in for the MCP SDK Tool object."""

    def __init__(self, name, input_schema=None, output_schema=None, description=""):
        self.name = name
        self.description = description
        self.inputSchema = input_schema
        self.outputSchema = output_schema


class _FakeHttpCM:
    """Stand-in for the object returned by streamablehttp_client(endpoint)."""

    async def __aenter__(self):
        return (MagicMock(), MagicMock(), None)

    async def __aexit__(self, *exc):
        return False


class TestFormatToolSchema:
    def test_emits_output_schema_when_present(self):
        """A tool advertised with an outputSchema shows it in describe."""
        tool = {
            "name": "gitlab_list_group_projects",
            "description": "List projects",
            "inputSchema": {"type": "object", "properties": {"group_path": {"type": "string"}}},
            "outputSchema": {"type": "object", "properties": {"projects": {"type": "array"}}},
        }
        out = client.format_tool_schema(tool)
        assert '"output"' in out
        assert '"projects"' in out

    def test_omits_output_schema_when_absent(self):
        """When tools/list has no outputSchema (stripped for schema: minimal),
        describe output must not contain an output field."""
        tool = {
            "name": "gitlab_list_group_projects",
            "description": "List projects",
            "inputSchema": {"type": "object", "properties": {"group_path": {"type": "string"}}},
        }
        out = client.format_tool_schema(tool)
        assert '"output"' not in out

    def test_output_schema_none_is_omitted(self):
        """An explicit outputSchema: None behaves like absent."""
        tool = {
            "name": "t",
            "description": "",
            "inputSchema": None,
            "outputSchema": None,
        }
        out = client.format_tool_schema(tool)
        assert '"output"' not in out


class TestFetchToolListLive:
    @pytest.mark.asyncio
    async def test_captures_output_schema(self, monkeypatch):
        """fetch captures outputSchema so describe can reflect it (full schema)."""
        session = AsyncMock()
        session.initialize = AsyncMock()
        session.list_tools = AsyncMock(
            return_value=MagicMock(
                tools=[
                    ToolFake(
                        name="gitlab_list_group_projects",
                        input_schema={"type": "object"},
                        output_schema={"type": "object", "properties": {"projects": {"type": "array"}}},
                        description="List projects",
                    )
                ]
            )
        )

        def fake_http_client(endpoint):
            return _FakeHttpCM()

        class FakeClientSession:
            def __init__(self, *a, **kw):
                self._s = session

            async def __aenter__(self):
                return self._s

            async def __aexit__(self, *exc):
                return False

        orig_client = client.streamablehttp_client
        orig_session = client.ClientSession
        client.streamablehttp_client = fake_http_client
        client.ClientSession = FakeClientSession
        try:
            tools = await client._fetch_tool_list_live("http://fake/mcp/full")
        finally:
            client.streamablehttp_client = orig_client
            client.ClientSession = orig_session

        assert len(tools) == 1
        assert tools[0]["outputSchema"] == {"type": "object", "properties": {"projects": {"type": "array"}}}
        assert tools[0]["inputSchema"] == {"type": "object"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])