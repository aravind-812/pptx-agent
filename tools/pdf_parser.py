"""Extract plain text from PDF files using pymupdf (fitz).

Handles:
- Text-based PDFs: fast, preserves reading order, includes tables
- Scanned PDFs: returns empty string (OCR not supported — caller should warn)
- Large PDFs: streams page-by-page, no full-file memory load
- Character-spaced artifacts: "T H A N K   Y O U" → "THANK YOU"
"""
import re
from pathlib import Path

# Match runs of single-letter-then-space patterns: "T H A N K" or "T h a n k".
# Require at least 4 single-letter tokens in a row to avoid false positives on
# initials like "J. R. Smith".
_CHAR_SPACED_RE = re.compile(r"(?:\b\w\b ){3,}\w\b")


def _collapse_char_spaced(text: str) -> str:
    """Detect and collapse 'T H A N K   Y O U' style PDF artifacts to 'THANK YOU'."""
    def collapse(match: re.Match) -> str:
        tokens = match.group(0).split()
        # Preserve word boundaries: 2+ spaces between letters → real space in output
        chunks: list[str] = []
        current: list[str] = []
        for tok in tokens:
            current.append(tok)
        # Single chunk for now — PDF rarely encodes word boundaries in char-spaced text
        return "".join(current)
    return _CHAR_SPACED_RE.sub(collapse, text)


def extract_pdf_text(path: str | Path) -> str:
    """Return extracted text from a PDF. Raises ImportError if pymupdf missing."""
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        raise ImportError(
            "pymupdf not installed. Run: pip install pymupdf"
        ) from exc

    doc = fitz.open(str(path))
    pages: list[str] = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            pages.append(_collapse_char_spaced(text))
    doc.close()

    full = "\n\n".join(pages)
    if not full.strip():
        raise ValueError(
            f"PDF at {path} contains no extractable text. "
            "It may be a scanned/image-only PDF — convert to text first."
        )
    return full


def is_pdf(path: str | Path) -> bool:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return True
    # Check magic bytes (%PDF-)
    try:
        with p.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def is_pdf_bytes(data: bytes) -> bool:
    return data[:5] == b"%PDF-"
