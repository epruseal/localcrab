from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import EdgeIdentityConflict, NodeIdentityConflict
from opencrab.common.ids import stable_id
from opencrab.common.text import slugify as _common_slugify
from opencrab.locking import write_lock
from opencrab.ontology.builder import (
    OntologyBuilder,
    store_write_failures,
    store_write_succeeded_for,
)
from opencrab.stores.local_doc_store import LocalDocStore
from opencrab.stores.neo4j_store import Neo4jStore
from opencrab.stores.sql_store import SQLStore

logger = logging.getLogger(__name__)

# write_gate.identity_reject_message() 의 "Fixed wording"(opencrab/pack/write_gate.py,
# CONFLICT_UNVERIFIABLE 케이스 전용 -- #143 invariant 7 로 다른 필드는 못 넣지만
# 이 문구 자체는 안정적으로 고정된 계약이다). 이 문구가 ValueError 메시지에 있으면
# identity probe 자체가 backend 장애로 실패한 것이지 이 노트/엣지의 데이터 문제가
# 아니므로 격리하지 않고 재던진다(#190).
_IDENTITY_UNVERIFIABLE_MARKER = "cannot verify existing ownership on this backend"

# _collect_notes 전용 화이트리스트: 파일 1건을 못 읽는 것(OSError)만 그 파일
# 국소 문제로 본다.
_COLLECT_ISOLATE: tuple[type[BaseException], ...] = (OSError,)

# 쓰기 루프(노트/폴더/태그/미해결링크) 전용 화이트리스트. 같은 OSError 라도
# 여기서는 스토어 자체의 쓰기 불가(디스크 용량 부족 등, 전역 장애)를 뜻할 수
# 있으므로 수집 루프와 공유하지 않는다(#190 설계검증 2차 지적).
_WRITE_ISOLATE: tuple[type[BaseException], ...] = (
    NodeIdentityConflict,
    EdgeIdentityConflict,
    ValueError,
)


@contextmanager
def _isolate_item_error(
    label: str,
    failures: list[dict[str, str]],
    *,
    isolate: tuple[type[BaseException], ...],
):
    """개별 항목 처리 중 예외를 격리한다.

    ``isolate``에 명시적으로 올린 예외 타입만 이 항목에 국한된 문제로 보고
    ``failures``에 적재한 뒤 계속한다. 그 외 전부는 기본적으로 재던진다
    (fail-closed) -- pack 레지스트리 불가, 손상된 문서 컬렉션, 그래프 백엔드
    자체 불가, identity probe 자체의 backend 장애처럼 "이 프로세스의 나머지
    처리 전부가 같은 이유로 실패한다"는 전역 상태를 항목 실패로 흡수하지
    않기 위함이다. 화이트리스트를 호출부(수집/쓰기)마다 다르게 넘기는 이유는
    같은 예외 타입(``OSError``)이라도 파일 1건을 못 읽는 것과 스토어 자체가
    쓰기 불가인 것은 격리 여부가 반대이기 때문이다(#190 설계검증 2차 지적).
    """
    try:
        yield
    except isolate as exc:
        if isinstance(exc, ValueError) and _IDENTITY_UNVERIFIABLE_MARKER in str(exc):
            raise
        logger.warning("import item isolated after error: %s: %s", label, exc)
        failures.append({"item": label, "error": str(exc)})


@dataclass
class _WriteTally:
    kind: str  # "node" | "edge"
    written: int = 0
    failed: int = 0

    def record(self, receipt: Any, label: str) -> None:
        stores = receipt.get("stores") if isinstance(receipt, dict) else None
        if not isinstance(stores, dict):  # truthy non-dict 도 여기서 걸러야 한다:
            stores = {}  # store_write_failures() 는 .items() 를 바로 부른다
        if store_write_succeeded_for(stores, self.kind):
            self.written += 1
            return
        self.failed += 1
        detail = "; ".join(store_write_failures(stores)) or "no store confirmed the write"
        logger.warning("%s %s not stored: %s", self.kind, label, detail)


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_가-힣\-/]+)")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


@dataclass(frozen=True)
class NoteRecord:
    path: Path
    rel_path: str
    title: str
    note_doc_id: str
    note_text_id: str
    note_topic_id: str
    text: str
    frontmatter: dict[str, Any]
    tags: list[str]
    wikilinks: list[str]
    folders: list[str]


def sha_id(prefix: str, value: str) -> str:
    # Canonical stable-id form (SHA-256, sorted-JSON, ``prefix:digest``), shared
    # with the rest of the codebase. Previously this used SHA-1 + dash + raw
    # string; the change only affects newly-derived obsidian ids (no obsidian
    # data is currently persisted, so no migration is required).
    return stable_id(prefix, value)


def slugify(value: str) -> str:
    return _common_slugify(value, allow_hangul=True, fallback="node")


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    frontmatter: dict[str, Any] = {}
    block = match.group(1)
    current_key: str | None = None

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") and current_key:
            frontmatter.setdefault(current_key, [])
            frontmatter[current_key].append(stripped[2:].strip().strip("'\""))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            frontmatter[key] = []
            continue
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
            frontmatter[key] = items
        else:
            frontmatter[key] = value.strip("'\"")

    return frontmatter, raw[match.end():]


def normalize_wikilink(link: str) -> str:
    core = link.split("|", 1)[0].split("#", 1)[0].strip()
    core = core.replace("\\", "/")
    if core.endswith(".md"):
        core = core[:-3]
    return core


def build_note_record(root: Path, path: Path) -> NoteRecord:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    frontmatter, body = parse_frontmatter(raw)
    rel_path = path.relative_to(root).as_posix()
    title = str(frontmatter.get("title") or path.stem)
    workspace_id = vault_workspace_id(root)

    inline_tags = TAG_RE.findall(body)
    fm_tags = frontmatter.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]
    tags = sorted({tag.strip("#") for tag in [*fm_tags, *inline_tags] if tag})

    wikilinks = [normalize_wikilink(match) for match in WIKILINK_RE.findall(body)]
    folders = rel_path.split("/")[:-1]

    return NoteRecord(
        path=path,
        rel_path=rel_path,
        title=title,
        note_doc_id=sha_id("doc-obsidian", f"{workspace_id}::{rel_path}"),
        note_text_id=sha_id("text-obsidian", f"{workspace_id}::{rel_path}"),
        note_topic_id=sha_id("topic-note", f"{workspace_id}::{rel_path}"),
        text=body.strip(),
        frontmatter=frontmatter,
        tags=tags,
        wikilinks=[link for link in wikilinks if link],
        folders=folders,
    )


def folder_topic_id(workspace_id: str, folder_path: str) -> str:
    return sha_id("topic-folder", f"{workspace_id}::{folder_path}")


def tag_topic_id(workspace_id: str, tag: str) -> str:
    return sha_id("topic-tag", f"{workspace_id}::{tag.lower()}")


def unresolved_link_topic_id(workspace_id: str, link: str) -> str:
    return sha_id("topic-link", f"{workspace_id}::{link.lower()}")


def vault_workspace_id(vault_root: Path) -> str:
    digest = hashlib.sha1(str(vault_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"obsidian-{slugify(vault_root.name)}-{digest}"


def note_theme(note: NoteRecord) -> str:
    haystack = " ".join([note.rel_path, note.title, *note.tags]).lower()
    if any(term in haystack for term in ["조경", "landscape", "garden", "tree", "plant", "정원"]):
        return "landscape"
    if any(term in haystack for term in ["alex", "alexai"]):
        return "alex"
    if any(term in haystack for term in ["ai", "agent", "llm", "rag", "ontology", "neo4j", "opencrab"]):
        return "ai"
    return "default"


def topic_theme(name: str) -> str:
    haystack = name.lower()
    if any(term in haystack for term in ["조경", "landscape", "garden", "tree", "plant", "정원"]):
        return "landscape"
    if any(term in haystack for term in ["alex", "alexai"]):
        return "alex"
    if any(term in haystack for term in ["ai", "agent", "llm", "rag", "ontology", "neo4j", "opencrab"]):
        return "ai"
    return "default"


def theme_color(theme: str) -> str:
    palette = {
        "landscape": "#5ea85b",
        "ai": "#e38b2c",
        "alex": "#d97ab5",
        "default": "#7f8c8d",
    }
    return palette.get(theme, palette["default"])


def excerpt(text: str, limit: int = 1200) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit]


def _collect_notes(vault_root: Path) -> tuple[list[NoteRecord], list[dict[str, str]]]:
    """Read and parse the vault without holding the shared write lock.

    한 파일을 못 읽는 ``OSError``(권한, 깨진 심볼릭 링크 등)는 그 파일만
    건너뛰고 나머지 파일은 계속 수집한다(#190). 그 외 예외는 그대로 전파된다.
    """
    notes: list[NoteRecord] = []
    failed: list[dict[str, str]] = []
    for path in sorted(vault_root.rglob("*.md")):
        rel = path.relative_to(vault_root).as_posix()
        with _isolate_item_error(f"note:{rel}", failed, isolate=_COLLECT_ISOLATE):
            notes.append(build_note_record(vault_root, path))
    return notes, failed


def _import_vault_unlocked(vault_root: Path, neo4j_uri: str, neo4j_user: str, neo4j_password: str, neo4j_database: str, local_data_dir: Path, notes: list[NoteRecord] | None = None, pack_id: str | None = None) -> dict[str, Any]:
    # notes=None 분기는 실제 호출자가 없는 죽은 코드다(테스트 편의용 시그니처로
    # 남겨둔다 -- #190 범위 밖). 도달할 경우에 대비해 튜플 언패킹만 안전하게
    # 고치고, 그 경로의 수집 실패는 버려진다(도달 불가능하므로 무해).
    notes = notes if notes is not None else _collect_notes(vault_root)[0]
    workspace_id = vault_workspace_id(vault_root)

    note_by_rel = {note.rel_path[:-3] if note.rel_path.endswith(".md") else note.rel_path: note for note in notes}
    note_by_basename: dict[str, list[NoteRecord]] = defaultdict(list)
    for note in notes:
        note_by_basename[note.path.stem].append(note)

    graph = Neo4jStore(neo4j_uri, neo4j_user, neo4j_password, database=neo4j_database)
    docs = LocalDocStore(str(local_data_dir / "docs"))
    sql = SQLStore(f"sqlite:///{local_data_dir / 'opencrab.db'}")
    builder = OntologyBuilder(graph, docs, sql)

    # #148: builder.add_node/add_edge now require a bound principal + pack_id.
    # The caller (import_vault) opens principal_scope around this call, so
    # current_principal() resolves here -- this function does not bind one
    # itself (its direct callers, e.g. tests, are expected to bind their own).
    from opencrab.auth import current_principal
    from opencrab.pack.ownership import resolve_write_pack

    target_pack_id = resolve_write_pack(sql, current_principal(), pack_id)

    nodes = _WriteTally("node")
    edges = _WriteTally("edge")
    failed_items: list[dict[str, str]] = []
    failed_notes: list[dict[str, str]] = []
    edge_props = {
        "source": "obsidian",
        "workspace_id": workspace_id,
        "workspace_label": vault_root.name,
    }

    folder_paths = set()
    tag_names = set()
    unresolved_links = set()

    for note in notes:
        for depth in range(1, len(note.folders) + 1):
            folder_paths.add("/".join(note.folders[:depth]))
        tag_names.update(note.tags)
        for link in note.wikilinks:
            target = note_by_rel.get(link)
            if target is None and len(note_by_basename.get(Path(link).name, [])) != 1:
                unresolved_links.add(link)

    for folder_path in sorted(folder_paths):
        with _isolate_item_error(f"folder:{folder_path}", failed_items, isolate=_WRITE_ISOLATE):
            name = folder_path.split("/")[-1]
            nodes.record(
                builder.add_node(
                    space="concept",
                    node_type="Topic",
                    node_id=folder_topic_id(workspace_id, folder_path),
                    properties={
                        "name": name,
                        "source": "obsidian",
                        "workspace_id": workspace_id,
                        "workspace_label": vault_root.name,
                        "obsidian_path": folder_path,
                        "viz_theme": topic_theme(folder_path),
                        "viz_color": theme_color(topic_theme(folder_path)),
                    },
                    pack_id=target_pack_id,
                ),
                folder_path,
            )
            parent = folder_path.rsplit("/", 1)[0] if "/" in folder_path else None
            if parent:
                edges.record(
                    builder.add_edge(
                        "concept",
                        folder_topic_id(workspace_id, folder_path),
                        "part_of",
                        "concept",
                        folder_topic_id(workspace_id, parent),
                        properties=edge_props,
                        pack_id=target_pack_id,
                    ),
                    f"{folder_path}-[part_of]->{parent}",
                )

    for tag in sorted(tag_names):
        with _isolate_item_error(f"tag:{tag}", failed_items, isolate=_WRITE_ISOLATE):
            nodes.record(
                builder.add_node(
                    space="concept",
                    node_type="Topic",
                    node_id=tag_topic_id(workspace_id, tag),
                    properties={
                        "name": tag,
                        "source": "obsidian",
                        "workspace_id": workspace_id,
                        "workspace_label": vault_root.name,
                        "obsidian_kind": "tag",
                        "viz_theme": topic_theme(tag),
                        "viz_color": theme_color(topic_theme(tag)),
                    },
                    pack_id=target_pack_id,
                ),
                tag,
            )

    for link in sorted(unresolved_links):
        with _isolate_item_error(f"link:{link}", failed_items, isolate=_WRITE_ISOLATE):
            nodes.record(
                builder.add_node(
                    space="concept",
                    node_type="Topic",
                    node_id=unresolved_link_topic_id(workspace_id, link),
                    properties={
                        "name": Path(link).name,
                        "source": "obsidian",
                        "workspace_id": workspace_id,
                        "workspace_label": vault_root.name,
                        "obsidian_kind": "wikilink_stub",
                        "obsidian_target": link,
                        "viz_theme": topic_theme(link),
                        "viz_color": theme_color(topic_theme(link)),
                    },
                    pack_id=target_pack_id,
                ),
                link,
            )

    for note in notes:
        with _isolate_item_error(f"note:{note.rel_path}", failed_notes, isolate=_WRITE_ISOLATE):
            theme = note_theme(note)
            color = theme_color(theme)
            text_excerpt = excerpt(note.text)
            title = note.title
            mtime = int(note.path.stat().st_mtime)

            nodes.record(
                builder.add_node(
                    space="resource",
                    node_type="Document",
                    node_id=note.note_doc_id,
                    properties={
                        "name": title,
                        "title": title,
                        "source": "obsidian",
                        "source_path": note.rel_path,
                        "workspace_label": vault_root.name,
                        "workspace_id": workspace_id,
                        "summary": text_excerpt[:400],
                        "obsidian_rel_path": note.rel_path,
                        "obsidian_theme": theme,
                        "viz_theme": theme,
                        "viz_color": color,
                    },
                    pack_id=target_pack_id,
                ),
                note.rel_path,
            )

            nodes.record(
                builder.add_node(
                    space="evidence",
                    node_type="TextUnit",
                    node_id=note.note_text_id,
                    properties={
                        "title": title,
                        "text": text_excerpt,
                        "source": "obsidian",
                        "source_path": note.rel_path,
                        "workspace_label": vault_root.name,
                        "workspace_id": workspace_id,
                        "obsidian_rel_path": note.rel_path,
                        "char_count": len(note.text),
                        "modified_at": mtime,
                        "viz_theme": theme,
                        "viz_color": color,
                    },
                    pack_id=target_pack_id,
                ),
                note.rel_path,
            )

            nodes.record(
                builder.add_node(
                    space="concept",
                    node_type="Topic",
                    node_id=note.note_topic_id,
                    properties={
                        "name": title,
                        "source": "obsidian",
                        "workspace_label": vault_root.name,
                        "workspace_id": workspace_id,
                        "obsidian_kind": "note",
                        "obsidian_rel_path": note.rel_path,
                        "viz_theme": theme,
                        "viz_color": color,
                    },
                    pack_id=target_pack_id,
                ),
                note.rel_path,
            )

            docs.upsert_source(
                source_id=str(note.path.resolve()),
                text=note.text,
                metadata={
                    "source": "obsidian",
                    "workspace_id": workspace_id,
                    "workspace_label": vault_root.name,
                    "relative_path": note.rel_path,
                    "title": title,
                    "tags": note.tags,
                    "wikilinks": note.wikilinks,
                },
            )

            edges.record(
                builder.add_edge(
                    "resource",
                    note.note_doc_id,
                    "contains",
                    "evidence",
                    note.note_text_id,
                    properties={**edge_props, "source_path": note.rel_path},
                    pack_id=target_pack_id,
                ),
                f"{note.rel_path}-[contains]",
            )
            edges.record(
                builder.add_edge(
                    "evidence",
                    note.note_text_id,
                    "describes",
                    "concept",
                    note.note_topic_id,
                    properties={**edge_props, "source_path": note.rel_path},
                    pack_id=target_pack_id,
                ),
                f"{note.rel_path}-[describes]->topic",
            )

            for depth in range(1, len(note.folders) + 1):
                folder_path = "/".join(note.folders[:depth])
                edges.record(
                    builder.add_edge(
                        "evidence",
                        note.note_text_id,
                        "describes",
                        "concept",
                        folder_topic_id(workspace_id, folder_path),
                        properties={**edge_props, "source_path": note.rel_path},
                        pack_id=target_pack_id,
                    ),
                    f"{note.rel_path}-[describes]->{folder_path}",
                )

            for tag in note.tags:
                edges.record(
                    builder.add_edge(
                        "evidence",
                        note.note_text_id,
                        "mentions",
                        "concept",
                        tag_topic_id(workspace_id, tag),
                        properties={**edge_props, "source_path": note.rel_path},
                        pack_id=target_pack_id,
                    ),
                    f"{note.rel_path}-[mentions]->{tag}",
                )

            for link in note.wikilinks:
                target = note_by_rel.get(link)
                if target is None:
                    basename_matches = note_by_basename.get(Path(link).name, [])
                    target_topic_id = basename_matches[0].note_topic_id if len(basename_matches) == 1 else unresolved_link_topic_id(workspace_id, link)
                else:
                    target_topic_id = target.note_topic_id
                edges.record(
                    builder.add_edge(
                        "concept",
                        note.note_topic_id,
                        "related_to",
                        "concept",
                        target_topic_id,
                        properties={**edge_props, "source_path": note.rel_path},
                        pack_id=target_pack_id,
                    ),
                    f"{note.rel_path}-[related_to]->{link}",
                )

    return {
        "notes": len(notes),
        "nodes_written": nodes.written,
        "edges_written": edges.written,
        "node_write_failures": nodes.failed,
        "edge_write_failures": edges.failed,
        "folder_topics": len(folder_paths),
        "tag_topics": len(tag_names),
        "unresolved_link_topics": len(unresolved_links),
        "failed_items": failed_items,
        "failed_notes": failed_notes,
        "notes_failed": len(failed_notes),
    }


def import_vault(vault_root: Path, neo4j_uri: str, neo4j_user: str, neo4j_password: str, neo4j_database: str, local_data_dir: Path, pack_id: str | None = None) -> dict[str, Any]:
    """Import one vault under the same cross-process write boundary as APIs."""
    from opencrab.auth import principal_scope, require_local_principal

    # 잠금 밖에서 먼저 수집한다(#190) -- vault 파싱은 순수 파일 읽기이므로 다른
    # 프로세스의 쓰기를 그 시간만큼 막을 이유가 없다. 이 순서는 수정 전 코드와
    # 동일하다.
    notes, collect_failures = _collect_notes(vault_root)
    # #148: builder.add_node/add_edge now require a bound principal -- this
    # is a standalone process entry point, so bind the local user here (same
    # as opencrab.cli's write paths) rather than assume one is already bound.
    principal = require_local_principal()
    with write_lock(str(local_data_dir)), principal_scope(principal):
        result = _import_vault_unlocked(
            vault_root, neo4j_uri, neo4j_user, neo4j_password, neo4j_database, local_data_dir, notes, pack_id=pack_id
        )
    result["failed_notes"] = collect_failures + result.get("failed_notes", [])
    result["notes_failed"] = len(result["failed_notes"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an Obsidian vault into the OpenCrab ontology.")
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", "opencrab"))
    parser.add_argument("--neo4j-database", default=os.environ.get("NEO4J_DATABASE", "opencrab"))
    parser.add_argument("--local-data-dir", default=os.environ.get("LOCAL_DATA_DIR", "./opencrab_data"))
    parser.add_argument(
        "--pack-id",
        dest="pack_id",
        default=None,
        help="Destination pack_id. Defaults to the local user's default pack.",
    )
    args = parser.parse_args()

    result = import_vault(
        vault_root=Path(args.vault_root),
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
        neo4j_database=args.neo4j_database,
        local_data_dir=Path(args.local_data_dir),
        pack_id=args.pack_id,
    )
    print(result)
    if result["node_write_failures"] or result["edge_write_failures"]:
        print(
            f"WARNING: {result['node_write_failures']} node and "
            f"{result['edge_write_failures']} edge writes did not reach every required store",
            file=sys.stderr,
        )
    if result["failed_notes"] or result["failed_items"]:
        print(
            f"WARNING: {len(result['failed_notes'])} note(s) and "
            f"{len(result['failed_items'])} folder/tag/link item(s) were skipped -- "
            "see the failure list for causes",
            file=sys.stderr,
        )

    # #189 의 부분 실패 종료 코드 계약을 그대로 재사용한다(opencrab/cli.py).
    from opencrab.cli import _exit_if_partial_failure

    _exit_if_partial_failure(
        bool(
            result["node_write_failures"]
            or result["edge_write_failures"]
            or result["failed_notes"]
            or result["failed_items"]
        )
    )


if __name__ == "__main__":
    main()
