#!/usr/bin/env python3
"""Progress notifications and the progress-aware (idle) call timeout.

``_call_tool_live_progress_timed`` re-arms its deadline on every progress
notification, so a tool that keeps reporting (e.g. a long skills playbook with
keep-alive beats) is never killed by ``--timeout-seconds``, while a silent one
still times out. The CLI prints each notification to stderr by default
(``MCP2CLI_PROGRESS`` / ``--no-progress`` disable it).
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from mcp2cli import client as mcp2cli_client


def _install_fake_live(monkeypatch, behavior):
    """Replace ``_call_tool_live`` with a fake driven by ``behavior``."""

    result = SimpleNamespace(content=[SimpleNamespace(type="text", text="done")])

    async def fake(endpoint, tool_id, arguments, progress_callback=None):
        await behavior(progress_callback)
        return result

    monkeypatch.setattr(mcp2cli_client, "_call_tool_live", fake)
    return fake


def test_no_progress_times_out_as_before(monkeypatch):
    """No progress notifications -> wall-clock timeout still applies."""

    async def behavior(progress_callback):
        await asyncio.sleep(5)

    _install_fake_live(monkeypatch, behavior)
    start = time.monotonic()
    out = mcp2cli_client.call_tool("http://x/mcp", "t", {}, timeout_seconds=1, progress=False)
    elapsed = time.monotonic() - start

    assert elapsed < 3, f"call was not bounded by the timeout: took {elapsed:.1f}s"
    assert out.startswith("Error calling tool 't'")
    assert "no progress for 1s" in out
    assert "wait timeout" in out


def test_progress_resets_idle_deadline(monkeypatch):
    """A tool reporting beats every 0.2s survives a 1s idle timeout."""
    seen = []

    async def behavior(progress_callback):
        for i in range(10):
            await asyncio.sleep(0.2)
            if progress_callback is not None:
                await progress_callback(float(i + 1), None, f"step {i + 1}")

    _install_fake_live(monkeypatch, behavior)
    start = time.monotonic()
    out = mcp2cli_client.call_tool(
        "http://x/mcp", "t", {}, timeout_seconds=1,
        on_progress=lambda p, tot, msg: seen.append(msg),
    )
    elapsed = time.monotonic() - start

    assert out == "done"  # ~2s total > 1s timeout: the deadline was re-armed
    assert elapsed > 1.5, f"expected the call to outlive the 1s idle timeout, took {elapsed:.1f}s"
    assert len(seen) >= 8, f"expected most beats to be delivered, got {len(seen)}"
    assert seen[0] == "step 1"


def test_stderr_progress_reporter_prints_messages(monkeypatch, capsys):
    monkeypatch.delenv("MCP2CLI_PROGRESS", raising=False)

    async def behavior(progress_callback):
        if progress_callback is not None:
            await progress_callback(1.0, None, "collecting gcdump (10s)")

    _install_fake_live(monkeypatch, behavior)
    out = mcp2cli_client.call_tool("http://x/mcp", "t", {}, timeout_seconds=5)

    assert out == "done"
    err = capsys.readouterr().err
    assert "collecting gcdump (10s)" in err
    assert "⏳" in err
    assert "⏳ t:" in err  # tool id included


def test_mcp2cli_progress_env_disables_reporting(monkeypatch, capsys):
    monkeypatch.setenv("MCP2CLI_PROGRESS", "0")

    async def behavior(progress_callback):
        if progress_callback is not None:
            await progress_callback(1.0, None, "beat")

    _install_fake_live(monkeypatch, behavior)
    mcp2cli_client.call_tool("http://x/mcp", "t", {}, timeout_seconds=5)

    assert capsys.readouterr().err == ""


def test_progress_false_disables_reporting(monkeypatch, capsys):
    async def behavior(progress_callback):
        if progress_callback is not None:
            await progress_callback(1.0, None, "beat")

    _install_fake_live(monkeypatch, behavior)
    mcp2cli_client.call_tool("http://x/mcp", "t", {}, timeout_seconds=5, progress=False)

    assert capsys.readouterr().err == ""


class TestCLIProgressFlag:
    def _patch(self, monkeypatch, captured):
        import mcp2cli.cli as cli_mod

        monkeypatch.setattr(cli_mod, "fetch_tool_list", lambda *a, **kw: [])
        monkeypatch.setattr(cli_mod, "resolve_tool_id", lambda p, n: p)

        def fake_call(endpoint, tool_id, arguments, **kw):
            captured.update(kw)
            return "ok"

        monkeypatch.setattr(cli_mod, "call_tool", fake_call)

    def test_cli_requests_progress_by_default(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        captured = {}
        self._patch(monkeypatch, captured)
        rc = cli_mod.main(["call", "test_tool"])
        assert rc == 0
        assert captured.get("progress") is True

    def test_cli_no_progress_flag(self, monkeypatch):
        import mcp2cli.cli as cli_mod

        captured = {}
        self._patch(monkeypatch, captured)
        rc = cli_mod.main(["call", "test_tool", "--no-progress"])
        assert rc == 0
        assert captured.get("progress") is False

    def test_rewrite_direct_kv_keeps_no_progress_flag(self):
        from mcp2cli.cli import _rewrite_direct_kv

        argv = ["call", "t", "--no-progress", "--a=1"]
        assert _rewrite_direct_kv(argv) == ["call", "t", "--no-progress", "--args", "a=1"]
