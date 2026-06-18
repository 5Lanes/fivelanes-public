"""
OCR for inbox image captures (screenshots, chat bubbles, etc.).

Uses system Tesseract via ``pytesseract``. Install on the host, e.g.::

    sudo apt install tesseract-ocr

Disable with ``IMAGE_OCR_DISABLE=1``. Tune thresholds with ``IMAGE_OCR_MIN_CHARS`` /
``IMAGE_OCR_MIN_WORDS``.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
from typing import Tuple

log = logging.getLogger(__name__)

_IMAGE_OCR_DISABLE = (os.getenv("IMAGE_OCR_DISABLE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _ocr_min_chars() -> int:
    raw = (os.getenv("IMAGE_OCR_MIN_CHARS") or "40").strip() or "40"
    try:
        return max(1, int(raw))
    except ValueError:
        return 40


def _ocr_min_words() -> int:
    raw = (os.getenv("IMAGE_OCR_MIN_WORDS") or "5").strip() or "5"
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def image_ocr_enabled() -> bool:
    if _IMAGE_OCR_DISABLE:
        return False
    return bool(shutil.which("tesseract"))


def _normalize_ocr_text(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines).strip()


def ocr_text_is_usable(text: str) -> bool:
    cleaned = _normalize_ocr_text(text)
    if len(cleaned) < _ocr_min_chars():
        return False
    words = re.findall(r"\w+", cleaned)
    return len(words) >= _ocr_min_words()


def extract_text_from_image_bytes(raw: bytes) -> Tuple[str, str]:
    """
    Run Tesseract on image bytes.

    Returns ``(transcript, error)``. ``error`` is empty when OCR succeeded; ``transcript``
  may still be empty if the image has no legible text.
    """
    if not raw:
        return "", "empty image"
    if _IMAGE_OCR_DISABLE:
        return "", "OCR disabled"
    if not shutil.which("tesseract"):
        return "", "tesseract not found on PATH"

    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return "", f"OCR dependencies not installed: {exc}"

    try:
        image = Image.open(io.BytesIO(raw))
    except Exception as exc:
        return "", f"could not decode image: {exc}"

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    configs = ("", "--psm 6")
    best = ""
    for extra in configs:
        try:
            kwargs = {"lang": "eng"}
            if extra:
                kwargs["config"] = extra
            candidate = pytesseract.image_to_string(image, **kwargs)
        except Exception as exc:
            return "", f"tesseract failed: {exc}"
        normalized = _normalize_ocr_text(candidate)
        if len(normalized) > len(best):
            best = normalized

    return best, ""
