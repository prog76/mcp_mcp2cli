#!/usr/bin/env python3
"""Tests for mcp2cli.client prompt helpers (pure-formatting functions, no server)."""

from types import SimpleNamespace

from mcp2cli.client import _format_prompt_content, _format_prompt_result, _split_server_prefix


def test_format_prompt_content_text():
    assert _format_prompt_content(SimpleNamespace(text="hello world")) == "hello world"


def test_format_prompt_content_fallback():
    obj = SimpleNamespace(mimeType="text/plain", uri="file:///x")
    out = _format_prompt_content(obj)
    assert "text/plain" in out  # falls back to repr for non-.text content


def test_format_prompt_result_with_description_and_messages():
    result = SimpleNamespace(
        description="My prompt",
        messages=[
            SimpleNamespace(role="user", content=SimpleNamespace(text="hello")),
            SimpleNamespace(role="assistant", content=SimpleNamespace(text="hi there")),
        ],
    )
    out = _format_prompt_result(result)
    assert "# My prompt" in out
    assert "[user]" in out
    assert "hello" in out
    assert "[assistant]" in out
    assert "hi there" in out


def test_format_prompt_result_empty_messages():
    result = SimpleNamespace(description=None, messages=[])
    assert _format_prompt_result(result) == ""


def test_fetch_prompt_list_returns_empty_on_error():
    """fetch_prompt_list swallows transport errors and returns [] (no server)."""
    # Point at an unreachable endpoint; must return [] not raise.
    from mcp2cli.client import fetch_prompt_list

    out = fetch_prompt_list("http://127.0.0.1:1/mcp/none")
    assert out == []


def test_get_prompt_returns_error_prefix_on_failure():
    """get_prompt returns an 'Error getting prompt' string on transport failure."""
    from mcp2cli.client import get_prompt

    out = get_prompt("http://127.0.0.1:1/mcp/none", "nope")
    assert out.startswith("Error getting prompt 'nope':")