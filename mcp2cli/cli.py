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
    _progress_enabled_from_env,
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

# Flags consumed by the wrapper itself — never converted into tool arguments
# by the direct ``--key=value`` shorthand.
_RESERVED_FLAGS = {
    # global parser options
    "--mcp2cli-bin", "--endpoint", "--cache-dir", "--cache-ttl-seconds",
    "--refresh", "--workspace-dir", "-h", "--help",
    # ``call`` subcommand options
    "--args", "--args-json", "--output-threshold-kb",
    "--output-threshold-chars", "--timeout-seconds", "--no-progress",
}
# Reserved flags that take a separate value token when written as ``--flag value``.
_RESERVED_VALUE_FLAGS = {
    "--mcp2cli-bin", "--endpoint", "--cache-dir", "--cache-ttl-seconds",
    "--workspace-dir", "--args", "--args-json", "--output-threshold-kb",
    "--output-threshold-chars", "--timeout-seconds",
}


def _rewrite_direct_kv(argv: List[str]) -> List[str]:
    """
    Rewrite direct ``--key=value`` (and bare ``--key``) tokens belonging to
    the ``call`` subcommand into ``--args key=value`` entries.

    Lets callers pass tool arguments as plain flags::

        mcp2cli call k8s_pods_get --context=devops --namespace=default
        mcp2cli call k8s_pods_get --query.text=hello   # dotted keys work
        mcp2cli call k8s_pods_get --verbose            # bare flag -> true

    Rules:
    - Only tokens after ``call`` are considered; everything before it and
      other subcommands pass through untouched.
    - Wrapper-owned flags (``_RESERVED_FLAGS``) pass through verbatim; when a
      reserved value-flag is written as ``--flag value``, the value token is
      kept together with it.
    - A bare ``--key`` becomes ``key=true``. Everything after a standalone
      ``--`` is left untouched (end-of-options marker).
    """
    out: List[str] = []
    i = 0
    n = len(argv)
    in_call = False
    while i < n:
        tok = argv[i]
        if tok in _RESERVED_VALUE_FLAGS and i + 1 < n:
            # Keep reserved flag and its separate value token together.
            out.append(tok)
            out.append(argv[i + 1])
            i += 2
            continue
        if tok in _RESERVED_FLAGS:
            out.append(tok)
            i += 1
            continue
        if not in_call:
            if tok.startswith("-"):
                out.append(tok)
                i += 1
                continue
            # First positional token: the subcommand.
            in_call = tok == "call"
            out.append(tok)
            i += 1
            if not in_call:
                out.extend(argv[i:])
                break
            continue
        # Inside `call` from here on.
        if tok == "--":
            out.extend(argv[i:])
            break
        if tok.startswith("--"):
            name, sep, inline_val = tok.partition("=")
            if name in _RESERVED_FLAGS:
                out.append(tok)
                i += 1
                continue
            if sep:
                out.extend(("--args", f"{name[2:]}={inline_val}"))
            else:
                out.extend(("--args", f"{name[2:]}=true"))
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


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


def _auth_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Collect the flags that override the OAuth environment config."""
    return {
        "client_id": getattr(args, "client_id", None),
        "scopes": getattr(args, "scopes", None),
        "redirect_host": getattr(args, "redirect_host", None),
        "redirect_port": getattr(args, "redirect_port", None),
        "callback_path": getattr(args, "callback_path", None),
        "login_timeout_seconds": getattr(args, "login_timeout", None),
    }


def cmd_auth_login(args: argparse.Namespace) -> int:
    """Print the login link, listen on loopback, store the grant."""
    from mcp2cli import auth

    endpoint = args.endpoint
    try:
        config = auth.load_config(_auth_overrides(args))
    except auth.OAuthError as e:
        print(f"{e}", file=sys.stderr)
        return 2

    try:
        tokens = auth.login(endpoint, config=config, open_browser=args.open_browser)
    except auth.OAuthError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1

    body = auth.status(endpoint)
    print(
        f"Signed in (client {tokens.client_id}, issuer {tokens.issuer}); "
        f"grant stored at {body['path']}",
        file=sys.stderr,
    )
    print(json.dumps(body, indent=2))
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    """Report the stored grant for an endpoint, or its absence."""
    from mcp2cli import auth

    body = auth.status(args.endpoint)
    print(json.dumps(body, indent=2))
    if not body["configured"]:
        print(
            f"No OAuth grant for {args.endpoint}.\n"
            f"  Mint one with: mcp2cli auth login --endpoint {args.endpoint}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_auth_logout(args: argparse.Namespace) -> int:
    """Delete the stored grant for an endpoint."""
    from mcp2cli import auth

    store = auth.TokenStore()
    removed = store.delete(args.endpoint)
    print(
        f"{'Removed' if removed else 'No'} stored grant for {args.endpoint} "
        f"({store.path_for(args.endpoint)})",
        file=sys.stderr,
    )
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    endpoint = args.endpoint
    cache_dir = Path(args.cache_dir)
    workdir = Path(args.workspace_dir)
    threshold = int(args.output_threshold_chars)
    if args.output_threshold_kb is not None:
        threshold = int(float(args.output_threshold_kb) * 1024)
    timeout_s = int(args.timeout_seconds)

    # Parse tool args: --args-json (raw JSON) forms the base, then --args and
    # direct --key=value flags (dotted key builder) are applied on top of it.
    tool_args: Dict[str, Any] = {}
    try:
        if args.args_json == "-":
            stdin_raw = sys.stdin.read()
            base: Dict[str, Any] = json.loads(stdin_raw) if stdin_raw.strip() else {}
        else:
            base = json.loads(args.args_json) if args.args_json else {}
    except json.JSONDecodeError as e:
        print(f"Invalid --args-json: {e.msg} (pos={e.pos}, line={getattr(e, 'lineno', None)})")
        return 2
    if not isinstance(base, dict):
        print("--args-json must be a JSON object.")
        return 2
    tool_args.update(base)

    for kv in args.args:
        try:
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
                _set_dotted_key(tool_args, key.strip(), stdin_content)
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
        progress=not args.no_progress,
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

    p_call = sub.add_parser(
        "call",
        help="Call a tool with --args-json, --args, or direct --key=value flags",
        epilog=(
            "examples:\n"
            "  mcp2cli call k8s_pods_get --context=devops --namespace=default\n"
            "  mcp2cli call k8s_pods_get --query.text=hello --labels[]=x\n"
            "  mcp2cli call k8s_pods_get --args context=devops --args name=@stdin <<< my-pod\n"
            "  echo '{\"context\":\"devops\"}' | mcp2cli call k8s_pods_get --args-json -\n"
            "\n"
            "notes:\n"
            "  - unknown --key=value flags become tool arguments; bare --key means\n"
            "    --key=true; dotted keys and key[]=value arrays are supported\n"
            "  - wrapper-owned flags (endpoint, cache, timeout, output-threshold,\n"
            "    args, args-json) are never treated as tool arguments\n"
            "  - --args-json builds the base; --args / direct keys override it\n"
            "  - progress notifications are requested and printed to stderr by\n"
            "    default ([HH:MM:SS] ⏳ tool: message); --timeout-seconds counts\n"
            "    from the last progress notification (idle timeout), so tools\n"
            "    that keep reporting are never killed; --no-progress turns both\n"
            "    off (plain wall-clock timeout)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_call.add_argument("tool_id", help="Tool name/id as shown by `list-tools`")
    p_call.add_argument(
        "--args-json",
        default="{}",
        help="JSON object of tool arguments (base layer; --args / direct keys override it)",
    )
    p_call.add_argument(
        "--args",
        action="append",
        default=[],
        metavar="key.subkey=value",
        help=(
            "Build nested JSON on the wrapper side using dotted keys (repeatable). "
            "Direct --key=value flags are converted to this automatically."
        ),
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
    p_call.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TOOL_TIMEOUT_SECONDS,
        help=(
            "Idle timeout: give up after this many seconds WITHOUT progress "
            "(default 120). Progress notifications (requested by default) reset "
            "the countdown, so tools that keep reporting are never killed."
        ),
    )
    p_call.add_argument(
        "--no-progress",
        action="store_true",
        default=not _progress_enabled_from_env(),
        help=(
            "Do not request/print progress notifications. With progress on, "
            "--timeout-seconds counts from the last progress notification "
            "(idle timeout); with it off, it is a plain wall-clock timeout."
        ),
    )
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

    p_auth = sub.add_parser(
        "auth",
        help="Manage the OAuth bearer grant for an endpoint",
        epilog=(
            "examples:\n"
            "  mcp2cli auth login --endpoint https://host/servers/<id>/mcp\n"
            "  mcp2cli auth status --endpoint https://host/servers/<id>/mcp\n"
            "  mcp2cli auth logout --endpoint https://host/servers/<id>/mcp\n"
            "\n"
            "notes:\n"
            "  - login prints an authorization URL and waits on a loopback\n"
            "    listener for the browser redirect (RFC 8252), so it must run\n"
            "    where the browser can reach the printed redirect URI\n"
            "  - the grant is cached per endpoint and used automatically by\n"
            "    every other subcommand; it is refreshed when expired\n"
            "  - client id and scopes come from MCP2CLI_OAUTH_CLIENT_ID /\n"
            "    MCP2CLI_OAUTH_SCOPES, or from the flags below\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)

    def _add_login_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--endpoint",
            default=os.environ.get("MCP_ENDPOINT", DEFAULT_ENDPOINT),
            help="MCP URL (the OAuth-protected resource)",
        )
        parser.add_argument(
            "--client-id",
            default=None,
            help="OAuth client id (default: $MCP2CLI_OAUTH_CLIENT_ID)",
        )
        parser.add_argument(
            "--scopes",
            default=None,
            help="Space-separated scopes (default: $MCP2CLI_OAUTH_SCOPES, else the resource's)",
        )
        parser.add_argument(
            "--redirect-host",
            default=None,
            help="Loopback callback host (default: localhost)",
        )
        parser.add_argument(
            "--redirect-port",
            type=int,
            default=None,
            help="Loopback callback port, 0 for an ephemeral one (default: 0)",
        )
        parser.add_argument(
            "--callback-path",
            default=None,
            help="Loopback callback path (default: /callback)",
        )
        parser.add_argument(
            "--login-timeout",
            type=int,
            default=None,
            help="Seconds to wait for the browser redirect (default: 300)",
        )

    p_auth_login = auth_sub.add_parser("login", help="Mint and store a grant for an endpoint")
    _add_login_flags(p_auth_login)
    p_auth_login.add_argument(
        "--open-browser",
        action="store_true",
        help="Also try to launch the authorization URL locally (default: only print it)",
    )
    p_auth_login.set_defaults(func=cmd_auth_login)

    p_auth_status = auth_sub.add_parser("status", help="Show the stored grant for an endpoint")
    _add_login_flags(p_auth_status)
    p_auth_status.set_defaults(func=cmd_auth_status)

    p_auth_logout = auth_sub.add_parser("logout", help="Delete the stored grant for an endpoint")
    _add_login_flags(p_auth_logout)
    p_auth_logout.set_defaults(func=cmd_auth_logout)

    argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(_rewrite_direct_kv(argv))
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
