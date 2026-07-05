"""Local media processing adapters for LocalCrab."""

from .image_context import ImageContextResult, build_image_context
from .ocr import OcrResult, run_ocr

__all__ = ["OcrResult", "run_ocr", "ImageContextResult", "build_image_context"]
