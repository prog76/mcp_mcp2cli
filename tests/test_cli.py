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

    def test_list_prompts_no_prompts(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        monkeypatch.setattr(cli_mod, "fetch_prompt_list", lambda *a, **kw: [])
        rc = main(["list-prompts"])
        assert rc == 0

    def test_list_prompts_prints_entries(self, monkeypatch, capsys):
        import mcp2cli.cli as cli_mod

        fake = [
            {"name": "a_boot", "description": "Bootstrap prompt", "arguments": []},
            {"name": "b_help", "description": "", "arguments": [{"name": "topic"}]},
        ]
        monkeypatch.setattr(cli_mod, "fetch_prompt_list", lambda *a, **kw: fake)
        rc = main(["list-prompts"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "- a_boot  Bootstrap prompt" in out
        assert "- b_help" in out
        assert "arguments: topic" in out

    def test_get_prompt_passes_name_and_no_args(self, monkeypatch, capsys):
        import mcp2cli.cli as cli_mod

        captured = {}

        def fake_get(endpoint, name, arguments=None):
            captured["name"] = name
            captured["arguments"] = arguments
            return "rendered body"

        monkeypatch.setattr(cli_mod, "get_prompt", fake_get)
        monkeypatch.setattr(cli_mod, "handle_large_output", lambda out, **kw: out)
        rc = main(["get-prompt", "a_boot"])
        assert rc == 0
        assert captured["name"] == "a_boot"
        assert captured["arguments"] is None

    def test_get_prompt_with_args_json(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        captured = {}

        def fake_get(endpoint, name, arguments=None):
            captured["arguments"] = arguments
            return "ok"

        monkeypatch.setattr(cli_mod, "get_prompt", fake_get)
        monkeypatch.setattr(cli_mod, "handle_large_output", lambda out, **kw: out)
        rc = main(["get-prompt", "my_prompt", "--args-json", '{"name": "bob"}'])
        assert rc == 0
        assert captured["arguments"] == {"name": "bob"}

    def test_get_prompt_invalid_args_json(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        monkeypatch.setattr(cli_mod, "get_prompt", lambda *a, **kw: "ok")
        rc = main(["get-prompt", "my_prompt", "--args-json", "{bad"])
        assert rc == 2

    def test_get_prompt_error_result_returns_1(self, monkeypatch, capsys):
        import mcp2cli.cli as cli_mod

        monkeypatch.setattr(cli_mod, "get_prompt", lambda *a, **kw: "Error getting prompt 'x': boom")
        monkeypatch.setattr(cli_mod, "handle_large_output", lambda out, **kw: out)
        rc = main(["get-prompt", "x"])
        assert rc == 1


class TestDirectKVArgs:
    """Direct --key=value flags on `call` (rewritten to --args)."""

    def _run_call(self, monkeypatch, argv):
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
        rc = main(argv)
        return rc, captured

    def test_direct_kv_becomes_tool_args(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--context=devops", "--namespace=default"],
        )
        assert rc == 0
        assert captured["arguments"] == {"context": "devops", "namespace": "default"}

    def test_bare_flag_becomes_true(self, monkeypatch):
        rc, captured = self._run_call(monkeypatch, ["call", "test_tool", "--verbose"])
        assert rc == 0
        assert captured["arguments"] == {"verbose": True}

    def test_scalar_values_parsed(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--count=42", "--ratio=1.5", "--off=false", "--name=abc"],
        )
        assert rc == 0
        assert captured["arguments"] == {"count": 42, "ratio": 1.5, "off": False, "name": "abc"}

    def test_dotted_and_array_direct_keys(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--query.text=hello", "--labels[]=x", "--labels[]=y"],
        )
        assert rc == 0
        assert captured["arguments"] == {"query": {"text": "hello"}, "labels": ["x", "y"]}

    def test_reserved_flags_not_converted(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--timeout-seconds=9", "--context=devops"],
        )
        assert rc == 0
        assert captured["arguments"] == {"context": "devops"}

    def test_direct_kv_merges_over_args_json(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--args-json", '{"context": "prod", "extra": 1}', "--context=devops"],
        )
        assert rc == 0
        assert captured["arguments"] == {"context": "devops", "extra": 1}

    def test_direct_kv_with_args_mixed(self, monkeypatch):
        rc, captured = self._run_call(
            monkeypatch,
            ["call", "test_tool", "--args", "a=1", "--b=2"],
        )
        assert rc == 0
        assert captured["arguments"] == {"a": 1, "b": 2}

    def test_direct_kv_stdin_marker(self, monkeypatch):
        import io

        import mcp2cli.cli as cli_mod

        captured = {}

        def fake_call(endpoint, tool_id, arguments, **kw):
            captured["arguments"] = arguments
            return "ok"

        monkeypatch.setattr(cli_mod, "fetch_tool_list", lambda *a, **kw: [])
        monkeypatch.setattr(cli_mod, "resolve_tool_id", lambda p, n: p)
        monkeypatch.setattr(cli_mod, "call_tool", fake_call)
        monkeypatch.setattr("sys.stdin", io.StringIO("my-pod"))

        rc = main(["call", "test_tool", "--context=devops", "--name=@stdin"])
        assert rc == 0
        assert captured["arguments"] == {"context": "devops", "name": "my-pod"}


class TestRewriteDirectKV:
    """Pure tests for mcp2cli.cli._rewrite_direct_kv."""

    def test_passthrough_other_subcommand(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = ["list-tools", "k8s", "--weird=1"]
        assert _rewrite_direct_kv(argv) == argv

    def test_passthrough_no_args(self):
        from mcp2cli.cli import _rewrite_direct_kv

        assert _rewrite_direct_kv(["list-servers"]) == ["list-servers"]

    def test_global_flags_before_call(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = ["--cache-ttl-seconds", "30", "call", "t", "--context=devops"]
        assert _rewrite_direct_kv(argv) == [
            "--cache-ttl-seconds", "30", "call", "t", "--args", "context=devops",
        ]

    def test_reserved_flags_kept_with_values(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = [
            "call", "t", "--timeout-seconds", "9",
            "--output-threshold-kb=50", "--args-json", "-", "--refresh",
        ]
        assert _rewrite_direct_kv(argv) == argv

    def test_inline_reserved_flag_kept(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = ["call", "t", "--timeout-seconds=9", "--context=devops"]
        assert _rewrite_direct_kv(argv) == [
            "call", "t", "--timeout-seconds=9", "--args", "context=devops",
        ]

    def test_stops_at_double_dash(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = ["call", "t", "--", "--weird=1"]
        assert _rewrite_direct_kv(argv) == argv

    def test_value_with_equals_sign(self):
        from mcp2cli.cli import _rewrite_direct_kv

        assert _rewrite_direct_kv(["call", "t", "--token=a=b"]) == [
            "call", "t", "--args", "token=a=b",
        ]
