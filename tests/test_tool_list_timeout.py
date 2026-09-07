#!/usr/bin/env python3
"""Regression: a hung endpoint must time out on tools/list, not hang forever.

The streamable-HTTP session (connect + initialize + tools/list) has no transport
timeout of its own. Before the fix, an unresponsive backend blocked
``_fetch_tool_list_live`` indefinitely — and because every bridge helper and
every mcp2cli subcommand calls it first, a single hung upstream wedged the
whole ipybox server. The per-call ``asyncio.wait_for`` is what turns that hang
into a ``TimeoutError``.
"""

import asyncio
import time

import pytest

from mcp2cli import client as mcp2cli_client


class _HungClientSession:
    """ClientSession stand-in whose initialize/list_tools never return."""

    def __init__(self, read_stream, write_stream):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        await asyncio.sleep(3600)

    async def list_tools(self):
        await asyncio.sleep(3600)


class _HungStreams:
    """streamablehttp_client stand-in: connects, then the session hangs."""

    async def __aenter__(self):
        # mcp2cli unpacks (_streams[0], _streams[1]); values are unused by the
        # hung session above.
        return (None, None)

    async def __aexit__(self, *exc):
        return False


def test_fetch_tool_list_live_times_out_when_endpoint_hangs(monkeypatch):
    """A hung tools/list must raise TimeoutError within the bound, not hang."""
    monkeypatch.setattr(mcp2cli_client, "DEFAULT_TOOL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(
        mcp2cli_client, "streamablehttp_client", lambda _endpoint: _HungStreams()
    )
    monkeypatch.setattr(mcp2cli_client, "ClientSession", _HungClientSession)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(mcp2cli_client._fetch_tool_list_live("http://127.0.0.1:9/mcp/none"))
    elapsed = time.monotonic() - start

    # Bounded by DEFAULT_TOOL_TIMEOUT_SECONDS (1s); must not approach the 3600s hang.
    assert elapsed < 10, f"tools/list fetch was not bounded: took {elapsed:.1f}s"


def test_fetch_tool_list_async_propagates_timeout_bounded(monkeypatch):
    """``fetch_tool_list_async`` propagates the timeout, bounded.

    The friendly-string wrapping happens one layer up (ipybox's
    ``mcp_list_upstreams_async``), so this async wrapper simply propagates the
    ``TimeoutError`` — but it must do so within the bound, not hang.
    """
    monkeypatch.setattr(mcp2cli_client, "DEFAULT_TOOL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(
        mcp2cli_client, "streamablehttp_client", lambda _endpoint: _HungStreams()
    )
    monkeypatch.setattr(mcp2cli_client, "ClientSession", _HungClientSession)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(
            mcp2cli_client.fetch_tool_list_async(
                endpoint="http://127.0.0.1:9/mcp/none",
                cache_dir=mcp2cli_client._default_cache_dir(),
                cache_ttl_s=3600,
                refresh=True,
            )
        )
    elapsed = time.monotonic() - start

    assert elapsed < 10, f"async fetch was not bounded: took {elapsed:.1f}s"