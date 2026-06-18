"""Image context adapter with optional embedding backend and local fallback."""

from __future__ import annotations

import json
import math
import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencrab.common.hashing import file_sha256


@dataclass(slots=True)
class ImageContextResult:
    path: str
    backend: str
    status: str
    embedding_id: str
    vector: list[float]
    caption: str | None = None
    tags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_evidence(self, evidence_id: str | None = None) -> dict[str, Any]:
        path = Path(self.path)
        digest = self.metadata.get("sha256") or file_sha256(path)
        return {
            "evidence_id": evidence_id or f"evidence:clip:{digest[:16]}",
            "kind": "image_context",
            "source": {"path": str(path), "url": None, "title": path.name},
            "hash": f"sha256:{digest}",
            "collected_at": datetime.now(UTC).isoformat(),
            "parser": {"status": self.status, "method": "image_context", "warnings": self.warnings},
            "ocr": None,
            "clip": {
                "engine": self.backend,
                "embedding_id": self.embedding_id,
                "dimensions": len(self.vector),
                "caption": self.caption,
                "tags": self.tags,
                "warnings": self.warnings,
            },
            "location": {"document_id": None, "page": None, "section": None, "chunk_index": None},
            "links": {"document_id": None, "chunk_ids": [], "node_ids": [], "edge_ids": []},
            "metadata": self.metadata,
        }




def _normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [round(v / norm, 6) for v in values]


def _fallback_context(path: Path) -> ImageContextResult:
    digest = file_sha256(path)
    stat = path.stat()
    metadata: dict[str, Any] = {
        "sha256": digest,
        "bytes": stat.st_size,
        "mime_type": mimetypes.guess_type(path.name)[0],
    }
    features: list[float] = []
    tags: list[str] = []
    try:
        from PIL import Image, ImageStat  # type: ignore

        with Image.open(path) as image:
            rgb = image.convert("RGB")
            stat_values = ImageStat.Stat(rgb)
            means = [float(v) for v in stat_values.mean]
            stddev = [float(v) for v in stat_values.stddev]
            metadata.update({"width": image.width, "height": image.height, "mode": image.mode})
            features = _normalise([image.width, image.height, *means, *stddev])
            dominant = max(zip(["red", "green", "blue"], means), key=lambda item: item[1])[0]
            tags = ["image", f"dominant_{dominant}"]
    except Exception as exc:  # optional dependency path
        metadata["image_warning"] = str(exc)
        raw = bytes.fromhex(digest[:32])
        features = _normalise([float(byte) for byte in raw])
        tags = ["image", "hash_feature"]

    return ImageContextResult(
        path=str(path),
        backend="local-image-fingerprint",
        status="ok",
        embedding_id=f"clip:fallback:{digest[:16]}",
        vector=features,
        caption=f"Local image context for {path.name}",
        tags=tags,
        warnings=["Using deterministic local image fingerprint, not semantic CLIP"],
        metadata=metadata,
    )


def _sentence_transformer_context(path: Path, model_name: str) -> ImageContextResult | None:
    try:
        from PIL import Image  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None

    try:  # pragma: no cover - optional heavyweight backend
        model = SentenceTransformer(model_name)
        with Image.open(path) as image:
            vector = [float(v) for v in model.encode(image).tolist()]
        digest = file_sha256(path)
        return ImageContextResult(
            path=str(path),
            backend=f"sentence-transformers:{model_name}",
            status="ok",
            embedding_id=f"clip:st:{digest[:16]}",
            vector=_normalise(vector),
            caption=f"Embedding for {path.name}",
            tags=["image", "semantic_embedding"],
            metadata={"sha256": digest, "bytes": path.stat().st_size, "mime_type": mimetypes.guess_type(path.name)[0]},
        )
    except Exception as exc:
        result = _fallback_context(path)
        result.warnings.append(f"sentence-transformers backend failed: {exc}")
        return result


def build_image_context(
    path: str | Path,
    *,
    backend: str = "auto",
    model_name: str = "clip-ViT-B-32",
) -> ImageContextResult:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if backend not in {"auto", "sentence-transformers", "fingerprint"}:
        raise ValueError(f"unsupported image context backend: {backend}")

    if backend in {"auto", "sentence-transformers"}:
        result = _sentence_transformer_context(source, model_name)
        if result is not None:
            return result
        if backend == "sentence-transformers":
            raise RuntimeError("sentence-transformers/Pillow are required for backend=sentence-transformers")
    return _fallback_context(source)


def write_image_context(result: ImageContextResult, output: str | Path) -> dict[str, Any]:
    evidence = result.to_evidence()
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return evidence
