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


def test_ocr_easyocr_backend(monkeypatch, tmp_path: Path):
    import sys
    import types

    image = tmp_path / "sample.bin"
    image.write_bytes(b"not really an image")

    class FakeReader:
        def __init__(self, languages, gpu=False, verbose=False):
            self.languages = languages

        def readtext(self, path, detail=1, paragraph=False):
            return [([[0, 0], [1, 0], [1, 1], [0, 1]], "한글 TEST 123", 0.91)]

    fake_easyocr = types.SimpleNamespace(Reader=FakeReader)
    monkeypatch.setitem(sys.modules, "easyocr", fake_easyocr)

    result = run_ocr(image, backend="easyocr", lang="kor+eng")

    assert result.backend == "easyocr"
    assert result.status == "ok"
    assert result.text == "한글 TEST 123"
    assert result.confidence == 0.91
    assert result.metadata["easyocr_languages"] == ["ko", "en"]
