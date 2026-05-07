from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DocumentExtractResult:
    text: str
    warnings: list[str]
    source: str  # e.g. "pdf_text", "ocr", "none"


def _norm_text(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").replace("\x00", " ")).strip()


def extract_text_from_upload(
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    max_chars: int = 60_000,
) -> DocumentExtractResult:
    """
    Best-effort text extraction for receipts/invoices.
    - PDF: try embedded text via pypdf
    - Images: optional OCR via pytesseract (requires system tesseract)

    Returns extracted text (possibly empty) and warnings.
    """
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    warnings: list[str] = []

    is_pdf = name.endswith(".pdf") or "pdf" in ctype
    if is_pdf:
        txt = _pdf_text(data, warnings)
        txt = _norm_text(txt)
        if txt:
            return DocumentExtractResult(text=txt[:max_chars], warnings=warnings, source="pdf_text")
        warnings.append("No embedded text detected in PDF (may be scanned).")
        # fall through: could be scanned PDF; we don't OCR PDFs in-process by default.
        return DocumentExtractResult(text="", warnings=warnings, source="none")

    is_image = any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")) or (
        ctype.startswith("image/")
    )
    if is_image:
        txt = _image_ocr(data, warnings)
        txt = _norm_text(txt)
        return DocumentExtractResult(text=txt[:max_chars], warnings=warnings, source="ocr" if txt else "none")

    warnings.append("Unsupported file type. Upload a PDF or an image (png/jpg).")
    return DocumentExtractResult(text="", warnings=warnings, source="none")


def _pdf_text(data: bytes, warnings: list[str]) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        warnings.append("Missing dependency: pypdf. Install it to enable PDF text extraction.")
        return ""

    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for p in reader.pages:
            try:
                parts.append(p.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception:
        warnings.append("Failed to parse PDF.")
        return ""


def _image_ocr(data: bytes, warnings: list[str]) -> str:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        warnings.append("Missing dependency: pillow. Install it to enable image OCR.")
        return ""
    try:
        import pytesseract  # type: ignore
    except Exception:
        warnings.append(
            "Missing dependency: pytesseract (and system tesseract). Install/configure to enable OCR."
        )
        return ""

    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img) or ""
    except Exception:
        warnings.append("OCR failed on the uploaded image.")
        return ""

