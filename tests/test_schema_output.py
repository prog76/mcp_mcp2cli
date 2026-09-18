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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp2cli import client


class ToolFake:
    """Minimal stand-in for the MCP SDK Tool object."""

    def __init__(self, name, input_schema=None, output_schema=None, description=""):
        self.name = name
        self.description = description
        self.inputSchema = input_schema
        self.outputSchema = output_schema


class _FakeResponse:
    """Fake httpx.Response that returns tool data."""

    def __init__(self, data, content_type="application/json", status_code=200):
        self._data = data
        self._content_type = content_type
        self.status_code = status_code

    def raise_for_status(self):
        pass

    @property
    def headers(self):
        return {"content-type": self._content_type}

    @property
    def text(self):
        return ""

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Fake httpx.AsyncClient that returns predefined responses."""

    def __init__(self, init_response, list_response):
        self._init_response = init_response
        self._list_response = list_response
        self._call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            # First call is initialize
            return self._init_response
        else:
            # Subsequent calls are tools/list
            return self._list_response


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
        # Fake responses for initialize and tools/list
        init_response = _FakeResponse({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "serverInfo": {"name": "test", "version": "1.0"},
            },
        })

        list_response = _FakeResponse({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "gitlab_list_group_projects",
                        "description": "List projects",
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object", "properties": {"projects": {"type": "array"}}},
                    }
                ]
            },
        })

        fake_client = _FakeAsyncClient(init_response, list_response)
        monkeypatch.setattr(client.httpx, "AsyncClient", lambda *a, **k: fake_client)

        tools = await client._fetch_tool_list_live("http://fake/mcp/full")

        assert len(tools) == 1
        assert tools[0]["outputSchema"] == {"type": "object", "properties": {"projects": {"type": "array"}}}
        assert tools[0]["inputSchema"] == {"type": "object"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])