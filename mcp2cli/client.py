#!/usr/bin/env python3
"""
mcp2cli.client — Shared MCP client utilities.

Adapted from the policy-proxy ``mcp_client_lib.py``. It contains no
argparse logic; it exposes callables that accept plain Python values and
return plain Python values.

Used by:
  - mcp2cli.cli  (CLI: list-servers, list-tools, describe, call, list-prompts, get-prompt)
  - skills_server (native MCP tools: mcp_list_upstreams, mcp_call, ...)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

import httpx


log = logging.getLogger(__name__)


class _IgnoreUnknownSseEvent(logging.Filter):
    """Suppress the MCP SDK's noisy 'Unknown SSE event: endpoint' warning.

    The Streamable HTTP spec requires servers to send `event: endpoint` SSE
    messages telling the client the GET-stream URL. The SDK's
    `_handle_sse_event` treats any non-`message` event as unknown and logs a
    warning. The event is harmless (we use the same URL for GET), so we drop
    that specific log line to reduce noise in tool output.
    """

    _MESSAGE = "Unknown SSE event"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._MESSAGE not in record.getMessage()


# Silence the SDK's harmless 'Unknown SSE event: endpoint' warning.
_sdk_logger = logging.getLogger("mcp.client.streamable_http")
_sdk_logger.addFilter(_IgnoreUnknownSseEvent())

DEFAULT_ENDPOINT = "http://localhost:8000/mcp/full"
DEFAULT_CACHE_TTL_SECONDS = 3600
DEFAULT_OUTPUT_THRESHOLD_CHARS = 50 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = int(os.environ.get("MCP_TOOL_TIMEOUT_SECONDS", "120"))


def _default_cache_dir() -> Path:
    return Path(os.environ.get("MCP2CLI_CACHE_DIR", "/tmp/mcp2cli_cache"))


def _default_workspace_dir() -> Path:
    return Path(os.environ.get("MCP2CLI_WORKSPACE_DIR", "/var/mcp/workspace"))


def _default_endpoint() -> str:
    return os.environ.get("MCP_ENDPOINT", DEFAULT_ENDPOINT)


def _split_server_prefix(tool_id: str) -> str:
    if "_" not in tool_id:
        return tool_id
    return tool_id.split("_", 1)[0]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
# An endpoint may require an OAuth bearer token: it answers 401 with an RFC 9728
# challenge naming its protected-resource metadata. This client never runs a
# login flow by itself -- minting a grant is an explicit operator action
# (`mcp2cli auth login`). Here we only attach a stored token, refresh it when it
# has expired, and turn a rejected request into a command the operator can run.

_DEFAULT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _attach_auth(endpoint: str, hdrs: Dict[str, str]) -> None:
    """Add the stored bearer token for ``endpoint`` to ``hdrs``, if there is one.

    Not having a usable grant is not fatal: the request is sent unauthenticated
    and a server that wants a token answers 401, which :func:`_auth_hint` turns
    into instructions.
    """
    from mcp2cli import auth

    token = auth.bearer_for(endpoint)
    if token:
        hdrs["Authorization"] = f"Bearer {token}"


def _auth_hint(response: httpx.Response, endpoint: str) -> Optional[str]:
    """The 'run mcp2cli auth login' hint for a bearer challenge, else None."""
    from mcp2cli import auth

    return auth.challenge_hint(
        response.headers.get("WWW-Authenticate", ""), endpoint
    )


# ---------------------------------------------------------------------------
# MCP session reuse cache
# ---------------------------------------------------------------------------
# Reuses the same Mcp-Session-Id across multiple calls to the same endpoint
# within one process. This makes the gateway's session-scoped confirm bypass
# ("Allow 10 min (session)") cover every call from a single client session
# (e.g. one ipybox kernel) instead of requiring per-call approval — because
# without reuse, every mcp_call() initializes a fresh session with a new id.
#
# Keyed by endpoint URL -> Mcp-Session-Id. Lives in process memory and dies
# naturally with the process (no explicit cleanup needed). A threading.Lock
# guards get-or-init because ipybox kernels run concurrent work in threads
# (background jobs, async prompt helpers via _sync()'s worker threads).
#
# Server-side session expiry / gateway restart surfaces as HTTP 404
# ("Invalid or expired session ID"); callers invalidate the cache and retry
# once with a fresh session on that condition.

_session_cache: Dict[str, str] = {}
_session_cache_lock = threading.Lock()


def _get_cached_session_id(endpoint: str) -> Optional[str]:
    """Return cached Mcp-Session-Id for endpoint, or None."""
    with _session_cache_lock:
        return _session_cache.get(endpoint)


def _set_cached_session_id(endpoint: str, session_id: str) -> None:
    """Cache Mcp-Session-Id for endpoint."""
    with _session_cache_lock:
        _session_cache[endpoint] = session_id


def _invalidate_cached_session_id(endpoint: str) -> None:
    """Drop cached session for endpoint (e.g. after a 404)."""
    with _session_cache_lock:
        _session_cache.pop(endpoint, None)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

@dataclass
class ToolListCache:
    tools: List[Dict[str, Any]]
    fetched_at: float


def _cache_path(cache_dir: Path, endpoint: str) -> Path:
    safe = endpoint.replace("://", "_").replace("/", "_").replace("?", "_").replace("&", "_")
    return cache_dir / f"tool_list_{safe}.json"


def load_cache(cache_dir: Path, endpoint: str, ttl_s: int) -> Optional[ToolListCache]:
    cache_dir = Path(cache_dir)
    cp = _cache_path(cache_dir, endpoint)
    if not cp.exists():
        return None
    try:
        raw = cp.read_text(encoding="utf-8")
        obj = json.loads(raw)
        fetched_at = float(obj.get("fetched_at", 0))
        if time.time() - fetched_at > ttl_s:
            return None
        tools = obj.get("tools", [])
        if not isinstance(tools, list):
            return None
        return ToolListCache(tools=tools, fetched_at=fetched_at)
    except Exception:
        return None


def save_cache(cache_dir: Path, endpoint: str, tools: List[Dict[str, Any]]) -> None:
    cache_dir = Path(cache_dir)
    cp = _cache_path(cache_dir, endpoint)
    obj = {"fetched_at": time.time(), "tools": tools}
    cp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Live fetching
# ---------------------------------------------------------------------------


async def _initialize_session(
    c: httpx.AsyncClient,
    endpoint: str,
    hdrs: Dict[str, str],
) -> str:
    """Initialize a new MCP session, set the session id header, cache it,
    and return the session id (empty string on unexpected failure)."""
    init_req = {
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp2cli", "version": "0.1"},
        },
        "jsonrpc": "2.0",
        "id": 1,
    }
    from mcp2cli import auth

    _attach_auth(endpoint, hdrs)
    resp = await c.post(endpoint, json=init_req, headers=hdrs)
    if resp.status_code in (401, 403):
        hint = _auth_hint(resp, endpoint)
        if hint:
            raise auth.AuthChallenge(hint)
    resp.raise_for_status()

    session_id = resp.headers.get("mcp-session-id", "")
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id
        _set_cached_session_id(endpoint, session_id)

    init_data = _parse_response(resp)
    if "error" in init_data:
        raise RuntimeError(f"Initialize failed: {init_data['error']}")

    return session_id


async def _ensure_session(
    c: httpx.AsyncClient,
    endpoint: str,
    hdrs: Dict[str, str],
) -> str:
    """Return an active Mcp-Session-Id for endpoint, using the cache when valid.

    If a cached session id exists it is applied to hdrs without re-initializing.
    Otherwise a new session is initialized and cached. Returns the session id
    (may be empty if the server does not assign one)."""
    cached = _get_cached_session_id(endpoint)
    if cached:
        hdrs["Mcp-Session-Id"] = cached
        return cached

    return await _initialize_session(c, endpoint, hdrs)


async def _fetch_tool_list_unbounded(endpoint: str) -> List[Dict[str, Any]]:
    """Raw, unbounded tool-list fetch.

    Prefer :func:`_fetch_tool_list_live`, which wraps this in a timeout. This
    split exists so the timeout boundary is explicit and testable.
    """
    hdrs = dict(_DEFAULT_HEADERS)
    _attach_auth(endpoint, hdrs)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        # Ensure a cached session (reuses Mcp-Session-Id across calls)
        await _ensure_session(c, endpoint, hdrs)

        # List tools with session ID, retry once on stale session (404)
        list_req = {
            "method": "tools/list",
            "params": {},
            "jsonrpc": "2.0",
            "id": 2,
        }
        try:
            resp2 = await c.post(endpoint, json=list_req, headers=hdrs)
            resp2.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404 and _get_cached_session_id(endpoint):
                log.info("Stale MCP session for %s, re-initializing", endpoint)
                _invalidate_cached_session_id(endpoint)
                hdrs.pop("Mcp-Session-Id", None)
                await _ensure_session(c, endpoint, hdrs)
                resp2 = await c.post(endpoint, json=list_req, headers=hdrs)
                resp2.raise_for_status()
            elif e.response.status_code in (401, 403):
                hint = _auth_hint(e.response, endpoint)
                if hint:
                    raise auth.AuthChallenge(hint) from e
                raise
            else:
                raise

        result = _parse_response(resp2)
        if "error" in result:
            raise RuntimeError(f"Tools list failed: {result['error']}")

        tools = result.get("result", {}).get("tools", [])
        out: List[Dict[str, Any]] = []
        for t in tools:
            out.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", "") or "",
                    "inputSchema": t.get("inputSchema"),
                    "outputSchema": t.get("outputSchema"),
                }
            )
        return out


async def _fetch_tool_list_live(endpoint: str) -> List[Dict[str, Any]]:
    """Fetch the live tool list from ``endpoint``, bounded by ``DEFAULT_TOOL_TIMEOUT_SECONDS``.

    The streamable-HTTP session (connect + ``initialize`` + ``tools/list``) has no
    transport timeout of its own. Every bridge helper in ipybox and every
    ``mcp2cli`` subcommand calls this first, so without this single bound an
    unresponsive backend would block the caller forever — and, when that caller
    is the FastMCP event loop, wedge the whole server (``execute_code``,
    ``list-servers``, everything). That is exactly the failure we are hardening
    against. The per-call ``wait_for`` is the mechanism that turns a hung
    upstream into a :class:`TimeoutError` instead of a freeze.
    """
    return await asyncio.wait_for(
        _fetch_tool_list_unbounded(endpoint),
        timeout=DEFAULT_TOOL_TIMEOUT_SECONDS,
    )


async def fetch_tool_list_async(
    endpoint: str,
    cache_dir: Path,
    cache_ttl_s: int,
    refresh: bool = False,
) -> List[Dict[str, Any]]:
    cache_dir = Path(cache_dir)
    if not refresh:
        cached = load_cache(cache_dir, endpoint, cache_ttl_s)
        if cached is not None:
            return cached.tools

    tools = await _fetch_tool_list_live(endpoint)
    cache_dir.mkdir(parents=True, exist_ok=True)
    save_cache(cache_dir, endpoint, tools)
    return tools


def fetch_tool_list(
    endpoint: str,
    cache_dir: Path,
    cache_ttl_s: int,
    refresh: bool = False,
) -> List[Dict[str, Any]]:
    cache_dir = Path(cache_dir)
    if not refresh:
        cached = load_cache(cache_dir, endpoint, cache_ttl_s)
        if cached is not None:
            return cached.tools

    tools = asyncio.run(_fetch_tool_list_live(endpoint))
    cache_dir.mkdir(parents=True, exist_ok=True)
    save_cache(cache_dir, endpoint, tools)
    return tools


# ---------------------------------------------------------------------------
# Schema formatting
# ---------------------------------------------------------------------------

def format_tool_schema(tool_obj: Dict[str, Any]) -> str:
    desc = tool_obj.get("description") or ""
    params = tool_obj.get("inputSchema") or tool_obj.get("input_schema") or None
    if isinstance(params, dict):
        props = params.get("properties")
        if isinstance(props, dict) and len(props) == 0:
            params = {
                "schema_empty": True,
                "note": "MCP returned an empty input schema for this tool. Use tool description/examples as the source of parameter hints.",
            }
    out: Dict[str, Any] = {
        "tool_id": tool_obj.get("name") or tool_obj.get("tool_id"),
        "description": desc,
        "parameters": params,
    }
    # Emit the advertised output schema only when the serving compound exposes
    # one. Browser-facing compounds with ``schema: minimal`` strip outputSchema
    # from tools/list at the proxy (MountedServer), so ``describe`` omits it
    # there — the stripping is controlled in that single place, not here.
    output_schema = tool_obj.get("outputSchema") or tool_obj.get("output_schema")
    if output_schema is not None:
        out["output"] = output_schema
    return json.dumps(out, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool ID resolution
# ---------------------------------------------------------------------------

def resolve_tool_id(provided_tool_id: str, tool_names: List[str]) -> str:
    if provided_tool_id in tool_names:
        return provided_tool_id

    needle = f"_{provided_tool_id}"
    matches = [n for n in tool_names if n.endswith(needle)]
    if len(matches) == 1:
        return matches[0]
    elif not matches:
        raise ValueError(f"Tool not found: '{provided_tool_id}'.")
    else:
        raise ValueError(
            f"Ambiguous real tool name '{provided_tool_id}'; matches: {', '.join(matches)}. "
            f"Use the full tool id (e.g. '{matches[0]}') to disambiguate."
        )


# ---------------------------------------------------------------------------
# Tool calling
# ---------------------------------------------------------------------------

async def _call_tool_live(
    endpoint: str,
    tool_id: str,
    arguments: Dict[str, Any],
    progress_callback: Optional[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]] = None,
) -> Any:
    """Call a tool via streamable HTTP with session reuse.

    Reuses the same Mcp-Session-Id across calls to the same endpoint so the
    gateway's session-scoped confirm bypass ("Allow 10 min (session)") covers
    every call from one client session. On a stale/expired session (HTTP 404
    "Invalid or expired session ID" from the MCP SDK streamable-HTTP server)
    the cache is invalidated and the call is retried once with a fresh session.

    The MCP SDK v1.30.0 has a bug where it doesn't always resend the
    Mcp-Session-Id header on subsequent requests after initialize().
        This implementation uses raw HTTP (like skill_runner.py) to ensure
    the session header is always included.
    """
    hdrs = dict(_DEFAULT_HEADERS)
    _attach_auth(endpoint, hdrs)
    # Forward the kernel's stable operator session (MCP_SESSION_ID, injected by
    # the gateway policy from the operator's inbound Mcp-Session-Id) under a
    # dedicated header. The gateway keys its per-session confirm bypass
    # ("Allow 10 min (session)") on this value so the grant survives ipybox
    # kernel idle-reaps — the kernel-local mcp2cli Mcp-Session-Id is re-created
    # on every reap and would otherwise force a fresh approval each time.
    _op_session = os.environ.get("MCP_SESSION_ID")
    if _op_session:
        hdrs["X-MCP-Operator-Session"] = _op_session
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        # Ensure a cached session (reuses Mcp-Session-Id across calls)
        await _ensure_session(c, endpoint, hdrs)

        # Call the tool with session ID, retry once on stale session (404)
        call_req = {
            "method": "tools/call",
            "params": {"name": tool_id, "arguments": arguments},
            "jsonrpc": "2.0",
            "id": 2,
        }

        # Request progress notifications if callback provided
        if progress_callback is not None:
            call_req["params"]["_meta"] = {"progressToken": str(uuid.uuid4())}

        try:
            resp2 = await c.post(endpoint, json=call_req, headers=hdrs)
            resp2.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404 and _get_cached_session_id(endpoint):
                log.info("Stale MCP session for %s, re-initializing", endpoint)
                _invalidate_cached_session_id(endpoint)
                hdrs.pop("Mcp-Session-Id", None)
                await _ensure_session(c, endpoint, hdrs)
                resp2 = await c.post(endpoint, json=call_req, headers=hdrs)
                resp2.raise_for_status()
            elif e.response.status_code in (401, 403):
                hint = _auth_hint(e.response, endpoint)
                if hint:
                    raise auth.AuthChallenge(hint) from e
                raise
            else:
                raise

        result = _parse_response(resp2, progress_callback=progress_callback)
        if "error" in result:
            raise RuntimeError(f"Tool call failed: {result['error']}")

        return result


def _parse_response(
    resp: httpx.Response,
    progress_callback: Optional[Callable[[float, Optional[float], Optional[str]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Parse MCP response handling both JSON and SSE formats.

    If progress_callback is provided, progress notifications found in SSE
    frames will be forwarded to the callback.
    """
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        data = {}
        for frame in resp.text.split("\n\n"):
            for line in frame.splitlines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if not isinstance(d, dict):
                        continue
                    # Check for progress notification
                    if (progress_callback is not None
                            and d.get("method") == "notifications/progress"
                            and "params" in d):
                        params = d["params"]
                        try:
                            progress = float(params.get("progress", 0))
                            total = params.get("total")
                            if total is not None:
                                total = float(total)
                            message = params.get("message")
                            asyncio.create_task(progress_callback(progress, total, message))
                        except (ValueError, TypeError):
                            pass
                        continue
                    if "result" in d or "error" in d:
                        data = d
        return data
    else:
        return resp.json()


# ---------------------------------------------------------------------------
# Progress reporting + progress-aware (idle) timeout
# ---------------------------------------------------------------------------

# Sync callback invoked for every notifications/progress received:
# (progress, total, message).
ProgressReporter = Callable[[float, Optional[float], Optional[str]], None]


def _progress_enabled_from_env(default: bool = True) -> bool:
    """Whether progress notifications should be requested/printed.

    On by default (used by the CLI ``call`` subcommand);
    ``MCP2CLI_PROGRESS=0`` (or false/no/off) disables it.
    """
    raw = os.environ.get("MCP2CLI_PROGRESS", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _stderr_progress_reporter(tool_id: str) -> ProgressReporter:
    """Build a reporter that prints each progress notification to stderr."""

    def _report(progress: float, total: Optional[float], message: Optional[str]) -> None:
        try:
            ts = time.strftime("%H:%M:%S")
            text = (message or "").strip()
            if not text:
                text = f"progress={progress}" + (f"/{total}" if total else "")
            print(f"[{ts}] \u23f3 {tool_id}: {text}", file=sys.stderr, flush=True)
        except Exception:
            pass

    return _report


def _resolve_progress_reporter(
    tool_id: str,
    progress: Optional[bool],
    on_progress: Optional[ProgressReporter],
) -> Optional[ProgressReporter]:
    """Pick the progress reporter for a call (an explicit callback wins)."""
    if on_progress is not None:
        return on_progress
    enabled = _progress_enabled_from_env() if progress is None else bool(progress)
    return _stderr_progress_reporter(tool_id) if enabled else None


async def _call_tool_live_progress_timed(
    endpoint: str,
    tool_id: str,
    arguments: Dict[str, Any],
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    on_progress: Optional[ProgressReporter] = None,
) -> Any:
    """``_call_tool_live`` wrapped in a progress-aware (idle) timeout.

    The deadline is ``timeout_seconds`` counted from the *last* progress
    notification (or from the start when none has arrived yet). Every incoming
    ``notifications/progress`` re-arms the timer, so a long-running tool that
    keeps reporting (e.g. a skills playbook with keep-alive beats) is never
    killed, while a tool that goes silent for ``timeout_seconds`` is bounded
    as before. With no progressToken in play (progress reporting disabled)
    this degrades to the plain wall-clock timeout.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    state = {"last_activity": started}

    async def _progress_cb(progress: float, total: Optional[float], message: Optional[str]) -> None:
        # Runs inside the SDK's receive-loop task: keep it cheap — record the
        # beat (re-arms the idle deadline) and let the sync reporter do I/O.
        state["last_activity"] = loop.time()
        if on_progress is not None:
            try:
                on_progress(progress, total, message)
            except Exception:
                pass

    call_task = asyncio.ensure_future(
        _call_tool_live(endpoint, tool_id, arguments, progress_callback=_progress_cb)
    )
    try:
        while True:
            remaining = (state["last_activity"] + timeout_seconds) - loop.time()
            if remaining <= 0:
                total_elapsed = loop.time() - started
                raise TimeoutError(
                    f"no progress for {timeout_seconds}s "
                    f"(idle timeout; total elapsed {total_elapsed:.0f}s)"
                )
            done, _pending = await asyncio.wait({call_task}, timeout=remaining)
            if call_task in done:
                return call_task.result()
            # This round hit the deadline, but a progress callback may have
            # re-armed it meanwhile — loop and recompute instead of giving up.
    except BaseException:
        if not call_task.done():
            call_task.cancel()
        with contextlib.suppress(BaseException):
            await call_task
        raise


def _flatten_exception(exc: BaseException) -> str:
    """Best-effort extract a readable message from an exception.

    ``str()`` of an :class:`ExceptionGroup` only prints the top label
    (e.g. ``unhandled errors in a TaskGroup (1 sub-exception)``),
    hiding the real cause one level down.  The gateway/MCP async layer wraps
    downstream failures in task groups, so this recurses into the leaves and
    returns the innermost (most specific) leaf as ``"TypeName: message"``.
    """

    def _leaves(e: BaseException):
        """Yield every non-group leaf, tracking nesting depth."""
        seen = set()
        stack = [(e, 0)]
        while stack:
            cur, depth = stack.pop()
            if isinstance(cur, (BaseExceptionGroup, ExceptionGroup)):
                if id(cur) in seen:
                    continue
                seen.add(id(cur))
                for sub in getattr(cur, "exceptions", ()):
                    stack.append((sub, depth + 1))
            else:
                yield cur, depth

    best = None
    best_depth = -1
    for leaf, depth in _leaves(exc):
        if depth > best_depth:
            best, best_depth = leaf, depth
    if best is not None:
        msg = (str(best).strip() or repr(best))
        return f"{type(best).__name__}: {msg}"
    return str(exc).strip() or type(exc).__name__


def _format_tool_result(out_obj: Any) -> str:
    """Compact rendering of a CallToolResult.

    The raw SDK repr carries the payload twice (content blocks and
    structuredContent) — and fastmcp wraps plain-string results into
    structuredContent={"result": msg}, so a single tool message would be
    printed multiple times.  Print the text payload once and append the
    structured form only when it actually adds information.
    """
    blocks = getattr(out_obj, "content", None) or []
    text = "\n".join(getattr(b, "text", "") for b in blocks
                      if getattr(b, "type", None) == "text" or hasattr(b, "text"))
    structured = getattr(out_obj, "structuredContent", None)
    parts = []
    if text.strip():
        parts.append(text)
    # structured adds info only when it is not just the text wrapped back
    if structured is not None and structured != {"result": text}:
        try:
            parts.append("structured: " + json.dumps(structured, ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(f"structured: {structured!r}")
    if not parts:
        return str(out_obj)  # nothing recognisable — fall back to the raw repr
    return "\n".join(parts)


def _format_tool_call_error(tool_id: str, endpoint: str, timeout_seconds: int, exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        # The idle-timer wrapper raises TimeoutError with a detail message
        # ("no progress for Ns ..."); a plain wall-clock one carries none.
        detail = str(exc).strip()
        log.warning(
            "Tool call timed out: tool=%s endpoint=%s timeout_seconds=%s (%s)",
            tool_id,
            endpoint,
            timeout_seconds,
            detail or "wall-clock",
        )
        return (
            f"Error calling tool '{tool_id}': {detail or f'timed out after {timeout_seconds}s'} "
            f"(wait timeout — not a transport failure; do not assume the command failed or re-run "
            f"mutating commands; check status / use terminal_wait if applicable)"
        )
    log.error(
        "Tool call failed: tool=%s endpoint=%s error=%s",
        tool_id,
        endpoint,
        exc,
    )
    return f"Error calling tool '{tool_id}': {_flatten_exception(exc)}"


async def call_tool_async(
    endpoint: str,
    tool_id: str,
    arguments: Dict[str, Any],
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    stdin: Optional[str] = None,
    progress: Optional[bool] = None,
    on_progress: Optional[ProgressReporter] = None,
) -> str:
    """Call a tool (async). ``timeout_seconds`` is an *idle* timeout: it is
    counted from the last progress notification, so a tool that keeps
    reporting progress (e.g. a long-running skills playbook) is never killed,
    while a silent one is bounded as before.

    Progress notifications are requested and (unless ``on_progress`` is given)
    printed to stderr when ``progress`` is true (default: enabled unless
    ``MCP2CLI_PROGRESS=0``). Disable with ``progress=False``/``--no-progress``
    — the timeout then becomes plain wall-clock.
    """
    call_args = dict(arguments) if arguments else {}
    if stdin is not None:
        call_args["stdin"] = stdin

    try:
        out_obj = await _call_tool_live_progress_timed(
            endpoint,
            tool_id,
            call_args,
            timeout_seconds=timeout_seconds,
            on_progress=_resolve_progress_reporter(tool_id, progress, on_progress),
        )
        return _format_tool_result(out_obj)
    except Exception as e:
        return _format_tool_call_error(tool_id, endpoint, timeout_seconds, e)


def call_tool(
    endpoint: str,
    tool_id: str,
    arguments: Dict[str, Any],
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    stdin: Optional[str] = None,
    progress: Optional[bool] = None,
    on_progress: Optional[ProgressReporter] = None,
) -> str:
    """Sync wrapper around :func:`call_tool_async` (same idle-timeout semantics).

    See :func:`call_tool_async` for the ``progress``/``on_progress`` options.
    """
    call_args = dict(arguments) if arguments else {}
    if stdin is not None:
        call_args["stdin"] = stdin

    try:
        out_obj = asyncio.run(
            _call_tool_live_progress_timed(
                endpoint,
                tool_id,
                call_args,
                timeout_seconds=timeout_seconds,
                on_progress=_resolve_progress_reporter(tool_id, progress, on_progress),
            )
        )
        return _format_tool_result(out_obj)
    except Exception as e:
        return _format_tool_call_error(tool_id, endpoint, timeout_seconds, e)


# ---------------------------------------------------------------------------
# Prompt listing / fetching (MCP `prompts/list` and `prompts/get`)
# ---------------------------------------------------------------------------

async def _fetch_prompt_list_live(endpoint: str) -> List[Dict[str, Any]]:
    """Fetch the prompt catalog from the endpoint (prompts/list)."""
    async with streamablehttp_client(endpoint) as _streams:
        # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields
        # (read, write) — unpack by position so both major lines work.
        r, w = _streams[0], _streams[1]
        async with ClientSession(r, w) as s:
            await s.initialize()
            prompts = (await s.list_prompts()).prompts or []
            out: List[Dict[str, Any]] = []
            for p in prompts:
                args = []
                for a in (getattr(p, "arguments", None) or []):
                    args.append(
                        {
                            "name": getattr(a, "name", ""),
                            "description": getattr(a, "description", "") or "",
                            "required": bool(getattr(a, "required", False)),
                        }
                    )
                out.append(
                    {
                        "name": getattr(p, "name", ""),
                        "description": getattr(p, "description", "") or "",
                        "arguments": args,
                    }
                )
            return out


def fetch_prompt_list(endpoint: str) -> List[Dict[str, Any]]:
    """List prompts available on the endpoint (sync wrapper)."""
    try:
        return asyncio.run(_fetch_prompt_list_live(endpoint))
    except Exception as e:
        log.warning("Could not fetch prompt list: endpoint=%s error=%s", endpoint, e)
        return []


def _format_prompt_content(content: Any) -> str:
    """Extract a prompt message's content as plain text."""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    # ResourceLink / EmbeddedResource and others without a `.text`.
    return repr(content)


def _format_prompt_result(result: Any) -> str:
    """Format a GetPromptResult into a human-readable prompt body."""
    description = getattr(result, "description", None) or ""
    messages = getattr(result, "messages", None) or []
    parts: List[str] = []
    if description:
        parts.append(f"# {description}")
    for msg in messages:
        role = getattr(msg, "role", "user")
        content = _format_prompt_content(getattr(msg, "content", None))
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


async def _get_prompt_live(
    endpoint: str,
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> str:
    """Fetch and render a single prompt by name (prompts/get)."""
    async with streamablehttp_client(endpoint) as _streams:
        # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields
        # (read, write) — unpack by position so both major lines work.
        r, w = _streams[0], _streams[1]
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.get_prompt(name, arguments)
            return _format_prompt_result(result)


def get_prompt(
    endpoint: str,
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a single prompt by name (sync wrapper)."""
    try:
        return asyncio.run(_get_prompt_live(endpoint, name, arguments))
    except Exception as e:
        return f"Error getting prompt '{name}': {e}"


# ---------------------------------------------------------------------------
# Output threshold / file saving
# ---------------------------------------------------------------------------

def _save_output_to_file(
    workspace_dir: Path,
    prefix: str,
    content: str,
) -> Path:
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        p = workspace_dir / f"{prefix}_{uuid.uuid4().hex}.txt"
        p.write_text(content, encoding="utf-8")
        return p
    except Exception as e:
        fallback_dir = Path(
            os.environ.get("MCP2CLI_FALLBACK_DIR", "/tmp/mcp2cli_workspace")
        )
        fallback_dir.mkdir(parents=True, exist_ok=True)
        p = fallback_dir / f"{prefix}_{uuid.uuid4().hex}.txt"
        p.write_text(content, encoding="utf-8")
        print(f"Warning: could not write to '{workspace_dir}': {e}. Saved to '{p}'.", file=sys.stderr)
        return p


def handle_large_output(
    out: str,
    is_error: bool = False,
    workspace_dir: Optional[Path] = None,
    threshold: int = DEFAULT_OUTPUT_THRESHOLD_CHARS,
) -> str:
    """If output exceeds threshold, save to file and return a summary with path."""
    if len(out) <= threshold:
        return out

    if workspace_dir is None:
        workspace_dir = _default_workspace_dir()

    prefix = "mcp2cli_error" if is_error else "mcp2cli_output"
    p = _save_output_to_file(workspace_dir, prefix, out)
    preview = out[:500].replace("\n", " ")
    return (
        f"Output saved to: {p}\n"
        f"Preview: {preview} ...\n"
        f"Copy/paste full output: cat \"{p}\""
    )
