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

The client that loads this plugin creates a persistent per-plugin data
directory and exposes it to the server as `${PLUGIN_DATA}` (see `mcp.json`,
which sets the server's `cwd` and `LOCAL_DATA_DIR` to that same path).
Before it can serve any tools, the server needs a local user and a local
SQLite store in that directory.

**Automatic bootstrap is the default path.** This package's `mcp.json` sets
`OPENCRAB_BOOTSTRAP_ON_EMPTY=1`, so the very first stdio launch against an
empty `${PLUGIN_DATA}` creates the local user and an empty store by itself —
no manual step required. Every one of the following must hold before it
acts; any violation refuses startup with a dedicated error instead of
silently doing nothing:

- `OPENCRAB_BOOTSTRAP_ON_EMPTY` is exactly `"1"` (unset/`""`/`"0"` means off
  and behavior is unchanged from before this feature existed; any other
  value refuses startup rather than being silently ignored).
- `STORAGE_MODE` is exactly `local` — this package's own setting.
- `LOCAL_DATA_DIR` was set explicitly (env or an env file), is non-blank,
  and does not contain `?` (SQLAlchemy's sqlite URL parsing truncates
  there, which would split the checked/locked path from the one the
  database actually opens).
- The directory it names already exists. The server never creates the
  directory itself, only the database file inside one that's already
  there — creating `${PLUGIN_DATA}` remains this package's client's job.

When bootstrap actually creates the local user and store, the server prints
one line to stderr naming the path and the new user id. A directory that's
already provisioned (or a second, concurrent launch) causes no new
filesystem writes.

**Manual `opencrab init` is still a fully supported fallback and recovery
path** — use it if automatic bootstrap is turned off, if one of the gates
above can't be met in your launch context, or if you need an HTTP-facing
token (the automatic path never issues one, by design — issue one
afterward with `opencrab token issue <user_id>`):

```bash
cd <PLUGIN_DATA>
STORAGE_MODE=local LOCAL_DATA_DIR=<PLUGIN_DATA> LOCALCRAB_ENV_FILE=<PLUGIN_DATA>/localcrab.env opencrab init
```

Replace `<PLUGIN_DATA>` with the actual path your client reports for this
plugin (shown by a plugin listing/inspect command). Because this is the same
directory the server's `cwd` and `LOCAL_DATA_DIR` point at, anything this
step writes here is guaranteed to be visible to the running server.

**If the server's first launch fails with `Run 'opencrab init' first`**,
automatic bootstrap did not run for this launch — either the opt-in is off,
or one of the gates above rejected it (the error message names the data
root it resolved and mentions the `OPENCRAB_BOOTSTRAP_ON_EMPTY=1` opt-in).
Find the `PLUGIN_DATA` path your client shows for this plugin, run the
manual command above from inside it, then restart the server.

**A residual risk worth knowing about**: automatic bootstrap cannot tell a
*correct* `${PLUGIN_DATA}` from an *incorrect* one. If the client substitutes
the wrong (but existing, empty) directory, every gate above still passes and
an empty store gets created there silently, serving zero results with no
error. Manual provisioning already carries the same risk — a person could
run `opencrab init` against the wrong path just as easily — what changes is
that automatic bootstrap has no human decision point in between, so it acts
on the very first launch rather than waiting for someone to type the init
command. The stderr creation notice is the only mitigation; check it (or
your client's logs) if a freshly provisioned server behaves as if it has no
data.

## No secrets in this package

`mcp.json` contains no credentials, tokens, or operator-specific URLs — only
the four environment variables above, each derived from the `${PLUGIN_DATA}`
placeholder or a fixed constant. This package configures **stdio transport
only**. Remote (HTTP) access, per-user tokens, and CORS configuration are
operator decisions made outside this package — see the LocalCrab repository's
documentation if you need to expose the server remotely.

## Ambient environment: read this before troubleshooting

`opencrab serve` reads a number of environment variables directly from its
process environment beyond the four this package sets — embedding backend
selection, external store URLs, and various tuning knobs. Of the four this
package does set, `OPENCRAB_BOOTSTRAP_ON_EMPTY` is the one that decides
whether state (a local user and an empty store) gets created on first
launch — see "One-time provisioning" above. This plugin's `mcp.json` only
points the server at its own data directory; it does not, and cannot,
isolate the server from the rest of the host's ambient environment. If
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
