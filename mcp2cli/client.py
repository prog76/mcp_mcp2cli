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
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

from mcp import ClientSession
try:
    # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    # mcp 2.x renamed to snake_case
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client


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

async def _fetch_tool_list_live(endpoint: str) -> List[Dict[str, Any]]:
    async with streamablehttp_client(endpoint) as _streams:
        # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields
        # (read, write) — unpack by position so both major lines work.
        r, w = _streams[0], _streams[1]
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            out: List[Dict[str, Any]] = []
            for t in tools:
                out.append(
                    {
                        "name": t.name,
                        "description": getattr(t, "description", "") or "",
                        "inputSchema": getattr(t, "inputSchema", None),
                        "outputSchema": getattr(t, "outputSchema", None),
                    }
                )
            return out


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
    async with streamablehttp_client(endpoint) as _streams:
        # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields
        # (read, write) — unpack by position so both major lines work.
        r, w = _streams[0], _streams[1]
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(tool_id, arguments,
                                     progress_callback=progress_callback)


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


def _format_tool_call_error(tool_id: str, endpoint: str, timeout_seconds: int, exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        log.warning(
            "Tool call timed out: tool=%s endpoint=%s timeout_seconds=%s",
            tool_id,
            endpoint,
            timeout_seconds,
        )
        return (
            f"Error calling tool '{tool_id}': timed out after {timeout_seconds}s "
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
) -> str:
    call_args = dict(arguments) if arguments else {}
    if stdin is not None:
        call_args["stdin"] = stdin

    try:
        out_obj = await asyncio.wait_for(_call_tool_live(endpoint, tool_id, call_args), timeout=timeout_seconds)
        out = str(out_obj)
        return out
    except Exception as e:
        return _format_tool_call_error(tool_id, endpoint, timeout_seconds, e)


def call_tool(
    endpoint: str,
    tool_id: str,
    arguments: Dict[str, Any],
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS,
    stdin: Optional[str] = None,
) -> str:
    call_args = dict(arguments) if arguments else {}
    if stdin is not None:
        call_args["stdin"] = stdin

    try:
        out_obj = asyncio.run(
            asyncio.wait_for(_call_tool_live(endpoint, tool_id, call_args), timeout=timeout_seconds)
        )
        out = str(out_obj)
        return out
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
