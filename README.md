# mcp2cli

Command-line interface for calling MCP tools on a `policy-proxy` endpoint.

Install:
```bash
pip install git+https://github.com/prog76/mcp_mcp2cli.git
```

This gives you the `mcp2cli` executable and `python -m mcp2cli`.

## CLI usage

### List upstreams / servers
```bash
mcp2cli list-servers
```

### List tools for one or more server prefixes
```bash
mcp2cli list-tools grafana-infra,k8s
```

### Describe tools
```bash
mcp2cli describe k8s_pods_get,grafana-infra_query_prometheus
```

`describe` prints the tool's `tool_id`, `description`, and `parameters` (input
schema). When the serving compound advertises an `outputSchema`, the result also
includes an `output` field. Compounds with `schema: minimal` (browser-facing)
strip `outputSchema`, so `describe` there omits `output` — controlled by
`deploy/config/compounds.yaml`.

### Call a tool
```bash
mcp2cli call query_prometheus --args expr=up --args datasourceUid=<uid>
mcp2cli call query_prometheus --args-json '{"expr":"up"}' --args-json -
mcp2cli call query_prometheus --expr=up --datasourceUid=<uid>
mcp2cli call query_prometheus --verbose            # bare flag -> {"verbose": true}
```

Direct `--key=value` flags are rewritten to `--args` automatically (dotted keys
and `key[]=value` arrays work). `--args-json` forms the base arguments object;
`--args` and direct keys are applied on top of it and override its values.
Wrapper-owned flags (`--endpoint`, `--timeout-seconds`, `--output-threshold-*`,
`--args`, `--args-json`) are never treated as tool arguments.

### Progress reporting (on by default)
`mcp2cli call` requests MCP progress notifications and prints every one to
stderr as `[HH:MM:SS] ⏳ <tool>: <message>`:

```text
$ mcp2cli call skills_ipybox_run_skill --args-json '{"skill":"k8s-memory-dump","args":{...}}'
[10:02:11] ⏳ skills_ipybox_run_skill: creating debug container memdump-debug (12s)
[10:02:21] ⏳ skills_ipybox_run_skill: collecting gcdump from pid 1 (22s)
```

`--timeout-seconds` (default 120) is an **idle** timeout: the countdown restarts
on every progress notification, so a tool that keeps reporting progress is
never killed no matter how long it runs, while a tool that goes silent for the
full period still times out. Disable with `--no-progress` or
`MCP2CLI_PROGRESS=0` — the timeout then becomes plain wall-clock.

### List prompts
```bash
mcp2cli list-prompts
```

### Get a prompt (rendered)
```bash
mcp2cli get-prompt infra_bootstrap
mcp2cli get-prompt myprompt --args-json '{"name":"bob"}'
mcp2cli get-prompt myprompt --args-json -   # read args JSON from stdin
```

### `@stdin` marker
Pipe content into `@stdin`-marked args:
```bash
cat file | mcp2cli call vscode_terminal_exec \
  --args workspaceId=... \
  --args stdin='@stdin'
```

## Environment variables (optional)
- `MCP_ENDPOINT` — target MCP URL (default `http://localhost:8000/mcp/full`)
- `MCP2CLI_CACHE_DIR` — tool-list cache directory
- `MCP2CLI_CACHE_TTL` — cache TTL in seconds
- `MCP2CLI_WORKSPACE_DIR` — where large outputs are saved
- `MCP2CLI_PROGRESS` — set to `0` to disable progress reporting (default on)
- `MCP_TOOL_TIMEOUT_SECONDS` — default call timeout in seconds (default 120;
  counts from the last progress notification, see above)

## How it works
`mcp2cli` discovers tools and calls the policy-proxy MCP endpoint directly
using the Python `mcp` SDK client. It does **not** shell out to an external
binary. All access control is enforced by policy-proxy; mcp2cli is a thin,
untrusted client.

## Package layout
- `mcp2cli.client` — reusable client library (no argparse): fetching, calling,
  caching, output threshold, tool-id resolution.
- `mcp2cli.cli`      — the `mcp2cli` command entry point.
