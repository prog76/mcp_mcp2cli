#!/usr/bin/env python3
"""
mcp2cli.cli

Adapted from the policy-proxy MCP-to-CLI wrapper (originally
``mcp/mcp2cli_wrapper.py``). All access control still happens in
policy-proxy; this wrapper only calls MCP tools via the policy-proxy
endpoint provided by MCP_ENDPOINT.

Changes from the original:
- No more sys.path hacks for /app — this is a real installed package.
- Imports are relative to the ``mcp2cli`` package.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp2cli.client import (
    DEFAULT_ENDPOINT,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_OUTPUT_THRESHOLD_CHARS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    _default_cache_dir,
    _default_workspace_dir,
    _split_server_prefix,
    call_tool,
    fetch_prompt_list,
    fetch_tool_list,
    format_tool_schema,
    get_prompt,
    handle_large_output,
    resolve_tool_id,
)

DEFAULT_MCP2CLI_BIN = "mcp2cli"


def _set_dotted_key(root: dict, dotted_key: str, value: Any) -> None:
    """
    Set ``root[key1][key2]``... using a dotted key.

    Supports:
    - nested objects via dots: ``a.b.c=1``
    - arrays via ``[]`` suffix: ``labels[]=x``  (appends)
    """
    parts = [p for p in dotted_key.split(".") if p != ""]
    cur: Any = root
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        is_array = part.endswith("[]")
        key = part[:-2] if is_array else part

        if is_array:
            if key == "":
                raise ValueError(f"Invalid array key in --args: {dotted_key}")
            if key not in cur or not isinstance(cur.get(key), list):
                cur[key] = []
            if is_last:
                cur[key].append(value)
            else:
                raise ValueError(
                    f"Unsupported array nesting in --args key (needs explicit JSON): {dotted_key}"
                )
        else:
            if is_last:
                cur[key] = value
            else:
                nxt = cur.get(key)
                if nxt is None:
                    cur[key] = {}
                    nxt = cur[key]
                if not isinstance(nxt, dict):
                    raise ValueError(f"Key collision in --args at '{key}' for '{dotted_key}'")
                cur = nxt


def cmd_list_tools(args: argparse.Namespace) -> int:
    endpoint = args.endpoint
    cache_dir = Path(args.cache_dir)
    tools = fetch_tool_list(
        endpoint=endpoint,
        cache_dir=cache_dir,
        cache_ttl_s=args.cache_ttl_seconds,
        refresh=args.refresh,
    )

    servers = [s.strip() for s in args.servers.split(",") if s.strip()]
    if not servers:
        print("No servers provided. Example: list-tools grafana-infra,k8s")
        return 2

    matched = []
    for t in tools:
        tool_name = t.get("name") or t.get("tool_id") or ""
        if not tool_name:
            continue
        srv = _split_server_prefix(tool_name)
        if srv in servers:
            matched.append((srv, tool_name, t.get("description") or ""))

    matched.sort(key=lambda x: (x[0], x[1]))
    for srv in servers:
        print(f"{srv}/")
        for m_srv, tool_name, desc in matched:
            if m_srv != srv:
                continue
            desc_short = (desc or "").strip().splitlines()[0] if desc else ""
            print(f"- {tool_name}  {desc_short}")
        print()
    return 0


def cmd_list_servers(args: argparse.Namespace) -> int:
    endpoint = args.endpoint
    cache_dir = Path(args.cache_dir)
    tools = fetch_tool_list(
        endpoint=endpoint,
        cache_dir=cache_dir,
        cache_ttl_s=args.cache_ttl_seconds,
        refresh=args.refresh,
    )

    servers: set[str] = set()
    for t in tools:
        tool_name = t.get("name") or t.get("tool_id") or ""
        if not tool_name:
            continue
        servers.add(_split_server_prefix(tool_name))

    for s in sorted(servers):
        print(s)

    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    endpoint = args.endpoint
    cache_dir = Path(args.cache_dir)
    tools = fetch_tool_list(
        endpoint=endpoint,
        cache_dir=cache_dir,
        cache_ttl_s=args.cache_ttl_seconds,
        refresh=args.refresh,
    )

    ids = [s.strip() for s in args.tool_ids.split(",") if s.strip()]
    if not ids:
        print("No tool_ids provided. Example: describe k8s_pods_get,grafana-infra_query_prometheus")
        return 2

    by_name = {}
    for t in tools:
        name = t.get("name") or t.get("tool_id")
        if name:
            by_name[name] = t

    for tool_id in ids:
        tool = by_name.get(tool_id)
        if tool is None:
            # Allow describing by unprefixed "real" tool name, resolving to the proxied id via suffix matching.
            needle = f"_{tool_id}"
            matches = [n for n in by_name.keys() if n.endswith(needle)]
            if len(matches) == 1:
                tool = by_name[matches[0]]
            elif not matches:
                print(f"Tool not found in list: {tool_id}")
                continue
            else:
                print(f"Ambiguous real tool name '{tool_id}'. Matches:")
                for m in matches:
                    print(f"- {m}")
                continue
        print(f"=== {tool_id} ===")
        # Pretty print best-effort schema.
        print(format_tool_schema(tool))
        print()

    return 0


def cmd_call(args: argparse.Namespace) -> int:
    endpoint = args.endpoint
    cache_dir = Path(args.cache_dir)
    workdir = Path(args.workspace_dir)
    threshold = int(args.output_threshold_chars)
    if args.output_threshold_kb is not None:
        threshold = int(float(args.output_threshold_kb) * 1024)
    timeout_s = int(args.timeout_seconds)

    # Parse tool args: either --args-json (raw JSON) OR --args (dotted key builder).
    if args.args:
        tool_args: Dict[str, Any] = {}
        try:
            for kv in args.args:
                if "=" not in kv:
                    raise ValueError(f"Expected key=value, got: {kv}")
                key, raw_val = kv.split("=", 1)
                # Simple inline scalar parser — mcp_client_lib itself doesn't
                # expose a standalone scalar parser, so we keep it local here.
                v = raw_val.strip()
                if v == "null":
                    parsed: Any = None
                elif v == "true":
                    parsed = True
                elif v == "false":
                    parsed = False
                else:
                    try:
                        parsed = int(v)
                    except Exception:
                        try:
                            parsed = float(v)
                        except Exception:
                            parsed = v
                _set_dotted_key(tool_args, key.strip(), parsed)
        except Exception as e:
            print(f"Invalid --args: {e}")
            return 2
    else:
        # Parse args-json. We expect it to be a flat JSON object of tool parameters.
        try:
            if args.args_json == "-":
                stdin_raw = sys.stdin.read()
                tool_args = json.loads(stdin_raw) if stdin_raw.strip() else {}
            else:
                tool_args = json.loads(args.args_json) if args.args_json else {}
        except json.JSONDecodeError as e:
            print(f"Invalid --args-json: {e.msg} (pos={e.pos}, line={getattr(e, 'lineno', None)})")
            return 2
        if not isinstance(tool_args, dict):
            print("--args-json must be a JSON object.")
            return 2

    # If stdin is piped (not a tty) and --args-json "-" was NOT used, slurp stdin
    # and either substitute an explicit @stdin marker in --args, or inject it
    # as the `stdin` tool parameter.
    stdin_content = ""
    if not sys.stdin.isatty() and args.args_json != "-":
        try:
            stdin_content = sys.stdin.read()
        except Exception:
            pass

    if stdin_content and args.args:
        substituted = False
        for kv in args.args:
            if "=" not in kv:
                continue
            key, raw_val = kv.split("=", 1)
            if raw_val.strip() == "@stdin":
                tool_args[key.strip()] = stdin_content
                substituted = True
        if not substituted:
            tool_args["stdin"] = stdin_content
    elif stdin_content:
        tool_args["stdin"] = stdin_content

    # Resolve tool id using shared library
    provided_tool_id = args.tool_id
    tools = fetch_tool_list(
        endpoint=endpoint,
        cache_dir=cache_dir,
        cache_ttl_s=args.cache_ttl_seconds,
        refresh=args.refresh,
    )
    tool_names = [t.get("name") for t in tools if t.get("name")]
    try:
        resolved_tool_id = resolve_tool_id(provided_tool_id, tool_names)
    except ValueError as e:
        print(str(e))
        print("Tip: run `mcp2cli list-tools <server-prefixes>` to see exact tool ids.")
        return 2

    out = call_tool(
        endpoint=endpoint,
        tool_id=resolved_tool_id,
        arguments=tool_args,
        timeout_seconds=timeout_s,
    )

    # Check for error marker in output
    is_error = out.startswith("Error calling tool")
    result = handle_large_output(out, is_error=is_error, workspace_dir=workdir, threshold=threshold)
    print(result)
    return 0 if not is_error else 1


def cmd_list_prompts(args: argparse.Namespace) -> int:
    """List prompts available on the endpoint (prompts/list)."""
    endpoint = args.endpoint
    prompts = fetch_prompt_list(endpoint)
    if not prompts:
        print("No prompts available on this endpoint.")
        print("Tip: run `mcp2cli list-servers` to check the endpoint, or `mcp2cli list-tools <prefix>`.")
        return 0

    for p in prompts:
        name = p.get("name") or ""
        desc = (p.get("description") or "").strip()
        first_line = desc.splitlines()[0] if desc else ""
        line = f"- {name}"
        if first_line:
            line += f"  {first_line}"
        print(line)
        args_list = [a.get("name", "") for a in (p.get("arguments") or [])]
        if args_list:
            print(f"    arguments: {', '.join(args_list)}")
    return 0


def cmd_get_prompt(args: argparse.Namespace) -> int:
    """Render a single prompt by name (prompts/get)."""
    endpoint = args.endpoint
    name = args.prompt_name

    arguments: Optional[Dict[str, Any]] = None
    if args.args_json:
        try:
            raw = sys.stdin.read() if args.args_json == "-" else args.args_json
            parsed = json.loads(raw) if raw.strip() else {}
            if not isinstance(parsed, dict):
                print("--args-json must be a JSON object.")
                return 2
            arguments = parsed
        except json.JSONDecodeError as e:
            print(f"Invalid --args-json: {e.msg} (pos={e.pos}, line={getattr(e, 'lineno', None)})")
            return 2

    out = get_prompt(endpoint, name, arguments)
    is_error = out.startswith("Error getting prompt")
    result = handle_large_output(out, is_error=is_error, workspace_dir=Path(args.workspace_dir), threshold=int(args.output_threshold_chars))
    print(result)
    return 0 if not is_error else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="mcp2cli wrapper for policy-proxy endpoints")
    # Kept for backwards compatibility with older wrapper versions.
    parser.add_argument("--mcp2cli-bin", default=DEFAULT_MCP2CLI_BIN, help=argparse.SUPPRESS)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("MCP_ENDPOINT", DEFAULT_ENDPOINT),
        help="MCP URL (policy-proxy endpoint)",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get("MCP2CLI_CACHE_DIR", str(_default_cache_dir())),
        help="Directory for tool-list cache",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=int(os.environ.get("MCP2CLI_CACHE_TTL", DEFAULT_CACHE_TTL_SECONDS)),
        help="Cache TTL in seconds",
    )
    parser.add_argument("--refresh", action="store_true", help="Bypass cache for list/describe")
    parser.add_argument(
        "--workspace-dir",
        default=os.environ.get("MCP2CLI_WORKSPACE_DIR", str(_default_workspace_dir())),
        help="Directory for large output files",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-tools", help="List tools for one or more server prefixes")
    p_list.add_argument("servers", help="Comma-separated server prefixes (derived from tool-name prefixes)")
    p_list.set_defaults(func=cmd_list_tools)

    p_ls = sub.add_parser("list-servers", help="List available server prefixes (discovered from tool ids)")
    p_ls.set_defaults(func=cmd_list_servers)

    p_desc = sub.add_parser("describe", help="Describe one or more tool ids")
    p_desc.add_argument("tool_ids", help="Comma-separated tool ids")
    p_desc.set_defaults(func=cmd_describe)

    p_call = sub.add_parser("call", help="Call a tool with --args-json")
    p_call.add_argument("tool_id", help="Tool name/id as shown by `list-tools`")
    p_call.add_argument("--args-json", default="{}", help="JSON object of tool arguments")
    p_call.add_argument(
        "--args",
        action="append",
        default=[],
        metavar="key.subkey=value",
        help="Build nested JSON on the wrapper side using dotted keys (repeatable).",
    )
    p_call.add_argument(
        "--output-threshold-kb",
        default=None,
        help="Override output threshold for saving to file, in KB (e.g. 50 means ~50KB).",
    )
    p_call.add_argument(
        "--output-threshold-chars",
        type=int,
        default=int(os.environ.get("MCP2CLI_OUTPUT_THRESHOLD_CHARS", DEFAULT_OUTPUT_THRESHOLD_CHARS)),
    )
    p_call.add_argument("--timeout-seconds", type=int, default=DEFAULT_TOOL_TIMEOUT_SECONDS)
    p_call.set_defaults(func=cmd_call)

    p_list_prompts = sub.add_parser("list-prompts", help="List prompts available on the endpoint")
    p_list_prompts.set_defaults(func=cmd_list_prompts)

    p_get_prompt = sub.add_parser("get-prompt", help="Render a single prompt by name")
    p_get_prompt.add_argument("prompt_name", help="Prompt name as shown by `list-prompts`")
    p_get_prompt.add_argument(
        "--args-json",
        default=None,
        help='JSON object of prompt arguments, or "-" to read from stdin (default: no arguments)',
    )
    p_get_prompt.add_argument(
        "--output-threshold-chars",
        type=int,
        default=int(os.environ.get("MCP2CLI_OUTPUT_THRESHOLD_CHARS", DEFAULT_OUTPUT_THRESHOLD_CHARS)),
    )
    p_get_prompt.set_defaults(func=cmd_get_prompt)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
