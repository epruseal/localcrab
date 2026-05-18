"""OCR adapter with a deterministic local fallback.

The default backend is intentionally lightweight: if pytesseract and Pillow are
installed it extracts text with Tesseract; otherwise it still emits a stable
OCR evidence record with file metadata and a warning. That lets pack assembly
preserve traceability before heavyweight OCR is configured.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OcrResult:
    path: str
    backend: str
    status: str
    text: str = ""
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_evidence(self, evidence_id: str | None = None) -> dict[str, Any]:
        path = Path(self.path)
        digest = self.metadata.get("sha256") or _sha256(path)
        return {
            "evidence_id": evidence_id or f"evidence:ocr:{digest[:16]}",
            "kind": "ocr_text",
            "source": {"path": str(path), "url": None, "title": path.name},
            "hash": f"sha256:{digest}",
            "collected_at": datetime.now(UTC).isoformat(),
            "parser": {"status": self.status, "method": "ocr", "warnings": self.warnings},
            "ocr": {
                "engine": self.backend,
                "confidence": self.confidence,
                "text_length": len(self.text),
                "warnings": self.warnings,
            },
            "clip": None,
            "location": {"document_id": None, "page": None, "section": None, "chunk_index": None},
            "links": {"document_id": None, "chunk_ids": [], "node_ids": [], "edge_ids": []},
            "text": self.text,
            "metadata": self.metadata,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    metadata: dict[str, Any] = {
        "sha256": _sha256(path),
        "bytes": stat.st_size,
        "mime_type": mimetypes.guess_type(path.name)[0],
    }
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            metadata.update({"width": image.width, "height": image.height, "mode": image.mode})
    except Exception as exc:  # pragma: no cover - optional dependency path
        metadata["image_warning"] = str(exc)
    return metadata


def _run_tesseract(path: Path, lang: str) -> OcrResult | None:
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return None

    metadata = _image_metadata(path)
    try:
        with Image.open(path) as image:
            text = pytesseract.image_to_string(image, lang=lang).strip()
            data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)
        confidences = []
        for raw in data.get("conf", []):
            try:
                value = float(raw)
            except Exception:
                continue
            if value >= 0:
                confidences.append(value / 100)
        confidence = round(sum(confidences) / len(confidences), 4) if confidences else None
        return OcrResult(
            path=str(path),
            backend="tesseract",
            status="ok" if text else "empty",
            text=text,
            confidence=confidence,
            warnings=[] if text else ["tesseract returned no text"],
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - depends on system tesseract
        return OcrResult(
            path=str(path),
            backend="tesseract",
            status="error",
            text="",
            confidence=None,
            warnings=[str(exc)],
            metadata=metadata,
        )


def run_ocr(path: str | Path, *, backend: str = "auto", lang: str = "eng+kor") -> OcrResult:
    """Run OCR for one image/PDF path.

    `backend=auto` tries Tesseract first and falls back to metadata-only
    evidence when OCR dependencies or binaries are unavailable.
    """
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    if backend not in {"auto", "tesseract", "metadata"}:
        raise ValueError(f"unsupported OCR backend: {backend}")

    if backend in {"auto", "tesseract"}:
        result = _run_tesseract(source, lang)
        if result is not None:
            return result
        if backend == "tesseract":
            raise RuntimeError("pytesseract/Pillow are required for backend=tesseract")

    return OcrResult(
        path=str(source),
        backend="metadata",
        status="skipped",
        text="",
        confidence=None,
        warnings=["OCR backend unavailable; emitted metadata-only evidence"],
        metadata=_image_metadata(source),
    )


def write_ocr_evidence(result: OcrResult, output: str | Path) -> dict[str, Any]:
    evidence = result.to_evidence()
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return evidence
