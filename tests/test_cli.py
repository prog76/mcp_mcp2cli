#!/usr/bin/env python3
"""Tests for mcp2cli.cli — argument parsing (no live server needed)."""

import pytest

from mcp2cli.cli import main


class TestCLIParsing:
    def test_help_list_servers(self, capsys):
        with pytest.raises(SystemExit):
            main()
        out = capsys.readouterr()
        assert "list-tools" in out.err or "list-tools" in out.out

    def test_no_subcommand_errors(self):
        with pytest.raises(SystemExit):
            main([])

    def test_list_tools_requires_servers(self, monkeypatch):
        """list-tools with empty servers string returns code 2 without calling server."""
        monkeypatch.setattr("sys.argv", ["mcp2cli", "list-tools", ""])

        import mcp2cli.cli as cli_mod

        # Stub fetch_tool_list so we never hit the network
        def fake_fetch(*a, **kw):
            return []

        monkeypatch.setattr(cli_mod, "fetch_tool_list", fake_fetch)

        rc = main(["list-tools", ""])
        assert rc == 2

    def test_describe_no_tool_ids(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        def fake_fetch(*a, **kw):
            return []

        monkeypatch.setattr(cli_mod, "fetch_tool_list", fake_fetch)
        rc = main(["describe", ""])
        assert rc == 2

    def test_call_with_args_json_dict(self, monkeypatch):
        """Verify --args-json parsing produces a dict for the tool call."""
        import mcp2cli.cli as cli_mod

        captured = {}

        def fake_fetch(*a, **kw):
            return []

        def fake_resolve(provided, names):
            captured["provided"] = provided
            captured["names"] = names
            return provided

        def fake_call(*a, **kw):
            captured["called"] = True
            return "ok"

        monkeypatch.setattr(cli_mod, "fetch_tool_list", fake_fetch)
        monkeypatch.setattr(cli_mod, "resolve_tool_id", fake_resolve)
        monkeypatch.setattr(cli_mod, "call_tool", fake_call)

        rc = main(["call", "test_tool", "--args-json", '{"key": "val"}'])
        assert rc == 0
        assert captured.get("called") is True

    def test_call_with_dotted_args(self, monkeypatch):
        """Verify --args dotted-key parsing builds nested dict."""
        import mcp2cli.cli as cli_mod

        captured = {}

        def fake_fetch(*a, **kw):
            return [{"name": "test_tool", "description": "", "inputSchema": {}}]

        def fake_call(endpoint, tool_id, arguments, **kw):
            captured["arguments"] = arguments
            return "ok"

        monkeypatch.setattr(cli_mod, "fetch_tool_list", fake_fetch)
        monkeypatch.setattr(cli_mod, "resolve_tool_id", lambda p, n: p)
        monkeypatch.setattr(cli_mod, "call_tool", fake_call)

        rc = main(["call", "test_tool", "--args", "query.text=hello", "--args", "labels[]=x"])
        assert rc == 0
        assert captured["arguments"] == {"query": {"text": "hello"}, "labels": ["x"]}
