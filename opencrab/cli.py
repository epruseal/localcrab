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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from opencrab.config import Settings

console = Console()


def _make_stores(
    cfg: Settings,
    *,
    graph: bool = False,
    vector: bool = False,
    doc: bool = False,
    sql: bool = False,
) -> SimpleNamespace:
    """Construct only the store backends a command actually needs.

    Each store carries its own setup cost (the vector store in particular
    loads the KURE embedding chain), so building all four unconditionally
    would e.g. load an embedding model for a command like
    ``export-neo4j-pack`` that never touches vectors. Callers request only
    the stores they use; unrequested attributes are ``None``.
    """
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    return SimpleNamespace(
        graph=make_graph_store(cfg) if graph else None,
        vector=make_vector_store(cfg) if vector else None,
        doc=make_doc_store(cfg) if doc else None,
        sql=make_sql_store(cfg) if sql else None,
    )


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

    _bootstrap_local_user()


def _bootstrap_local_user() -> None:
    """Create the bootstrap owner/local user + token once (#144), atomically
    (a crash between creating the user and issuing its token must not leave
    a tokenless local user -- see ``auth.bootstrap_local_user``). Idempotent:
    a second ``opencrab init`` finds the existing is_local user (enabled or
    disabled -- ``get_local_user`` doesn't filter on that) and does not
    recreate the user or reissue a token; reissuing would silently undo an
    operator's deliberate ``user disable``."""
    from sqlalchemy.exc import IntegrityError

    from opencrab.auth import bootstrap_local_user, get_local_user
    from opencrab.config import get_settings

    cfg = get_settings()
    sql = _make_stores(cfg, sql=True).sql
    if not sql.available:
        console.print("[red]SQL store unavailable -- skipping user bootstrap.[/red]")
        raise SystemExit(1)

    existing = get_local_user(sql)
    if existing is not None:
        console.print(f"[dim]Local user already bootstrapped ({existing.user_id}).[/dim]")
        return

    try:
        user_id, secret = bootstrap_local_user(sql)
    except IntegrityError as exc:
        # Concurrent `init`: two runs both saw "no local user" above and both
        # tried to insert is_local=1 -- only idx_users_single_local's own
        # violation is that benign race; any other IntegrityError is a real
        # failure and must propagate. bootstrap_local_user's `begin()` block
        # already rolled back and closed the failed transaction, so
        # get_local_user(sql) below opens a fresh connection rather than
        # reusing the aborted one (required on PostgreSQL, where an aborted
        # transaction can't be queried further).
        #
        # Dialects report this differently: PostgreSQL's message names the
        # index ("duplicate key value violates unique constraint
        # \"idx_users_single_local\""); SQLite's names the column instead
        # ("UNIQUE constraint failed: users.is_local", confirmed by direct
        # reproduction) -- match either.
        orig = str(exc.orig)
        if "idx_users_single_local" not in orig and "users.is_local" not in orig:
            raise
        existing = get_local_user(sql)
        if existing is None:
            console.print(
                "[red]Local user bootstrap race detected, but no local user "
                "exists afterward -- something else is wrong.[/red]"
            )
            raise SystemExit(1) from exc
        console.print(
            f"[dim]Local user already bootstrapped ({existing.user_id}) "
            "(lost a concurrent init race).[/dim]"
        )
        return

    console.print(
        Panel(
            f"[bold]Local user created:[/bold] {user_id}\n\n"
            "[bold]Bootstrap token (shown once -- save it now):[/bold]\n"
            f"[cyan]{secret}[/cyan]\n\n"
            f"[dim]Lost it? Run: opencrab token issue {user_id}[/dim]",
            title="OpenCrab Auth Bootstrap",
            border_style="yellow",
        )
    )


def _write_default_env(path: Path) -> None:
    default_data_dir = Path.home() / ".local" / "share" / "localcrab"
    content = f"""\
LOCAL_DATA_DIR={default_data_dir}
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
    "--allow-query-token",
    is_flag=True,
    default=False,
    help=(
        "http only. Also accept the token as ?token= in the URL. OFF by default: "
        "a URL-borne credential leaks into access logs, proxy logs, browser "
        "history and Referer headers. Enable it only for clients that cannot "
        "set an Authorization header -- see docs/mcp-client-auth.md."
    ),
)
def serve(
    transport: str,
    host: str | None,
    port: int | None,
    allow_query_token: bool,
) -> None:
    """Start the OpenCrab MCP server (stdio by default, or Streamable HTTP)."""
    from opencrab.auth import refuse_stale_shared_secret_env

    # #145: a leftover pre-#145 shared-secret env var no longer protects
    # anything -- refuse both transports rather than let an operator believe
    # they're still gated. create_app() (http path) checks this too, but
    # stdio never calls create_app, so it must be checked here as well.
    try:
        refuse_stale_shared_secret_env()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    if transport == "stdio":
        if allow_query_token:
            # Rejected, not ignored: stdio carries no HTTP request, so the flag
            # can never take effect. Ignoring it would leave the operator
            # believing query-token auth is on.
            console.print(
                "[red]--allow-query-token applies to --transport http only "
                "(stdio has no HTTP request to carry a query parameter).[/red]"
            )
            raise SystemExit(1)
        # Suppress all non-error logging to keep the stdio JSON-RPC channel clean.
        logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
        from opencrab.auth import principal_scope, require_local_principal
        from opencrab.mcp.server import MCPServer

        try:
            principal = require_local_principal()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        # #145: stdio serves exactly one local principal for its whole
        # lifetime (there is no per-request identity over stdio) -- bind it
        # once around the entire blocking run loop so every tools/call
        # dispatched during it can read current_principal().
        with principal_scope(principal):
            MCPServer().run()
        return

    # transport == "http"
    from opencrab.config import get_settings
    from opencrab.mcp.http_app import create_app

    cfg = get_settings()
    bind_host = host or cfg.mcp_http_host
    bind_port = port or cfg.mcp_http_port

    # HTTP can log normally — stdout is not a protocol channel here.
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        stream=sys.stderr,
    )
    console.print(
        "[green]OpenCrab MCP (Streamable HTTP, per-user bearer token auth) → "
        f"http://{bind_host}:{bind_port}/mcp[/green]"
    )

    import uvicorn

    # Single worker: historically required because the chroma PersistentClient is
    # single-process only. Under VECTOR_BACKEND=sqlite-vec (SQLite WAL) multiple
    # workers are technically possible, but 1 is retained — each worker would hold
    # its own in-memory BM25 index + embedding function, and write.lock already
    # serialises cross-process writes. Not a hard constraint under sqlite-vec.
    uvicorn.run(
        create_app(allow_query_token=allow_query_token),
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

    cfg = get_settings()
    mode_label = "[bold cyan]LOCAL MODE[/bold cyan]"
    storage_loc = cfg.local_data_dir
    console.print(f"\n{mode_label} - storage at: {storage_loc}\n")

    stores = _make_stores(cfg, graph=True, vector=True, doc=True, sql=True)
    graph, vector, docs, sql = stores.graph, stores.vector, stores.doc, stores.sql

    # VECTOR_BACKEND 가 조건부 기본값(vector_backend_resolved)을 가지므로 라벨/경로도
    # 하드코딩 대신 실제 선택된 백엔드를 반영한다.
    if cfg.vector_backend_resolved == "sqlite-vec":
        vector_label = "Vector (sqlite-vec)"
        vector_path = cfg.local_data_dir + "/" + cfg.vector_db_file
    else:
        vector_label = "Vector (ChromaDB)"
        vector_path = cfg.local_data_dir + "/chroma"

    store_rows: list[tuple[str, str, Any]] = [
        ("Graph (SQLite)",    cfg.local_data_dir + "/graph.db",    graph),
        (vector_label,        vector_path,                         vector),
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

    # issue #105 codex follow-up: billing_events now lives in its own file
    # in local/kuzu mode (make_billing_sql_store) and can fail independently
    # of `sql` -- a corrupt billing.db or a permission problem specific to
    # that one file. BillingHooks swallows a failed table-creation into a
    # WARNING log only (see its `tables_ready` attribute), so without this
    # row `status` could report every configured store OK while every
    # billing event is silently being dropped -- the same "failure is
    # swallowed and nobody notices" shape issue #105 itself was about, one
    # layer up from emit()'s own return value. pg/docker mode shares `sql`
    # (make_billing_sql_store returns it unchanged there), so its health is
    # already covered by the "SQL" row above -- no separate row needed.
    from opencrab.billing.hooks import BillingHooks
    from opencrab.stores.factory import make_billing_sql_store

    billing_store = make_billing_sql_store(cfg, sql)
    if billing_store is not sql:
        billing_hooks = BillingHooks(billing_store)
        if not billing_store.available:
            billing_status = "[red]UNAVAILABLE[/red]"
        elif not billing_hooks.tables_ready:
            billing_status = "[red]UNAVAILABLE (table creation failed)[/red]"
        else:
            try:
                ok = billing_store.ping()
                billing_status = "[green]OK[/green]" if ok else "[yellow]CONNECTED (ping failed)[/yellow]"
            except Exception:
                billing_status = "[yellow]CONNECTED[/yellow]"
        table.add_row("Billing (SQLite)", cfg.local_data_dir + "/billing.db", billing_status)

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
    from opencrab.auth import require_local_principal
    from opencrab.config import get_settings
    from opencrab.locking import write_lock
    from opencrab.ontology.pack_provenance import infer_pack_id_from_path
    from opencrab.ontology.query import HybridQuery

    # #145: a CLI write is attributed to the local user, and resolving the
    # principal happens BEFORE any store is opened or written -- a missing
    # local user must fail with an instruction, not a traceback halfway
    # through an ingest run. Admin commands (init/user/token/status) are
    # deliberately exempt: they are how a local user comes to exist.
    try:
        principal = require_local_principal()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    cfg = get_settings()
    stores = _make_stores(cfg, graph=True, vector=True, doc=True)
    chroma, neo4j, mongo = stores.vector, stores.graph, stores.doc
    hybrid = HybridQuery(chroma, neo4j)

    extensions = [e.strip() for e in extension.split(",")]
    root = Path(path)
    if root.is_dir():
        files = list(root.rglob("*")) if recursive else list(root.iterdir())
    else:
        files = [root]
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
            # user_id is the audit actor for this source row. Set from the
            # server-resolved principal, never from anything the caller typed.
            meta = {
                "source_path": str(file),
                "extension": file.suffix,
                "user_id": principal.user_id,
            }

            effective_pack = pack_id or infer_pack_id_from_path(file.resolve())
            if effective_pack:
                meta["pack_id"] = effective_pack

            with write_lock():
                hybrid.ingest(text=text, source_id=source_id, metadata=meta)

                if mongo.available:
                    mongo.upsert_source(source_id, text, meta)
                    # Audit row carries the same actor as the source metadata.
                    try:
                        mongo.log_event(
                            "ingest",
                            subject_id=principal.user_id,
                            details={"source_id": source_id, "pack_id": effective_pack},
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Audit is best-effort by contract: a failed audit row
                        # must not lose an otherwise-good ingest. But it must
                        # not vanish either -- silently swallowing it lets an
                        # actor-less ingest report success.
                        console.print(
                            f"  [yellow]audit record failed for {source_id}: {exc}[/yellow]"
                        )

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
    from opencrab.auth import require_local_principal
    from opencrab.config import get_settings
    from opencrab.locking import write_lock
    from opencrab.ontology.builder import OntologyBuilder
    from opencrab.ontology.extractor import LLMExtractor

    if not api_key:
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]ANTHROPIC_API_KEY not set. Pass --api-key or set the env var.[/red]")
        raise SystemExit(1)

    # #145: only the writing path needs an actor. --dry-run writes nothing, so
    # requiring a bootstrapped local user there would break a command that
    # works today. Ordered AFTER the argument checks above and BEFORE any store
    # is opened: a missing API key is the more specific failure and should be
    # what the operator sees, but a missing local user must still stop the run
    # before anything is written rather than partway through extraction.
    principal = None
    if not dry_run:
        try:
            principal = require_local_principal()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

    cfg = get_settings()
    stores = _make_stores(cfg, graph=True, doc=True, sql=True)
    graph, doc, sql = stores.graph, stores.doc, stores.sql
    builder = OntologyBuilder(graph, doc, sql)
    extractor = LLMExtractor(api_key=api_key, model=model)

    extensions = [e.strip() for e in extension.split(",")]
    root = Path(path)
    if root.is_dir():
        files = list(root.rglob("*")) if recursive else list(root.iterdir())
    else:
        files = [root]
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
                with write_lock():
                    for node in result.nodes:
                        try:
                            builder.add_node(
                                space=node.space,
                                node_type=node.node_type,
                                node_id=node.node_id,
                                properties=node.properties,
                                subject_id=principal.user_id,
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
                                subject_id=principal.user_id,
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

    cfg = get_settings()
    stores = _make_stores(cfg, graph=True, vector=True, doc=True)
    chroma, neo4j, docs = stores.vector, stores.graph, stores.doc
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

    outcome = hybrid.query(
        question=question,
        spaces=space_filter,
        limit=limit,
        pack_ids=effective_pack_ids,
        include_unpackaged=include_unpackaged,
    )
    results = outcome.results
    # #51: spaces 필터의 과도기 경고(백필 전 기존 벡터 제외)를 pack 경고와 동일하게 echo.
    # outcome.warnings 는 query() 의 반환값(지역 변수)이라 인스턴스 상태 경합이 없다.
    for warning in outcome.warnings:
        click.echo(f"warning: {warning}", err=True)

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

    Works with all storage modes (local/kuzu/docker/pg) via STORAGE_MODE env var.
    """
    from opencrab.config import get_settings
    from opencrab.pack import export_neo4j_opencrab_ingest

    cfg = get_settings()
    graph = _make_stores(cfg, graph=True).graph
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
    from opencrab.config import get_settings
    from opencrab.ontology.pack_provenance import backfill_pack_ids, resolve_backfill_dry_run

    cfg = get_settings()
    db_path = Path(cfg.local_data_dir) / "graph.db"
    if not db_path.exists():
        console.print(f"[red]graph.db not found: {db_path}[/red]")
        raise SystemExit(1)

    effective_dry_run, warning = resolve_backfill_dry_run(apply_changes, dry_run)
    if warning:
        console.print(f"[yellow]{warning}[/yellow]")

    # No write_lock() here on purpose: backfill_pack_ids() takes it itself,
    # around the write and only when there is one. Locking here as well would
    # take an exclusive lock for --dry-run, which writes nothing, and would
    # leave every non-CLI caller of backfill_pack_ids() unprotected.
    summary = backfill_pack_ids(
        db_path, assume_pack_id=assume_pack_id, dry_run=effective_dry_run
    )

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

    cfg = get_settings()
    docs = _make_stores(cfg, doc=True).doc
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
# user group (#144)
# ---------------------------------------------------------------------------


def _require_sql():
    """Shared helper: build the SQL store or exit(1) if unavailable."""
    from opencrab.config import get_settings

    sql = _make_stores(get_settings(), sql=True).sql
    if not sql.available:
        console.print("[red]SQL store unavailable.[/red]")
        raise SystemExit(1)
    return sql


@main.group()
def user() -> None:
    """Manage principal-store users."""


@user.command("add")
@click.argument("display_name")
@click.option(
    "--local",
    "is_local",
    is_flag=True,
    default=False,
    help="Bind as the stdio/CLI local principal (at most one; see 'opencrab init').",
)
def user_add(display_name: str, is_local: bool) -> None:
    """Create a new user."""
    from opencrab.auth import create_user

    sql = _require_sql()
    try:
        user_id = create_user(sql, display_name, is_local=is_local)
    except Exception as exc:
        console.print(f"[red]Could not create user: {exc}[/red]")
        raise SystemExit(1) from exc
    console.print_json(
        json.dumps(
            {"user_id": user_id, "display_name": display_name, "is_local": is_local},
            ensure_ascii=False,
        )
    )


@user.command("list")
def user_list() -> None:
    """List all users."""
    from opencrab.auth import list_users

    sql = _require_sql()
    users = list_users(sql)
    table = Table(title="OpenCrab Users", show_header=True, header_style="bold cyan")
    table.add_column("user_id", style="bold")
    table.add_column("display_name")
    table.add_column("local")
    table.add_column("disabled")
    table.add_column("created_at")
    for u in users:
        table.add_row(
            u["user_id"], u["display_name"], str(u["is_local"]), str(u["disabled"]), str(u["created_at"])
        )
    console.print(table)


@user.command("disable")
@click.argument("user_id")
def user_disable(user_id: str) -> None:
    """Disable a user (their tokens stop verifying)."""
    from opencrab.auth import disable_user

    sql = _require_sql()
    try:
        disabled = disable_user(sql, user_id)
    except Exception as exc:
        console.print(f"[red]Could not disable user: {exc}[/red]")
        raise SystemExit(1) from exc
    if not disabled:
        console.print(f"[red]No such user: {user_id}[/red]")
        raise SystemExit(1)
    console.print(f"[green]Disabled {user_id}[/green]")


@user.command("enable")
@click.argument("user_id")
def user_enable(user_id: str) -> None:
    """Re-enable a disabled user (their tokens verify again). The recovery
    path that makes ``user disable`` (including on the local user) safe."""
    from opencrab.auth import enable_user

    sql = _require_sql()
    if not enable_user(sql, user_id):
        console.print(f"[red]No such user: {user_id}[/red]")
        raise SystemExit(1)
    console.print(f"[green]Enabled {user_id}[/green]")


# ---------------------------------------------------------------------------
# token group (#144)
# ---------------------------------------------------------------------------


@main.group()
def token() -> None:
    """Manage API tokens."""


@token.command("issue")
@click.argument("user_id")
@click.option("--name", default=None, help="Optional label for this token.")
def token_issue(user_id: str, name: str | None) -> None:
    """Issue a new token for a user. Prints the secret once."""
    from opencrab.auth import issue_token

    sql = _require_sql()
    try:
        token_id, secret = issue_token(sql, user_id, name=name)
    except ValueError as exc:
        console.print(f"[red]Could not issue token: {exc}[/red]")
        raise SystemExit(1) from exc
    console.print(
        Panel(
            f"[bold]token_id:[/bold] {token_id}\n\n"
            "[bold]Secret (shown once -- save it now):[/bold]\n"
            f"[cyan]{secret}[/cyan]",
            title="OpenCrab Token Issued",
            border_style="yellow",
        )
    )


@token.command("list")
@click.argument("user_id")
def token_list(user_id: str) -> None:
    """List a user's tokens (never shows hashes or secrets)."""
    from opencrab.auth import list_tokens

    sql = _require_sql()
    tokens = list_tokens(sql, user_id)
    table = Table(title=f"Tokens for {user_id}", show_header=True, header_style="bold cyan")
    table.add_column("token_id", style="bold")
    table.add_column("name")
    table.add_column("created_at")
    table.add_column("last_used_at")
    table.add_column("revoked_at")
    for t in tokens:
        table.add_row(
            t["token_id"], t["name"] or "", str(t["created_at"]), str(t["last_used_at"]), str(t["revoked_at"])
        )
    console.print(table)


@token.command("revoke")
@click.argument("token_id")
def token_revoke(token_id: str) -> None:
    """Revoke a token."""
    from opencrab.auth import revoke_token

    sql = _require_sql()
    revoke_token(sql, token_id)
    console.print(f"[green]Revoked {token_id}[/green]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
