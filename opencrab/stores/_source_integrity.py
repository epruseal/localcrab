"""Private source-integrity values shared by document stores."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_SOURCE_INTEGRITY_VERSION = 1
_HEX_DIGEST_LENGTH = 64
_UNSET = object()


class _JsonObject(dict[str, Any]):
    """A JSON object that retains duplicate keys during source parsing."""

    __slots__ = ("duplicate_keys", "child_has_duplicates")

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__()
        self.duplicate_keys: set[str] = set()
        self.child_has_duplicates = False
        for key, value in pairs:
            if key in self:
                self.duplicate_keys.add(key)
            if isinstance(value, _JsonObject) and (
                value.duplicate_keys or value.child_has_duplicates
            ):
                self.child_has_duplicates = True
            elif isinstance(value, list) and any(_has_json_duplicates(item) for item in value):
                self.child_has_duplicates = True
            self[key] = value


def _has_json_duplicates(value: Any) -> bool:
    if isinstance(value, _JsonObject):
        return bool(value.duplicate_keys or value.child_has_duplicates)
    if isinstance(value, list):
        return any(_has_json_duplicates(item) for item in value)
    return False


def source_json_object_pairs_hook(pairs: list[tuple[str, Any]]) -> _JsonObject:
    """Keep JSON duplicate-key evidence until LocalDocStore normalizes rows."""
    return _JsonObject(pairs)


def load_source_json(stream: Any) -> dict[str, Any]:
    """Load sources JSON while preserving duplicate keys until normalization."""
    return json.load(stream, object_pairs_hook=source_json_object_pairs_hook)


def decode_integrity_value(value: Any, *, encoded_json: bool) -> Any:
    """Decode one backend helper value without changing metadata decoding."""
    if encoded_json and isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=source_json_object_pairs_hook)
        except (TypeError, ValueError):
            return value
    if _has_json_duplicates(value) or not _is_strict_json_value(value):
        return unknown_integrity()
    return _normalize_json_value(value)


def normalize_source_rows(data: dict[str, Any]) -> dict[str, Any]:
    """Convert source helper duplicates and remove temporary JSON wrappers."""
    for source_id in list(data):
        row = data[source_id]
        if isinstance(row, _JsonObject):
            helper = row.get("source_integrity")
            if "source_integrity" in row.duplicate_keys:
                row["source_integrity"] = unknown_integrity()
            elif "source_integrity" in row:
                row["source_integrity"] = decode_integrity_value(
                    helper, encoded_json=False
                )
        data[source_id] = _normalize_json_value(row)
    if isinstance(data, _JsonObject):
        plain_root = dict(data)
        del data
        return plain_root
    return data


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, list):
        for index, child in enumerate(value):
            value[index] = _normalize_json_value(child)
        return value
    if isinstance(value, _JsonObject):
        for key in list(value):
            value[key] = _normalize_json_value(value[key])
        plain_value = dict(value)
        del value
        return plain_value
    if isinstance(value, dict):
        for key in list(value):
            value[key] = _normalize_json_value(value[key])
    return value


@dataclass(frozen=True)
class SourceCopyState:
    """A backend-created snapshot for a source copy operation."""

    backend_kind: str
    source_id: str
    text: str
    text_digest: str | None
    complete: bool
    raw_state_token: Any | None = None


_copy_state: ContextVar[SourceCopyState | None] = ContextVar("source_copy_state", default=None)


@contextmanager
def source_copy_context(state: SourceCopyState):
    """Expose backend-owned copy state to one existing public write call."""
    token = _copy_state.set(state)
    try:
        yield
    finally:
        _copy_state.reset(token)


def current_source_copy_state() -> SourceCopyState | None:
    """Return copy state bound by the writer, not by caller metadata."""
    return _copy_state.get()


def strict_utf8_digest(value: Any) -> str | None:
    """Return a SHA-256 digest only for a strict UTF-8 string."""
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(encoded).hexdigest()


def require_original_source(source_id: Any, text: Any) -> tuple[str, str, str]:
    """Validate an ordinary original-content write."""
    source_digest = strict_utf8_digest(source_id)
    text_digest = strict_utf8_digest(text)
    if source_digest is None or text_digest is None:
        raise ValueError("source_id and text must be strict UTF-8 strings")
    return source_id, text, text_digest


def complete_integrity(text_digest: str) -> dict[str, str | int]:
    """Build the persisted complete state from a stored text digest."""
    return {
        "version": _SOURCE_INTEGRITY_VERSION,
        "state": "complete",
        "text_digest": text_digest,
    }


def unknown_integrity() -> dict[str, str | int]:
    """Build the persisted state for content whose integrity is unknown."""
    return {"version": _SOURCE_INTEGRITY_VERSION, "state": "unknown"}


def _is_strict_json_value(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_is_strict_json_value(item) for item in value.values())
    if isinstance(value, list):
        return all(_is_strict_json_value(item) for item in value)
    return True


def source_state_kind(structural_state: Any, *, helper_present: bool, legacy_complete: bool) -> str:
    """Classify stored integrity shape without promoting an invalid state."""
    if not helper_present:
        return "legacy_absent" if legacy_complete else "absent"
    if structural_state is None:
        return "present_null"
    if not isinstance(structural_state, dict) or not _is_strict_json_value(structural_state):
        return "malformed"
    version = structural_state.get("version")
    state = structural_state.get("state")
    if type(version) is not int or not isinstance(state, str):
        return "malformed"
    if version != _SOURCE_INTEGRITY_VERSION:
        return "unsupported_version"
    if state == "unknown" and set(structural_state) == {"version", "state"}:
        return "unknown"
    if state == "complete" and set(structural_state) == {"version", "state", "text_digest"}:
        return "complete"
    return "malformed"


def classify_integrity(
    text: Any,
    structural_state: Any,
    *,
    helper_present: bool,
    legacy_complete: bool,
) -> tuple[bool, str | None]:
    """Classify a stored row without promoting malformed or unknown state."""
    actual_digest = strict_utf8_digest(text)
    if actual_digest is None:
        return False, None
    kind = source_state_kind(
        structural_state, helper_present=helper_present, legacy_complete=legacy_complete
    )
    if kind == "legacy_absent":
        return True, actual_digest
    if kind != "complete":
        return False, actual_digest
    recorded_digest = structural_state.get("text_digest")
    if (
        not isinstance(recorded_digest, str)
        or len(recorded_digest) != _HEX_DIGEST_LENGTH
        or any(char not in "0123456789abcdef" for char in recorded_digest)
    ):
        return False, actual_digest
    return recorded_digest == actual_digest, actual_digest


def source_classification(
    source_id: Any,
    text: Any,
    pack_id: Any,
    structural_state: Any,
    *,
    helper_present: bool,
    legacy_complete: bool,
    known_state: SourceCopyState | None = None,
) -> dict[str, Any]:
    """Build private backend classification for one scoped source snapshot."""
    state_kind = source_state_kind(
        structural_state, helper_present=helper_present, legacy_complete=legacy_complete
    )
    complete, digest = classify_integrity(
        text,
        structural_state,
        helper_present=helper_present,
        legacy_complete=legacy_complete,
    )
    if known_state is not None and (
        known_state.complete != complete or known_state.text_digest != digest
    ):
        complete = False
    return {
        "source_id": source_id,
        "text": text,
        "pack_id": pack_id,
        "text_digest": digest,
        "complete": complete,
        "state_kind": state_kind,
    }


def validate_source_row_payload(
    record: Any,
    *,
    permitted_packs: Any,
    expected_source_id: Any,
    expected_text: Any = _UNSET,
) -> tuple[str, str, str, str] | None:
    """Return one exact scoped source payload without a backend lookup."""
    if (
        not isinstance(record, Mapping)
        or not isinstance(expected_source_id, str)
        or strict_utf8_digest(expected_source_id) is None
        or isinstance(permitted_packs, (str, bytes, Mapping))
    ):
        return None
    try:
        scope = set(permitted_packs)
    except TypeError:
        return None
    if not scope or any(
        not isinstance(pack_id, str) or strict_utf8_digest(pack_id) is None
        for pack_id in scope
    ):
        return None
    source_id = record.get("source_id")
    text = record.get("text")
    metadata = record.get("metadata")
    classification = record.get("_source_classification")
    if (
        not isinstance(source_id, str)
        or strict_utf8_digest(source_id) is None
        or source_id != expected_source_id
        or not isinstance(text, str)
        or strict_utf8_digest(text) is None
        or not isinstance(metadata, Mapping)
        or not isinstance(classification, Mapping)
    ):
        return None
    if expected_text is not _UNSET and (
        not isinstance(expected_text, str)
        or strict_utf8_digest(expected_text) is None
        or text != expected_text
    ):
        return None
    pack_id = metadata.get("pack_id")
    digest = strict_utf8_digest(text)
    if (
        not isinstance(pack_id, str)
        or strict_utf8_digest(pack_id) is None
        or pack_id not in scope
        or classification.get("source_id") != source_id
        or classification.get("text") != text
        or classification.get("pack_id") != pack_id
        or classification.get("text_digest") != digest
        or classification.get("complete") is not True
    ):
        return None
    state_kind = classification.get("state_kind")
    if not isinstance(state_kind, str) or state_kind not in {"complete", "legacy_absent"}:
        return None
    return source_id, pack_id, text, digest
