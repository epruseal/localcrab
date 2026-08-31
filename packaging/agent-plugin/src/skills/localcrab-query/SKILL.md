---
name: localcrab-query
description: Guidance for choosing among LocalCrab's MetaOntology MCP tools (ontology_manifest, ontology_query, ontology_add_node/ontology_add_edge, pack_* and schema_pack_* family, harness_promotion_apply, tool_search) for hybrid graph/vector/BM25 knowledge work. Use when a task needs to explore LocalCrab's grammar, search or query an existing ontology, ingest nodes/edges/documents, or manage content or schema packs, and the exact tool set for the current connection has not been discovered yet.
license: MIT
---

## When to use this skill

Use this skill when a task involves a LocalCrab MCP server connection:
exploring its knowledge-graph grammar, running a hybrid search, adding
nodes/edges, or managing content or schema packs. It is a selection guide, not
a substitute for confirming what tools actually exist on this connection.

## Tool discovery is authoritative

The tools a LocalCrab MCP server exposes on a given connection — their names,
input schemas, and whether a specific tool is present at all — are determined
by that connection's `tools/list` response at runtime, not by this document.
Availability can vary by deployment: for example, some administrative tools
are hidden for non-local/remote principals. Always treat `tools/list` as the
source of truth for what is callable. This skill only helps you pick among
tools you've confirmed exist there. Where present, the `tool_search` tool
searches the current catalog by case-insensitive substring and returns a
catalog fingerprint (`catalog_version`) for staleness detection; its results
grant no execution rights.

## Choosing a tool by task

- **Understand the grammar first**: call `ontology_manifest` to get the full
  space/edge/node-type vocabulary before adding or querying anything
  unfamiliar. It is a pure, read-only call, safe to call speculatively.
- **Search existing knowledge**: call `ontology_query` for hybrid
  graph + vector + BM25 search. Prefer it over listing tools
  (`ontology_list_nodes`/`ontology_list_edges`) when the question is "what's
  relevant to X" rather than "give me everything of type Y". Use
  `ontology_get_node` when the exact node id is already known.
- **Add knowledge**: use `ontology_add_node` and `ontology_add_edge` to load
  new nodes and edges. Both validate against the grammar from
  `ontology_manifest`, so confirm the intended node/edge types against it
  first.
- **Analyze impact**: `ontology_impact` and `ontology_lever_simulate` once
  relevant nodes already exist in the graph.
- **Manage content packs**: `content_pack_list`, `pack_create`, `pack_ingest`
  (and, where present, `pack_fork`/`pack_publish`) group and scope ingested
  content. Reach for these when the task is about organizing or bulk-loading
  content rather than a single node or edge.
- **Manage schema packs**: `schema_pack_list`/`schema_pack_install`/
  `schema_pack_uninstall` add or remove domain-specific schema vocabulary —
  use before ingesting content that needs a schema not already in the base
  grammar.
- **Promotion**: `harness_promotion_apply` applies a CrabHarness
  PromotionPackage. It is an administrative operation and, per the discovery
  note above, may not appear in `tools/list` on every connection.

## One-time setup required

The LocalCrab MCP server needs its local data directory provisioned once
(`opencrab init`) before it can serve any of the tools above. If tool calls
fail immediately or the server won't start, provisioning likely hasn't run
yet — see this plugin package's README for the exact command. Don't work
around a missing-provisioning failure by guessing at file paths.
