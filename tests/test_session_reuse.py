"""Tests for MCP session reuse in mcp2cli.client.

Verifies that _call_tool_live and _fetch_tool_list_unbounded reuse the same
Mcp-Session-Id across calls to the same endpoint, and that a stale-session
404 triggers a single retry with a fresh session.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp2cli import client as c


def _make_response(session_id=None, status_code=200, is_tool_result=True, content=None):
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}
    if session_id is not None:
        resp.headers["mcp-session-id"] = session_id
    if content is None:
        content = {"result": {"content": [{"type": "text", "text": "ok"}]}, "id": 2}
    resp.json.return_value = content
    resp.text = ""
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError

        def _raise():
            raise HTTPStatusError(
                f"{status_code}", request=MagicMock(), response=resp
            )

        resp.raise_for_status.side_effect = _raise
    return resp


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty session cache."""
    c._session_cache.clear()
    yield
    c._session_cache.clear()


@pytest.mark.asyncio
async def test_call_tool_live_reuses_session():
    """Two calls to the same endpoint should initialize only once."""
    init_resp = _make_response(session_id="sess-1", is_tool_result=False)
    tool_resp = _make_response()

    post_mock = AsyncMock(side_effect=[init_resp, tool_resp, tool_resp])
    client_mock = AsyncMock()
    client_mock.post = post_mock
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client_mock):
        ep = "http://test/mcp/k8s"
        await c._call_tool_live(ep, "k8s_pods_get", {"name": "x"})
        await c._call_tool_live(ep, "k8s_pods_list", {"namespace": "default"})

    # 1 initialize + 2 tool calls = 3 POSTs total (no second initialize)
    assert post_mock.call_count == 3
    assert c._get_cached_session_id(ep) == "sess-1"


@pytest.mark.asyncio
async def test_call_tool_live_retries_on_404():
    """A 404 on the tool call should invalidate cache and retry once."""
    init_resp_1 = _make_response(session_id="sess-1", is_tool_result=False)
    tool_resp_404 = _make_response(status_code=404)
    init_resp_2 = _make_response(session_id="sess-2", is_tool_result=False)
    tool_resp_ok = _make_response()

    post_mock = AsyncMock(
        side_effect=[init_resp_1, tool_resp_404, init_resp_2, tool_resp_ok]
    )
    client_mock = AsyncMock()
    client_mock.post = post_mock
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client_mock):
        ep = "http://test/mcp/k8s"
        result = await c._call_tool_live(ep, "k8s_pods_get", {"name": "x"})

    # init, 404 call, re-init, retry call = 4 POSTs
    assert post_mock.call_count == 4
    assert c._get_cached_session_id(ep) == "sess-2"


@pytest.mark.asyncio
async def test_call_tool_live_separate_endpoints():
    """Different endpoints get independent sessions."""
    init_a = _make_response(session_id="sess-a", is_tool_result=False)
    tool_a = _make_response()
    init_b = _make_response(session_id="sess-b", is_tool_result=False)
    tool_b = _make_response()

    post_mock = AsyncMock(side_effect=[init_a, tool_a, init_b, tool_b])
    client_mock = AsyncMock()
    client_mock.post = post_mock
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client_mock):
        await c._call_tool_live("http://test/mcp/k8s", "k8s_pods_get", {})
        await c._call_tool_live("http://test/mcp/exec", "exec_run", {})

    assert c._get_cached_session_id("http://test/mcp/k8s") == "sess-a"
    assert c._get_cached_session_id("http://test/mcp/exec") == "sess-b"


@pytest.mark.asyncio
async def test_fetch_tool_list_reuses_session():
    """Tool list fetch should also reuse cached sessions."""
    init_resp = _make_response(session_id="sess-1", is_tool_result=False)
    list_resp = _make_response(
        content={"result": {"tools": [{"name": "k8s_pods_get"}]}, "id": 2}
    )

    post_mock = AsyncMock(side_effect=[init_resp, list_resp, list_resp])
    client_mock = AsyncMock()
    client_mock.post = post_mock
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client_mock):
        ep = "http://test/mcp/k8s"
        await c._fetch_tool_list_unbounded(ep)
        await c._fetch_tool_list_unbounded(ep)

    # 1 initialize + 2 list calls = 3 POSTs
    assert post_mock.call_count == 3
    assert c._get_cached_session_id(ep) == "sess-1"


def test_cache_thread_safety():
    """Concurrent sets from multiple threads must not corrupt the cache."""
    import threading

    errors = []

    def worker(ep, sid):
        try:
            c._set_cached_session_id(ep, sid)
            got = c._get_cached_session_id(ep)
            if got != sid:
                errors.append(f"{ep}: expected {sid}, got {got}")
        except Exception as e:
            errors.append(str(e))

    threads = [
        threading.Thread(target=worker, args=(f"ep{i}", f"sid{i}")) for i in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread safety errors: {errors}"
    for i in range(50):
        assert c._get_cached_session_id(f"ep{i}") == f"sid{i}"
