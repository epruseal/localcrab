# LocalCrab Agent Plugin

This package is the **Agent Plugins 1.0.0** manifest and MCP configuration for
LocalCrab, a local-first MetaOntology knowledge service (hybrid graph/vector/BM25
search over MCP). It lets an Agent Plugins-compatible client discover LocalCrab's
stdio MCP server and its usage skill without any manual MCP config editing.

## What this package provides — and what it does not

This package supplies **discovery and configuration only**:

- `plugin.json` — plugin identity (name, version, description).
- `mcp.json` — the `localcrab` stdio MCP server entry: command, args, working
  directory, and the environment variables needed to point the server at its
  own data directory.
- `skills/localcrab-query/SKILL.md` — usage guidance for the LocalCrab MCP tools.

It does **not** ship the LocalCrab runtime. The client launches `opencrab serve`
as a subprocess, so the `opencrab` command must already be installed and
resolvable on the `PATH` that the client uses to launch stdio subprocesses:

```bash
pip install opencrab   # or: pip install -e ".[dev]" from a LocalCrab checkout
```

If `opencrab` is not on that `PATH` (for example, it's only installed inside a
virtualenv the client doesn't activate), the server will fail to launch —
that's an installation problem, not a bug in this package.

## One-time provisioning

Before `opencrab serve` can start, its data directory needs to be initialized
once with `opencrab init`. The client that loads this plugin creates a
persistent per-plugin data directory and exposes it to the server as
`${PLUGIN_DATA}` (see `mcp.json`, which sets the server's `cwd` and
`LOCAL_DATA_DIR` to that same path). Provision that directory once, using the
exact variables the server itself will run with:

```bash
cd <PLUGIN_DATA>
STORAGE_MODE=local LOCAL_DATA_DIR=<PLUGIN_DATA> LOCALCRAB_ENV_FILE=<PLUGIN_DATA>/localcrab.env opencrab init
```

Replace `<PLUGIN_DATA>` with the actual path your client reports for this
plugin (shown by a plugin listing/inspect command). Because this is the same
directory the server's `cwd` and `LOCAL_DATA_DIR` point at, anything the
provisioning step writes here is guaranteed to be visible to the running server.

**If the server's first launch fails with `Run 'opencrab init' first`**, this
is the expected outcome of an unprovisioned data directory — it is not a
defect. Find the `PLUGIN_DATA` path your client shows for this plugin, run the
command above from inside it, then restart the server.

## No secrets in this package

`mcp.json` contains no credentials, tokens, or operator-specific URLs — only
the three environment variables above, each derived from the `${PLUGIN_DATA}`
placeholder or a fixed constant. This package configures **stdio transport
only**. Remote (HTTP) access, per-user tokens, and CORS configuration are
operator decisions made outside this package — see the LocalCrab repository's
documentation if you need to expose the server remotely.

## Ambient environment: read this before troubleshooting

`opencrab serve` reads a number of environment variables directly from its
process environment beyond the three this package sets — embedding backend
selection, external store URLs, and various tuning knobs. This plugin's
`mcp.json` only points the server at its own data directory; it does not, and
cannot, isolate the server from the rest of the host's ambient environment. If
the process that launches `opencrab serve` inherits your shell's or another
tool's environment, any `OPENCRAB_*`/`LOCALCRAB_*`/`VECTOR_*`/`EMBEDDING_*`
variables already set there still apply, and can change where the server reads
or writes data or which external services it contacts.

If you are troubleshooting behavior that doesn't match this package's
`mcp.json`, or you run this plugin from a shared or long-lived environment,
sanitize the ambient environment your plugin host launches subprocesses with —
or at minimum check for stray `OPENCRAB_*`/`LOCALCRAB_*` variables before
filing a bug. Note also that three variables (`OPENCRAB_API_KEY`,
`LOCALCRAB_MCP_TOKEN`, `LOCALCRAB_MCP_TOKEN_FILE`) will make the server refuse
to start at all if present in the ambient environment — this is a deliberate
safe failure for a retired shared-secret auth scheme, not a defect.
