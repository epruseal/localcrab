from pathlib import Path

from opencrab.media.image_context import build_image_context
from opencrab.media.ocr import run_ocr


def test_ocr_metadata_fallback(tmp_path: Path):
    image = tmp_path / "sample.bin"
    image.write_bytes(b"not really an image")
    result = run_ocr(image, backend="metadata")
    evidence = result.to_evidence()
    assert result.backend == "metadata"
    assert evidence["kind"] == "ocr_text"
    assert evidence["hash"].startswith("sha256:")


def test_image_context_fingerprint_fallback(tmp_path: Path):
    image = tmp_path / "sample.bin"
    image.write_bytes(b"image-ish bytes")
    result = build_image_context(image, backend="fingerprint")
    evidence = result.to_evidence()
    assert result.backend == "local-image-fingerprint"
    assert result.vector
    assert evidence["kind"] == "image_context"
    assert evidence["clip"]["dimensions"] == len(result.vector)
