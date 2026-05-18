"""Local media processing adapters for LocalCrab."""

from .ocr import OcrResult, run_ocr
from .image_context import ImageContextResult, build_image_context

__all__ = ["OcrResult", "run_ocr", "ImageContextResult", "build_image_context"]
