"""
Local document store — JSON-file-backed store for local-only mode.

Implements the same interface as MongoStore so consumers are agnostic
of the backend. Each "collection" is a single JSON file on disk.

THREAD SAFETY: a threading.Lock serialises all write operations (the
read-modify-write of _load/_save). Reads take no lock and stay safe
because _save writes to a temp file and os.replace()s it atomically, so a
concurrent reader always sees either the whole old file or the whole new
file — never a partially written one.

CORRUPTION CONTRACT: _load fails closed on a corrupt collection file. A
collection file is corrupt when it cannot be decoded as UTF-8, when its
content does not parse as JSON, or when the parsed value is not a JSON
object. _load raises CorruptCollectionError in each of these cases. A
missing file is not corrupt; _load returns {} for it, the same as before.
Every write reads the collection with _load before it writes, inside the
lock. A corrupt file therefore blocks the write and stays on disk,
unmodified, for the operator to inspect or repair.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

from opencrab.stores._source_integrity import (
    SourceCopyState,
    classify_integrity,
    complete_integrity,
    load_source_json,
    normalize_source_rows,
    require_original_source,
    source_classification,
    unknown_integrity,
)

logger = logging.getLogger(__name__)


class CorruptCollectionError(RuntimeError):
    """A collection file (``<collection>.json``) failed to load: it is not
    valid UTF-8, it is not valid JSON, or its top-level value is not a JSON
    object. ``_load`` raises this instead of returning ``{}`` (issue #209):
    the old fallback hid the corruption from every reader and let the next
    write silently replace the file, destroying the original bytes.

    Attributes:
        collection: the collection name (e.g. ``"nodes"``), not the path.
        path: the collection file's on-disk path.
    """

    def __init__(self, collection: str, path: str, reason: str) -> None:
        self.collection = collection
        self.path = path
        super().__init__(
            f"corrupt collection file '{collection}' ({path}): {reason}; "
            "not overwritten; inspect, repair or remove the file"
        )


class LocalDocStore:
    """JSON-file document store with the same interface as MongoStore."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._available = True
        self._lock = threading.Lock()
        logger.info("LocalDocStore initialised at %s", data_dir)

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        return os.path.isdir(self._data_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collection_path(self, name: str) -> str:
        return os.path.join(self._data_dir, f"{name}.json")

    def _load(self, collection: str) -> dict[str, Any]:
        path = self._collection_path(collection)
        if not os.path.exists(path):
            return {}
        # Decode through the text wrapper so only the str is alive while
        # json parses (a bytes buffer plus a str would double the peak).
        try:
            with open(path, encoding="utf-8") as f:
                data = load_source_json(f) if collection == "sources" else json.load(f)
            if not isinstance(data, dict):
                reason = f"top-level value is {type(data).__name__}, not an object"
                logger.error("Corrupt %s.json: %s", collection, reason)
                raise CorruptCollectionError(collection, path, reason)
            if collection == "sources":
                data = normalize_source_rows(data)
        except UnicodeDecodeError as exc:
            reason = f"invalid UTF-8: {exc}"
            logger.error("Corrupt %s.json: %s", collection, reason)
            raise CorruptCollectionError(collection, path, reason) from exc
        except json.JSONDecodeError as exc:
            reason = f"invalid JSON: {exc}"
            logger.error("Corrupt %s.json: %s", collection, reason)
            raise CorruptCollectionError(collection, path, reason) from exc
        if not isinstance(data, dict):
            reason = f"top-level value is {type(data).__name__}, not an object"
            logger.error("Corrupt %s.json: %s", collection, reason)
            raise CorruptCollectionError(collection, path, reason)
        return data

    def _save(self, collection: str, data: dict[str, Any]) -> None:
        """Atomic write: serialize to tmp file then rename to avoid corruption."""
        path = self._collection_path(collection)
        tmp_path = path + ".tmp"
        try:
            serialized = json.dumps(data, ensure_ascii=True, indent=2, default=str)
        except (TypeError, ValueError) as exc:
            logger.error("JSON serialization failed for %s: %s", collection, exc)
            raise
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(serialized)
        os.replace(tmp_path, path)

    # ------------------------------------------------------------------
    # Node document operations (mirrors MongoStore)
    # ------------------------------------------------------------------

    def upsert_node_doc(
        self,
        space: str,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
    ) -> str:
        with self._lock:
            data = self._load("nodes")
            key = f"{space}::{node_id}"
            data[key] = {
                "space": space,
                "node_type": node_type,
                "node_id": node_id,
                "properties": properties,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self._save("nodes", data)
            return key

    def get_node_doc(self, space: str, node_id: str) -> dict[str, Any] | None:
        data = self._load("nodes")
        return data.get(f"{space}::{node_id}")

    def list_nodes(self, space: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """``limit <= 0`` (issue #120 follow-up): returns ``[]`` without
        loading -- ``rows[:limit]`` with a negative ``limit`` is Python
        slicing, not "no limit"/"unlimited" like the other backends' native
        LIMIT quirks; it means "all but the last ``abs(limit)`` rows", a
        fourth distinct wrong answer. Same guard, same contract as every
        other store's list_nodes/export_nodes."""
        if limit <= 0:
            return []
        data = self._load("nodes")
        rows = list(data.values())
        if space:
            rows = [r for r in rows if r.get("space") == space]
        return rows[:limit]

    def delete_node_doc(self, space: str, node_id: str) -> bool:
        with self._lock:
            data = self._load("nodes")
            key = f"{space}::{node_id}"
            if key in data:
                del data[key]
                self._save("nodes", data)
                return True
            return False

    # ------------------------------------------------------------------
    # Source ingestion (mirrors MongoStore)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_str(s: str) -> str:
        """Remove surrogate characters that Windows MCP pipeline may introduce."""
        if not isinstance(s, str):
            return str(s)
        return s.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    def _read_source_state_row(
        self, source_id: str, row: Any
    ) -> SourceCopyState | None:
        if not isinstance(row, dict) or row.get("source_id") != source_id:
            return None
        helper_present = "source_integrity" in row
        complete, digest = classify_integrity(
            row.get("text"),
            row.get("source_integrity"),
            helper_present=helper_present,
            legacy_complete=(
                not helper_present
                and isinstance(row.get("text"), str)
                and len(row["text"]) < 4096
            ),
        )
        text = row.get("text")
        if not isinstance(text, str):
            return None
        return SourceCopyState("local", source_id, text, digest, complete)

    def read_source_state_exact(self, source_id: str) -> SourceCopyState | None:
        """Read one source and classify its backend-owned integrity state."""
        with self._lock:
            return self._read_source_state_row(source_id, self._load("sources").get(source_id))

    def _write_source(
        self,
        source_id: str,
        text: str,
        metadata: dict[str, Any],
        source_integrity: dict[str, str | int],
    ) -> str:
        with self._lock:
            data = self._load("sources")
            data[source_id] = {
                "source_id": source_id,
                "text": text,
                "metadata": metadata,
                "ingested_at": datetime.now(UTC).isoformat(),
                "source_integrity": source_integrity,
            }
            self._save("sources", data)
        return source_id

    def write_source_original(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        source_id, text, text_digest = require_original_source(source_id, text)
        return self._write_source(source_id, text, metadata, complete_integrity(text_digest))

    def write_source_copy(
        self,
        source_id: str,
        text: str,
        metadata: dict[str, Any],
        source_copy_state: SourceCopyState,
    ) -> str:
        source_id, text, text_digest = require_original_source(source_id, text)
        integrity = (
            complete_integrity(text_digest)
            if source_copy_state.complete
            and source_copy_state.text_digest == text_digest
            and source_copy_state.text == text
            else unknown_integrity()
        )
        return self._write_source(source_id, text, metadata, integrity)

    def rewrite_source_metadata(
        self, source_id: str, metadata: dict[str, Any], source_state: SourceCopyState
    ) -> str:
        """Rewrite metadata while one lock owns read, comparison, and write."""
        with self._lock:
            data = self._load("sources")
            current = self._read_source_state_row(source_id, data.get(source_id))
            if current is None or current != source_state:
                raise ValueError("source state changed before metadata rewrite")
            integrity = (
                complete_integrity(current.text_digest)
                if current.complete and current.text_digest
                else unknown_integrity()
            )
            data[source_id] = {
                "source_id": source_id,
                "text": current.text,
                "metadata": metadata,
                "ingested_at": datetime.now(UTC).isoformat(),
                "source_integrity": integrity,
            }
            self._save("sources", data)
        return source_id

    def upsert_source(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        from opencrab.stores._source_integrity import current_source_copy_state

        state = current_source_copy_state()
        if state is None:
            return self.write_source_original(source_id, text, metadata)
        return self.write_source_copy(source_id, text, metadata, state)

    def get_source_scoped_exact(
        self, source_id: str, pack_ids: list[str]
    ) -> dict[str, Any] | None:
        if not pack_ids:
            return None
        with self._lock:
            row = self._load("sources").get(source_id)
            metadata = row.get("metadata") if isinstance(row, dict) else None
            if (
                not isinstance(metadata, dict)
                or not isinstance(metadata.get("pack_id"), str)
                or metadata["pack_id"] not in set(pack_ids)
            ):
                return None
            state = self._read_source_state_row(source_id, row)
            if state is None:
                return None
            helper_present = "source_integrity" in row
            raw_state = row.get("source_integrity") if helper_present else None
            result = dict(row)
            result["_source_classification"] = source_classification(
                source_id,
                row.get("text"),
                metadata.get("pack_id"),
                raw_state,
                helper_present=helper_present,
                legacy_complete=(not helper_present and len(row["text"]) < 4096),
                known_state=state,
            )
            return result

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        row = self._load("sources").get(source_id)
        if row is None:
            return None
        return {key: value for key, value in row.items() if key != "source_integrity"}

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        """``limit <= 0``: returns ``[]`` -- same contract as ``list_nodes``
        above."""
        if limit <= 0:
            return []
        return [
            {key: value for key, value in row.items() if key != "source_integrity"}
            for row in list(self._load("sources").values())[:limit]
        ]

    # ------------------------------------------------------------------
    # Audit log (mirrors MongoStore)
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        subject_id: str | None,
        details: dict[str, Any],
    ) -> None:
        with self._lock:
            data = self._load("audit_log")
            ts = datetime.now(UTC).isoformat()
            entry_id = f"{event_type}::{ts}"
            data[entry_id] = {
                "event_type": event_type,
                "subject_id": subject_id,
                "details": details,
                "timestamp": ts,
            }
            self._save("audit_log", data)

    def get_audit_log(self, limit: int = 100, event_type: str | None = None) -> list[dict[str, Any]]:
        """``limit <= 0``: returns ``[]`` -- same contract as ``list_nodes``
        above."""
        if limit <= 0:
            return []
        data = self._load("audit_log")
        entries = sorted(data.values(), key=lambda e: e["timestamp"], reverse=True)
        if event_type:
            entries = [e for e in entries if e.get("event_type") == event_type]
        return entries[:limit]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def collection_stats(self) -> dict[str, int]:
        return {
            "nodes": len(self._load("nodes")),
            "sources": len(self._load("sources")),
            "audit_log": len(self._load("audit_log")),
        }
