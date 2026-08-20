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

### Call a tool
```bash
mcp2cli call query_prometheus --args expr=up --args datasourceUid=<uid>
mcp2cli call query_prometheus --args-json '{"expr":"up"}' --args-json -
```

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

## How it works
`mcp2cli` discovers tools and calls the policy-proxy MCP endpoint directly
using the Python `mcp` SDK client. It does **not** shell out to an external
binary. All access control is enforced by policy-proxy; mcp2cli is a thin,
untrusted client.

## Package layout
- `mcp2cli.client` — reusable client library (no argparse): fetching, calling,
  caching, output threshold, tool-id resolution.
- `mcp2cli.cli`      — the `mcp2cli` command entry point.
