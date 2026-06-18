"""
OpenCrab CLI — Click command interface.

Commands:
  init      Create .env from template
  serve     Start the MCP server (stdio default, or --transport http)
  status    Check all store connections
  ingest    Ingest files from a path
  extract   LLM-extract nodes/edges from files into the graph
  query     Run a hybrid query
  manifest  Print the MetaOntology grammar
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Main group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="opencrab")
def main() -> None:
    """OpenCrab — MetaOntology MCP server. Carcinization is inevitable."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--force", is_flag=True, default=False, help="Overwrite existing .env file."
)
def init(force: bool) -> None:
    """Create a .env file from .env.example and show startup instructions."""
    here = Path.cwd()
    src = here / ".env.example"
    dst = here / ".env"

    # Search for .env.example up from cwd (handles running from subdirs)
    if not src.exists():
        pkg_dir = Path(__file__).parent.parent
        src = pkg_dir / ".env.example"

    if dst.exists() and not force:
        console.print(
            "[yellow].env already exists. Use --force to overwrite.[/yellow]"
        )
    else:
        if src.exists():
            shutil.copy(src, dst)
            console.print(f"[green]Created {dst}[/green]")
        else:
            _write_default_env(dst)
            console.print(f"[green]Created default {dst}[/green]")

    console.print(
        Panel(
            "[bold]Next steps:[/bold]\n\n"
            "1. Optional: edit [cyan].env[/cyan] to change LOCAL_DATA_DIR.\n"
            "2. Add to Claude Code MCP config:\n"
            "   [cyan]claude mcp add opencrab -- opencrab serve[/cyan]\n"
            "3. Seed example data:\n"
            "   [cyan]python scripts/seed_ontology.py[/cyan]",
            title="OpenCrab Setup",
            border_style="green",
        )
    )


def _write_default_env(path: Path) -> None:
    content = """\
LOCAL_DATA_DIR=/home/asdf/.openclaw/workspace/data/localcrab
CHROMA_COLLECTION=opencrab_vectors
MCP_SERVER_NAME=localcrab
MCP_SERVER_VERSION=0.1.0-localcrab
LOG_LEVEL=INFO
"""
    path.write_text(content)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="stdio: local Claude Code integration. http: Streamable HTTP MCP.",
)
@click.option("--host", default=None, help="HTTP bind host (http only). Defaults to config.")
@click.option("--port", default=None, type=int, help="HTTP bind port (http only). Defaults to config.")
@click.option(
    "--auth-token",
    default=None,
    help="Bearer token for the HTTP transport. Unset + no token source = no auth.",
)
@click.option("--auth-token-file", default=None, help="Path to a file holding the bearer token (http only).")
def serve(
    transport: str,
    host: str | None,
    port: int | None,
    auth_token: str | None,
    auth_token_file: str | None,
) -> None:
    """Start the OpenCrab MCP server (stdio by default, or Streamable HTTP)."""
    if transport == "stdio":
        # Suppress all non-error logging to keep the stdio JSON-RPC channel clean.
        logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
        from opencrab.mcp.server import MCPServer

        MCPServer().run()
        return

    # transport == "http"
    from opencrab.config import get_settings
    from opencrab.mcp.http_app import _resolve_token, create_app

    cfg = get_settings()
    bind_host = host or cfg.mcp_http_host
    bind_port = port or cfg.mcp_http_port
    token = _resolve_token(auth_token, auth_token_file)

    # HTTP can log normally — stdout is not a protocol channel here.
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        stream=sys.stderr,
    )
    mode = "auth" if token else "OPEN(no-auth)"
    console.print(
        f"[green]OpenCrab MCP (Streamable HTTP, {mode}) → http://{bind_host}:{bind_port}/mcp[/green]"
    )

    import uvicorn

    # Single worker: the chroma PersistentClient is single-process only.
    uvicorn.run(
        create_app(auth_token=token),
        host=bind_host,
        port=bind_port,
        workers=1,
        log_level=cfg.log_level.lower(),
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command()
def status() -> None:
    """Check connectivity to all configured data stores."""
    from opencrab.config import get_settings
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    cfg = get_settings()
    mode_label = "[bold cyan]LOCAL MODE[/bold cyan]"
    storage_loc = cfg.local_data_dir
    console.print(f"\n{mode_label} - storage at: {storage_loc}\n")

    graph  = make_graph_store(cfg)
    vector = make_vector_store(cfg)
    docs   = make_doc_store(cfg)
    sql    = make_sql_store(cfg)

    store_rows: list[tuple[str, str, Any]] = [
        ("Graph (SQLite)",    cfg.local_data_dir + "/graph.db",    graph),
        ("Vector (ChromaDB)", cfg.local_data_dir + "/chroma",      vector),
        ("Docs (JSON files)", cfg.local_data_dir + "/docs",        docs),
        ("SQL (SQLite)",      cfg.local_data_dir + "/opencrab.db", sql),
    ]

    table = Table(title="OpenCrab Store Status", show_header=True, header_style="bold cyan")
    table.add_column("Store", style="bold")
    table.add_column("Path / URL")
    table.add_column("Status")

    for name, url, store in store_rows:
        if store.available:
            try:
                ok = store.ping()
                status_text = "[green]OK[/green]" if ok else "[yellow]CONNECTED (ping failed)[/yellow]"
            except Exception:
                status_text = "[yellow]CONNECTED[/yellow]"
        else:
            status_text = "[red]UNAVAILABLE[/red]"
        table.add_row(name, url, status_text)

    console.print(table)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--recursive", "-r", is_flag=True, default=False)
@click.option("--extension", "-e", default=".txt,.md,.py", show_default=True)
@click.option(
    "--pack-id",
    "pack_id",
    default=None,
    help="Attach pack_id metadata to ingested docs. Inferred from path when omitted.",
)
def ingest(path: str, recursive: bool, extension: str, pack_id: str | None) -> None:
    """Ingest files from PATH into the ontology vector store."""
    from opencrab.config import get_settings
    from opencrab.ontology.pack_provenance import infer_pack_id_from_path
    from opencrab.ontology.query import HybridQuery
    from opencrab.stores.factory import make_doc_store, make_graph_store, make_vector_store

    cfg = get_settings()
    chroma = make_vector_store(cfg)
    neo4j = make_graph_store(cfg)
    mongo = make_doc_store(cfg)
    hybrid = HybridQuery(chroma, neo4j)

    extensions = [e.strip() for e in extension.split(",")]
    root = Path(path)
    files = list(root.rglob("*")) if recursive else list(root.iterdir())
    files = [f for f in files if f.is_file() and f.suffix in extensions]

    if not files:
        console.print(f"[yellow]No files with extensions {extensions} found in {path}[/yellow]")
        return

    console.print(f"[cyan]Ingesting {len(files)} file(s)...[/cyan]")

    ok_count = 0
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            source_id = str(file.resolve())
            meta = {"source_path": str(file), "extension": file.suffix}

            effective_pack = pack_id or infer_pack_id_from_path(file.resolve())
            if effective_pack:
                meta["pack_id"] = effective_pack

            hybrid.ingest(text=text, source_id=source_id, metadata=meta)

            if mongo.available:
                mongo.upsert_source(source_id, text, meta)

            ok_count += 1
            tag = f" pack={effective_pack}" if effective_pack else ""
            console.print(f"  [green]OK[/green] {file.name} ({len(text)} chars){tag}")
        except Exception as exc:
            console.print(f"  [red]FAIL[/red] {file.name}: {exc}")

    console.print(f"\n[bold green]Ingested {ok_count}/{len(files)} files.[/bold green]")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--recursive", "-r", is_flag=True, default=False)
@click.option("--extension", "-e", default=".md,.txt,.py", show_default=True)
@click.option("--model", default="claude-haiku-4-5-20251001", show_default=True, help="Claude model for extraction.")
@click.option("--dry-run", is_flag=True, default=False, help="Extract but do not write to stores.")
@click.option("--api-key", default=None, envvar="ANTHROPIC_API_KEY", help="Anthropic API key.")
def extract(
    path: str,
    recursive: bool,
    extension: str,
    model: str,
    dry_run: bool,
    api_key: str | None,
) -> None:
    """LLM-extract ontology nodes/edges from files and write to the graph."""
    from opencrab.config import get_settings
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.extractor import LLMExtractor
    from opencrab.stores.factory import make_doc_store, make_graph_store, make_sql_store

    if not api_key:
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]ANTHROPIC_API_KEY not set. Pass --api-key or set the env var.[/red]")
        raise SystemExit(1)

    cfg = get_settings()
    graph = make_graph_store(cfg)
    doc = make_doc_store(cfg)
    sql = make_sql_store(cfg)
    builder = OntologyBuilder(graph, doc, sql)
    extractor = LLMExtractor(api_key=api_key, model=model)

    extensions = [e.strip() for e in extension.split(",")]
    root = Path(path)
    files = list(root.rglob("*")) if recursive else list(root.iterdir())
    files = [f for f in files if f.is_file() and f.suffix in extensions]

    if not files:
        console.print(f"[yellow]No files with extensions {extensions} found.[/yellow]")
        return

    console.print(f"[cyan]Extracting ontology from {len(files)} file(s)...[/cyan]")

    total_nodes = 0
    total_edges = 0
    total_errors = 0

    for file in files:
        console.print(f"\n[bold]{file.name}[/bold]")
        try:
            result = extractor.extract_from_file(file)
            console.print(f"  nodes={len(result.nodes)} edges={len(result.edges)}", end="")
            if result.errors:
                console.print(f" [yellow]warn={len(result.errors)}[/yellow]")
                total_errors += len(result.errors)
            else:
                console.print()

            if not dry_run:
                for node in result.nodes:
                    try:
                        builder.add_node(
                            space=node.space,
                            node_type=node.node_type,
                            node_id=node.node_id,
                            properties=node.properties,
                        )
                    except Exception as exc:
                        console.print(f"    [red]node {node.node_id}: {exc}[/red]")

                for edge in result.edges:
                    try:
                        builder.add_edge(
                            from_space=edge.from_space,
                            from_id=edge.from_id,
                            relation=edge.relation,
                            to_space=edge.to_space,
                            to_id=edge.to_id,
                            properties=edge.properties,
                        )
                    except Exception as exc:
                        console.print(f"    [yellow]edge {edge.from_id}→{edge.to_id}: {exc}[/yellow]")

            total_nodes += len(result.nodes)
            total_edges += len(result.edges)
        except Exception as exc:
            console.print(f"  [red]FAIL: {exc}[/red]")
            total_errors += 1

    mode_label = "[dim](dry-run)[/dim]" if dry_run else ""
    console.print(
        f"\n[bold green]Done {mode_label}[/bold green] — "
        f"nodes={total_nodes} edges={total_edges} errors={total_errors}"
    )


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


@main.command()
@click.argument("question")
@click.option("--spaces", "-s", default=None, help="Comma-separated space IDs to filter.")
@click.option("--limit", "-n", default=10, show_default=True)
@click.option("--json-output", is_flag=True, default=False, help="Output raw JSON (legacy list format).")
@click.option(
    "--pack-id",
    "pack_ids",
    multiple=True,
    help="Restrict the query to one or more pack IDs. May be repeated.",
)
@click.option(
    "--auto-pack",
    is_flag=True,
    default=False,
    help="Pick the most relevant pack from the local registry (deterministic scoring).",
)
@click.option(
    "--include-unpackaged",
    is_flag=True,
    default=False,
    help="Include items with no pack_id (legacy data). Only meaningful with --pack-id.",
)
@click.option(
    "--show-pack/--hide-pack",
    default=True,
    help="Show pack provenance in human output.",
)
@click.option(
    "--json-envelope",
    is_flag=True,
    default=False,
    help="Output an envelope JSON {question, selected_packs, pack_filter, results}.",
)
def query(
    question: str,
    spaces: str | None,
    limit: int,
    json_output: bool,
    pack_ids: tuple[str, ...],
    auto_pack: bool,
    include_unpackaged: bool,
    show_pack: bool,
    json_envelope: bool,
) -> None:
    """Run a hybrid query and print results."""
    from opencrab.config import get_settings
    from opencrab.ontology.query import HybridQuery
    from opencrab.services.pack_selection import cli_warning_text, resolve_packs
    from opencrab.stores.factory import make_doc_store, make_graph_store, make_vector_store

    cfg = get_settings()
    chroma = make_vector_store(cfg)
    neo4j = make_graph_store(cfg)
    docs = make_doc_store(cfg)
    hybrid = HybridQuery(chroma, neo4j)
    if docs.available:
        hybrid._doc_store = docs  # noqa: SLF001 — same wiring tools.py uses

    space_filter = [s.strip() for s in spaces.split(",")] if spaces else None

    selection = resolve_packs(
        question,
        list(pack_ids) if pack_ids else None,
        auto_pack,
        include_unpackaged,
        cfg.local_data_dir,
        raise_on_error=True,
    )
    effective_pack_ids = selection.effective_pack_ids
    selected_packs = selection.selected_packs
    auto_pack = selection.auto_pack_active
    for warning in selection.warnings:
        click.echo(cli_warning_text(warning), err=True)
    for sp in selected_packs:
        click.echo(
            f"info: auto-pack selected '{sp['pack_id']}' "
            f"(score={sp['score']:.1f}, matched={sp['matched'][:6]})",
            err=True,
        )

    results = hybrid.query(
        question=question,
        spaces=space_filter,
        limit=limit,
        pack_ids=effective_pack_ids,
        include_unpackaged=include_unpackaged,
    )

    # --- Legacy list JSON output (must remain unchanged in shape) ---
    if json_output and not json_envelope:
        click.echo(json.dumps([r.to_dict() for r in results], indent=2, default=str))
        return

    # --- New envelope output ---
    if json_envelope:
        envelope = {
            "question": question,
            "spaces_filter": space_filter,
            "pack_filter": {
                "pack_ids": effective_pack_ids,
                "auto_pack": bool(auto_pack),
                "include_unpackaged": bool(include_unpackaged),
            },
            "selected_packs": selected_packs,
            "total": len(results),
            "results": [r.to_dict() for r in results],
        }
        click.echo(json.dumps(envelope, indent=2, ensure_ascii=False, default=str))
        return

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    console.print(f"\n[bold]Query:[/bold] {question}")
    if selected_packs:
        sp = selected_packs[0]
        console.print(
            f"[dim]Auto-pack selected pack={sp['pack_id']} score={sp['score']:.1f}[/dim]"
        )
    console.print(f"[dim]Found {len(results)} result(s)[/dim]\n")

    for i, result in enumerate(results, 1):
        pack_label = ""
        if show_pack:
            pid = (result.metadata or {}).get("pack_id") or "?"
            pack_label = f"pack={pid} "
        console.print(
            f"[bold cyan]{i}.[/bold cyan] "
            f"[{result.source}] "
            f"{pack_label}"
            f"node={result.node_id or '?'} "
            f"score={result.score:.3f}"
        )
        if result.text:
            preview = result.text[:200].replace("\n", " ")
            console.print(f"   [dim]{preview}...[/dim]")
        console.print()


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


@main.command()
@click.option("--json-output", is_flag=True, default=False)
def manifest(json_output: bool) -> None:
    """Print the full MetaOntology OS grammar."""
    from opencrab.grammar.validator import describe_grammar

    grammar = describe_grammar()

    if json_output:
        click.echo(json.dumps(grammar, indent=2))
        return

    console.print(
        Panel(
            "[bold magenta]MetaOntology OS Grammar[/bold magenta]",
            subtitle="OpenCrab",
        )
    )

    # Spaces
    table = Table(title="Spaces", show_header=True)
    table.add_column("Space ID", style="cyan bold")
    table.add_column("Node Types", style="green")
    table.add_column("Description")
    for space_id, spec in grammar["spaces"].items():
        table.add_row(
            space_id,
            ", ".join(spec["node_types"]),
            spec["description"],
        )
    console.print(table)

    # Meta-edges
    edge_table = Table(title="Meta-Edges", show_header=True)
    edge_table.add_column("From", style="cyan")
    edge_table.add_column("To", style="green")
    edge_table.add_column("Relations")
    for edge in grammar["meta_edges"]:
        edge_table.add_row(
            edge["from_space"],
            edge["to_space"],
            ", ".join(edge["relations"]),
        )
    console.print(edge_table)

    # Impact categories
    impact_table = Table(title="Impact Categories", show_header=True)
    impact_table.add_column("ID", style="yellow bold")
    impact_table.add_column("Name", style="cyan")
    impact_table.add_column("Question")
    for cat in grammar["impact_categories"]:
        impact_table.add_row(cat["id"], cat["name"], cat["question"])
    console.print(impact_table)

    # ReBAC
    rebac = grammar["rebac"]
    console.print(
        Panel(
            f"[bold]Object types:[/bold] {', '.join(rebac['object_types'])}\n"
            f"[bold]Permissions:[/bold] {', '.join(rebac['permissions'])}",
            title="ReBAC",
        )
    )


# ---------------------------------------------------------------------------
# media adapters
# ---------------------------------------------------------------------------


@main.command("ocr")
@click.argument("path", type=click.Path(exists=True))
@click.option("--output", "output", "-o", default=None, type=click.Path(), help="Optional evidence JSON output path.")
@click.option("--backend", default="auto", show_default=True, type=click.Choice(["auto", "easyocr", "tesseract", "metadata"]))
@click.option("--lang", default="eng+kor", show_default=True, help="OCR language list: EasyOCR accepts en/ko, Tesseract accepts eng/kor.")
def ocr_command(path: str, output: str | None, backend: str, lang: str) -> None:
    """Run LocalCrab OCR adapter for one image/document path."""
    from opencrab.media.ocr import run_ocr, write_ocr_evidence

    result = run_ocr(path, backend=backend, lang=lang)
    payload = result.to_evidence()
    if output:
        payload = write_ocr_evidence(result, output)
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


@main.command("image-context")
@click.argument("path", type=click.Path(exists=True))
@click.option("--output", "output", "-o", default=None, type=click.Path(), help="Optional evidence JSON output path.")
@click.option("--backend", default="auto", show_default=True, type=click.Choice(["auto", "sentence-transformers", "fingerprint"]))
@click.option("--model-name", default="clip-ViT-B-32", show_default=True, help="sentence-transformers model name when available.")
def image_context_command(path: str, output: str | None, backend: str, model_name: str) -> None:
    """Build image context/CLIP-style evidence for one image path."""
    from opencrab.media.image_context import build_image_context, write_image_context

    result = build_image_context(path, backend=backend, model_name=model_name)
    payload = result.to_evidence()
    if output:
        payload = write_image_context(result, output)
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# export-neo4j-pack
# ---------------------------------------------------------------------------


@main.command("export-neo4j-pack")
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(),
    help="Output JSONL path, usually neo4j/opencrab_ingest.jsonl.",
)
@click.option("--pack-id", default=None, help="Optional pack_id/source filter.")
@click.option("--node-limit", default=500_000, show_default=True, type=int)
@click.option("--edge-limit", default=1_000_000, show_default=True, type=int)
def export_neo4j_pack(
    output: str,
    pack_id: str | None,
    node_limit: int,
    edge_limit: int,
) -> None:
    """Export graph store snapshot to OpenCrab Pack v1 JSONL.

    Works with all storage modes (local/kuzu/docker) via STORAGE_MODE env var.
    """
    from opencrab.config import get_settings
    from opencrab.pack import export_neo4j_opencrab_ingest
    from opencrab.stores.factory import make_graph_store

    cfg = get_settings()
    graph = make_graph_store(cfg)
    status = export_neo4j_opencrab_ingest(
        graph,
        output,
        pack_id=pack_id,
        node_limit=node_limit,
        edge_limit=edge_limit,
    )
    console.print_json(json.dumps(status, ensure_ascii=False))


# ---------------------------------------------------------------------------
# assemble-pack-v1
# ---------------------------------------------------------------------------


@main.command("assemble-pack-v1")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option("--output", "output", "-o", required=True, type=click.Path(), help="Output ZIP path.")
@click.option("--pack-id", required=True, help="OpenCrab Pack id.")
@click.option("--title", default=None, help="Human-readable pack title.")
def assemble_pack_v1_command(source_dir: str, output: str, pack_id: str, title: str | None) -> None:
    """Assemble an OpenCrab Pack v1 ZIP from a staging directory."""
    from opencrab.pack import assemble_pack_v1

    status = assemble_pack_v1(source_dir, output, pack_id=pack_id, title=title)
    console.print_json(json.dumps(status, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# packs group
# ---------------------------------------------------------------------------


@main.group()
def packs() -> None:
    """Inspect and maintain local OpenCrab packs."""


@packs.command("list")
def packs_list() -> None:
    """List packs found under <local_data_dir>/packs/."""
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import load_pack_registry

    cfg = get_settings()
    registry = load_pack_registry(cfg.local_data_dir)
    if not registry:
        console.print(f"[yellow]No packs under {cfg.local_data_dir}/packs/[/yellow]")
        return

    table = Table(title="OpenCrab Packs", show_header=True, header_style="bold cyan")
    table.add_column("pack_id", style="bold")
    table.add_column("title")
    table.add_column("version")
    table.add_column("nodes", justify="right")
    table.add_column("edges", justify="right")
    table.add_column("path")

    for pack in registry:
        nodes = pack.counts.get("nodes", "?")
        edges = pack.counts.get("edges", "?")
        table.add_row(
            pack.pack_id,
            (pack.title or "")[:60],
            pack.version,
            str(nodes),
            str(edges),
            str(pack.path),
        )
    console.print(table)


@packs.command("show")
@click.argument("pack_id")
def packs_show(pack_id: str) -> None:
    """Show full manifest summary for one pack."""
    from opencrab.config import get_settings
    from opencrab.ontology.pack_registry import get_pack

    cfg = get_settings()
    pack = get_pack(cfg.local_data_dir, pack_id)
    if pack is None:
        console.print(f"[red]Pack '{pack_id}' not found under {cfg.local_data_dir}/packs/[/red]")
        raise SystemExit(1)

    info = {
        "pack_id": pack.pack_id,
        "title": pack.title,
        "version": pack.version,
        "description": pack.description,
        "source": {
            "label": pack.source_label,
            "url": pack.source_url,
        },
        "counts": pack.counts,
        "path": str(pack.path),
        "manifest_path": str(pack.manifest_path),
    }
    console.print_json(json.dumps(info, ensure_ascii=False, default=str))


@packs.command("backfill-pack-id")
@click.option(
    "--assume-pack-id",
    "assume_pack_id",
    default=None,
    help="Assign this pack_id to every node/edge without one (escape hatch).",
)
@click.option(
    "--dry-run/--no-dry-run",
    "dry_run",
    default=None,
    help="Explicit dry-run toggle. Defaults to true; --apply is required to mutate.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Apply changes. Without this flag the command runs in dry-run mode.",
)
def packs_backfill_pack_id(
    assume_pack_id: str | None,
    dry_run: bool | None,
    apply_changes: bool,
) -> None:
    """Back-fill ``properties.pack_id`` on graph nodes/edges (default dry-run).

    Default mode infers pack_id from any ``/packs/<id>/`` path stored in
    ``properties.source_path`` / ``source_id`` / ``node_id`` / ``id``.
    ``--assume-pack-id X`` fills every still-empty entry with X.
    """
    import sqlite3 as _sqlite3

    from opencrab.config import get_settings
    from opencrab.ontology.pack_provenance import infer_pack_id_from_path

    cfg = get_settings()
    db_path = Path(cfg.local_data_dir) / "graph.db"
    if not db_path.exists():
        console.print(f"[red]graph.db not found: {db_path}[/red]")
        raise SystemExit(1)

    effective_dry_run = True
    if apply_changes and dry_run is True:
        console.print(
            "[yellow]warning: both --apply and --dry-run given; honouring --dry-run.[/yellow]"
        )
    elif apply_changes and dry_run is None:
        effective_dry_run = False
    elif dry_run is False and not apply_changes:
        console.print(
            "[yellow]warning: --no-dry-run given without --apply; staying in dry-run.[/yellow]"
        )

    summary = {
        "dry_run": effective_dry_run,
        "nodes_inferred": 0,
        "nodes_assumed": 0,
        "nodes_skipped": 0,
        "edges_inferred": 0,
        "edges_assumed": 0,
        "edges_skipped": 0,
    }

    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    cur = conn.cursor()

    def _process(table: str, key_cols: tuple[str, ...]) -> None:
        cur.execute(f"SELECT {', '.join(key_cols)}, properties FROM {table}")
        rows = cur.fetchall()
        for row in rows:
            try:
                props = json.loads(row["properties"]) if row["properties"] else {}
            except (TypeError, ValueError):
                props = {}
            if not isinstance(props, dict):
                summary[f"{table.split('_')[1]}_skipped"] += 1
                continue
            if props.get("pack_id"):
                continue
            inferred: str | None = None
            for candidate_key in ("source_path", "source_id", "id"):
                value = props.get(candidate_key)
                if value:
                    inferred = infer_pack_id_from_path(str(value))
                    if inferred:
                        break
            if not inferred:
                # node_id column from the row itself
                for key in key_cols:
                    if key.endswith("_id"):
                        inferred = infer_pack_id_from_path(str(row[key]))
                        if inferred:
                            break
            if inferred:
                props["pack_id"] = inferred
                summary_key = f"{table.split('_')[1]}_inferred"
                summary[summary_key] += 1
            elif assume_pack_id:
                props["pack_id"] = assume_pack_id
                summary_key = f"{table.split('_')[1]}_assumed"
                summary[summary_key] += 1
            else:
                summary_key = f"{table.split('_')[1]}_skipped"
                summary[summary_key] += 1
                continue
            if not effective_dry_run:
                set_clauses = " AND ".join(f"{c}=?" for c in key_cols)
                values = [json.dumps(props)] + [row[c] for c in key_cols]
                cur.execute(
                    f"UPDATE {table} SET properties=? WHERE {set_clauses}",
                    values,
                )

    _process("graph_nodes", ("node_type", "node_id"))
    _process("graph_edges", ("from_type", "from_id", "relation", "to_type", "to_id"))

    if not effective_dry_run:
        conn.commit()
    conn.close()

    console.print_json(json.dumps(summary, ensure_ascii=False))
    if effective_dry_run:
        console.print(
            "[dim]Dry-run only. Re-run with --apply to persist these changes.[/dim]"
        )


@packs.command("reindex-bm25")
def packs_reindex_bm25() -> None:
    """Rebuild the BM25 cache once (escape hatch; lazy rebuild is the default)."""
    from opencrab.config import get_settings
    from opencrab.ontology.bm25 import BM25Index
    from opencrab.stores.factory import make_doc_store

    cfg = get_settings()
    docs = make_doc_store(cfg)
    if not docs.available:
        console.print("[red]Doc store unavailable.[/red]")
        raise SystemExit(1)
    nodes = docs.list_nodes(limit=200_000)
    index = BM25Index.build(nodes)
    console.print_json(
        json.dumps(
            {"rebuilt": True, "node_count": len(nodes), "fingerprint": index.fingerprint},
            ensure_ascii=False,
            default=str,
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
