"""
Contract tests for the media backend-cascade adapters (opencrab.media.ocr,
opencrab.media.image_context).

Neither easyocr, pytesseract, PIL, nor sentence-transformers are installed
in this environment, so the real "everything absent" fallback path is
exercised naturally. Backend presence is simulated via sys.modules
monkeypatching (as tests/test_media_adapters.py already does for easyocr) —
no packages are installed for these tests.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from opencrab.media import image_context, ocr

# ---------------------------------------------------------------------------
# Fake backend module builders
# ---------------------------------------------------------------------------


class _FakeImage:
    def __init__(self, width=10, height=10, mode="RGB"):
        self.width = width
        self.height = height
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def convert(self, mode):
        return self


class _FakeStat:
    def __init__(self, image):
        self.mean = [100.0, 120.0, 140.0]
        self.stddev = [10.0, 12.0, 14.0]


def _install_fake_pil(monkeypatch):
    pil_pkg = types.ModuleType("PIL")
    pil_pkg.Image = types.SimpleNamespace(open=lambda path: _FakeImage())
    pil_pkg.ImageStat = types.SimpleNamespace(Stat=_FakeStat)
    monkeypatch.setitem(sys.modules, "PIL", pil_pkg)


def _install_fake_pytesseract(monkeypatch, text="Hello World"):
    class _Output:
        DICT = "dict"

    fake = types.SimpleNamespace(
        image_to_string=lambda image, lang=None: text,
        image_to_data=lambda image, lang=None, output_type=None: {"conf": ["95", "80", "-1"]},
        Output=_Output,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)


def _install_fake_sentence_transformers(monkeypatch, encode_result=None, raises=False):
    class _Arr:
        def __init__(self, vals):
            self._vals = vals

        def tolist(self):
            return self._vals

    class _FakeModel:
        def __init__(self, name):
            self.name = name

        def encode(self, image):
            if raises:
                raise RuntimeError("encode failed")
            return _Arr(encode_result or [0.1, 0.2, 0.3])

    fake = types.SimpleNamespace(SentenceTransformer=_FakeModel)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)


# ---------------------------------------------------------------------------
# Normal: backend cascade selection
# ---------------------------------------------------------------------------


class TestOcrBackendCascade:
    def test_auto_falls_through_to_tesseract_when_easyocr_absent(self, monkeypatch, tmp_path: Path):
        # easyocr genuinely not installed in this env: _run_easyocr returns None.
        _install_fake_pil(monkeypatch)
        _install_fake_pytesseract(monkeypatch, text="tesseract text")
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = ocr.run_ocr(image, backend="auto")

        assert result.backend == "tesseract"
        assert result.text == "tesseract text"

    def test_auto_falls_through_past_easyocr_error_status_to_tesseract(self, monkeypatch, tmp_path: Path):
        """When easyocr IS present but errors (status='error'), auto mode
        must continue the cascade rather than returning the error result."""
        fake_easyocr = types.SimpleNamespace(
            Reader=lambda languages, gpu=False, verbose=False: (_ for _ in ()).throw(RuntimeError("reader init failed"))
        )
        monkeypatch.setitem(sys.modules, "easyocr", fake_easyocr)
        _install_fake_pil(monkeypatch)
        _install_fake_pytesseract(monkeypatch, text="fallback ok")
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = ocr.run_ocr(image, backend="auto")

        assert result.backend == "tesseract"
        assert result.text == "fallback ok"

    def test_auto_falls_through_to_metadata_when_both_absent(self, tmp_path: Path):
        # Real environment: easyocr, pytesseract both absent.
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = ocr.run_ocr(image, backend="auto")

        assert result.backend == "metadata"
        assert result.status == "skipped"


class TestImageContextBackendCascade:
    def test_auto_uses_fingerprint_when_sentence_transformers_absent(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = image_context.build_image_context(image, backend="auto")

        assert result.backend == "local-image-fingerprint"

    def test_auto_uses_sentence_transformers_when_present(self, monkeypatch, tmp_path: Path):
        _install_fake_pil(monkeypatch)
        _install_fake_sentence_transformers(monkeypatch, encode_result=[0.3, 0.4])
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = image_context.build_image_context(image, backend="auto")

        assert result.backend.startswith("sentence-transformers:")
        assert result.embedding_id.startswith("clip:st:")

    def test_sentence_transformers_encode_failure_falls_back_with_warning(self, monkeypatch, tmp_path: Path):
        _install_fake_pil(monkeypatch)
        _install_fake_sentence_transformers(monkeypatch, raises=True)
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        result = image_context.build_image_context(image, backend="auto")

        assert result.backend == "local-image-fingerprint"
        assert any("sentence-transformers backend failed" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestOcrErrors:
    def test_explicit_easyocr_backend_raises_when_unavailable(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        with pytest.raises(RuntimeError, match="easyocr"):
            ocr.run_ocr(image, backend="easyocr")

    def test_explicit_tesseract_backend_raises_when_unavailable(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        with pytest.raises(RuntimeError, match="tesseract"):
            ocr.run_ocr(image, backend="tesseract")

    def test_unsupported_backend_raises_value_error(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        with pytest.raises(ValueError, match="unsupported OCR backend"):
            ocr.run_ocr(image, backend="not-a-backend")

    def test_missing_path_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ocr.run_ocr(tmp_path / "does-not-exist.png")


class TestImageContextErrors:
    def test_explicit_sentence_transformers_backend_raises_when_unavailable(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        with pytest.raises(RuntimeError, match="sentence-transformers"):
            image_context.build_image_context(image, backend="sentence-transformers")

    def test_unsupported_backend_raises_value_error(self, tmp_path: Path):
        image = tmp_path / "sample.bin"
        image.write_bytes(b"bytes")

        with pytest.raises(ValueError, match="unsupported image context backend"):
            image_context.build_image_context(image, backend="not-a-backend")

    def test_missing_path_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            image_context.build_image_context(tmp_path / "does-not-exist.png")


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestNormaliseEasyocrLang:
    def test_maps_known_aliases(self):
        assert ocr._normalise_easyocr_lang("eng+kor") == ["en", "ko"]

    def test_deduplicates_and_preserves_order(self):
        assert ocr._normalise_easyocr_lang("eng,english,eng") == ["en"]

    def test_empty_string_defaults_to_en(self):
        assert ocr._normalise_easyocr_lang("") == ["en"]

    def test_unknown_language_code_passed_through(self):
        assert ocr._normalise_easyocr_lang("fra") == ["fra"]


class TestNormaliseVector:
    def test_zero_vector_guard_avoids_division_by_zero(self):
        assert image_context._normalise([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]

    def test_normal_vector_is_unit_length(self):
        import math

        result = image_context._normalise([3.0, 4.0])
        assert result == [0.6, 0.8]
        assert math.isclose(sum(v * v for v in result), 1.0, rel_tol=1e-6)
