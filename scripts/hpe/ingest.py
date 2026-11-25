"""PDF ingestion: extraction, page/section indexing, quality scoring, quote checks.

The ingestion layer is deterministic and never calls an LLM. It is intentionally
conservative: if extraction quality is poor the caller must opt in before the
protocol proceeds, so a scanned PDF cannot silently become a confident review.

Public entry points
-------------------
``ingest_pdf``   extract + sectionize + write artifacts + manifest
``QuoteIndex``   normalized quote verification against extracted page text
``doctor``       dependency/engine availability for ``hpe.py doctor``
"""

from __future__ import annotations

import importlib
import json
import math
import re
import shutil
import statistics
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import MAX_PDF_BYTES, MAX_PDF_PAGES, SECTIONIZER_VERSION
from .errors import IngestError
from .util import (
    atomic_write_json,
    atomic_write_text,
    detect_language,
    ensure_dir,
    estimate_tokens,
    normalize_whitespace,
    sha256_file,
    strip_control_chars,
    unique_preserve,
    utc_now_iso,
)

SCHEMA_VERSION = "hpe/v2"
PAGE_MARKER_RE = re.compile(r"\[\[PAGE\s+(\d+)\]\]")
PAGE_MARKER_TEMPLATE = "\n[[PAGE {page}]]\n"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class HeadingCandidate:
    page: int
    text: str
    size: float = 0.0
    bold: bool = False
    y: float = 0.0


@dataclass
class PageText:
    page: int
    text: str
    width: float = 0.0
    height: float = 0.0
    headings: list[HeadingCandidate] = field(default_factory=list)
    chars: int = 0

    def __post_init__(self) -> None:
        self.chars = len(self.text)


@dataclass
class Section:
    id: str
    kind: str
    heading: str
    text: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    tokens_est: int

    def to_manifest(self, text_rel_path: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "heading": self.heading,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "tokens_est": self.tokens_est,
            "text_path": text_rel_path,
        }


@dataclass
class IngestedPaper:
    paper_id: str
    pdf_path: Path
    title: str
    authors: list[str]
    year: int | None
    pages: list[PageText]
    sections: list[Section]
    full_text: str
    manifest: dict[str, Any]
    quality: dict[str, Any]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def language(self) -> str:
        return detect_language(self.full_text)


@dataclass
class QuoteMatch:
    status: str
    page: int | None = None
    score: float = 0.0
    snippet: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"exact", "normalized", "segmented", "fuzzy"}

    @property
    def strong(self) -> bool:
        return self.status in {"exact", "normalized", "segmented"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "page": self.page,
            "score": round(self.score, 3),
            "snippet": self.snippet,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------


def _module_version(module_name: str) -> str:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return ""
    for attr in ("__version__", "version"):
        value = getattr(module, attr, None)
        if value:
            return str(value)
    doc = getattr(module, "__doc__", "") or ""
    return doc.splitlines()[0].strip() if doc else ""


def available_engines() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for engine, module in (("pymupdf", "fitz"), ("pdfplumber", "pdfplumber"), ("pypdf", "pypdf")):
        try:
            importlib.import_module(module)
        except Exception as exc:  # pragma: no cover - environment dependent
            out[engine] = {"available": False, "error": str(exc)}
        else:
            out[engine] = {"available": True, "version": _module_version(module)}
    out["pdftotext"] = {
        "available": shutil.which("pdftotext") is not None,
        "version": (shutil.which("pdftotext") or ""),
    }
    out["ocr"] = ocr_status()
    return out


def ocr_status() -> dict[str, Any]:
    try:
        import pytesseract  # type: ignore
    except Exception as exc:
        return {"available": False, "error": f"pytesseract not installed: {exc}"}
    binary = shutil.which("tesseract")
    if not binary:
        return {"available": False, "error": "tesseract binary not found on PATH"}
    return {"available": True, "version": f"pytesseract {getattr(pytesseract, '__version__', '?')}"}


def doctor() -> dict[str, Any]:
    from .validate import schema_validation_available

    engines = available_engines()
    return {
        "python": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}.{__import__('sys').version_info.micro}",
        "engines": engines,
        "jsonschema": schema_validation_available(),
        "sectionizer_version": SECTIONIZER_VERSION,
        "page_images": {
            "available": bool((engines.get("pymupdf") or {}).get("available")),
            "modes": ["off", "auto", "all"],
            "default_mode": "auto",
        },
        "limits": {"max_pdf_bytes": MAX_PDF_BYTES, "max_pdf_pages": MAX_PDF_PAGES},
    }


# ---------------------------------------------------------------------------
# Extraction backends
# ---------------------------------------------------------------------------


def _clean_page_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.replace("\u00ad", "")  # soft hyphen
    text = strip_control_chars(text)
    # Normalize exotic whitespace but keep paragraph breaks.
    text = re.sub(r"[ \t\u00a0\u2000-\u200a]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _pymupdf_extract(pdf_path: Path) -> tuple[list[PageText], list[str]]:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise IngestError(f"PyMuPDF is not available: {exc}") from exc

    warnings: list[str] = []
    pages: list[PageText] = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        raise IngestError(f"PyMuPDF cannot open the PDF: {exc}") from exc
    try:
        if doc.is_encrypted:
            if not doc.authenticate(""):
                raise IngestError("The PDF is encrypted; decrypt it before review.")
        if doc.page_count > MAX_PDF_PAGES:
            raise IngestError(f"PDF has {doc.page_count} pages (limit {MAX_PDF_PAGES}).")
        for index, page in enumerate(doc):
            number = index + 1
            try:
                text = page.get_text("text", sort=True) or ""
            except TypeError:  # older PyMuPDF
                text = page.get_text("text") or ""
            headings: list[HeadingCandidate] = []
            try:
                data = page.get_text("dict")
                for block in data.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        if not spans:
                            continue
                        line_text = "".join(str(span.get("text", "")) for span in spans).strip()
                        if not line_text:
                            continue
                        size = max(float(span.get("size", 0.0)) for span in spans)
                        bold = any(bool(int(span.get("flags", 0)) & 16) for span in spans)
                        y = float(line.get("bbox", [0, 0, 0, 0])[1])
                        headings.append(HeadingCandidate(page=number, text=line_text, size=size, bold=bold, y=y))
            except Exception as exc:  # non-fatal; regex heading fallback still works
                warnings.append(f"page {number}: font metadata unavailable ({exc})")
            pages.append(
                PageText(
                    page=number,
                    text=_clean_page_text(text),
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    headings=headings,
                )
            )
    finally:
        doc.close()
    return pages, warnings


def _pdfplumber_extract(pdf_path: Path) -> tuple[list[PageText], list[str]]:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise IngestError(f"pdfplumber is not available: {exc}") from exc
    warnings: list[str] = []
    pages: list[PageText] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise IngestError(f"PDF has {len(pdf.pages)} pages (limit {MAX_PDF_PAGES}).")
        for index, page in enumerate(pdf.pages):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                warnings.append(f"page {index + 1}: pdfplumber extraction failed ({exc})")
                text = ""
            pages.append(
                PageText(
                    page=index + 1,
                    text=_clean_page_text(text),
                    width=float(page.width or 0),
                    height=float(page.height or 0),
                )
            )
    return pages, warnings


def _pypdf_extract(pdf_path: Path) -> tuple[list[PageText], list[str]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise IngestError(f"pypdf is not available: {exc}") from exc
    warnings: list[str] = []
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise IngestError("The PDF is encrypted; decrypt it before review.") from exc
    if len(reader.pages) > MAX_PDF_PAGES:
        raise IngestError(f"PDF has {len(reader.pages)} pages (limit {MAX_PDF_PAGES}).")
    pages: list[PageText] = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"page {index + 1}: pypdf extraction failed ({exc})")
            text = ""
        pages.append(PageText(page=index + 1, text=_clean_page_text(text)))
    return pages, warnings


def _pdftotext_extract(pdf_path: Path) -> tuple[list[PageText], list[str]]:
    binary = shutil.which("pdftotext")
    if not binary:
        raise IngestError("pdftotext is not installed")
    try:
        completed = subprocess.run(
            [binary, "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IngestError("pdftotext timed out after 180s") from exc
    if completed.returncode != 0:
        raise IngestError(f"pdftotext failed: {completed.stderr.decode('utf-8', 'replace')[:400]}")
    raw = completed.stdout.decode("utf-8", "replace")
    chunks = raw.split("\f")
    if chunks and not chunks[-1].strip():
        chunks = chunks[:-1]
    if len(chunks) > MAX_PDF_PAGES:
        raise IngestError(f"PDF has {len(chunks)} pages (limit {MAX_PDF_PAGES}).")
    pages = [PageText(page=i + 1, text=_clean_page_text(chunk)) for i, chunk in enumerate(chunks)]
    return pages, []


_EXTRACTORS = {
    "pymupdf": _pymupdf_extract,
    "pdfplumber": _pdfplumber_extract,
    "pypdf": _pypdf_extract,
    "pdftotext": _pdftotext_extract,
}

_ENGINE_ORDER = ("pymupdf", "pdfplumber", "pypdf", "pdftotext")
_ENGINE_RANK = {"good": 3, "partial": 2, "poor": 1}


def _rough_text_score(pages: Sequence[PageText]) -> float:
    total = sum(len(p.text) for p in pages)
    replacement = sum(p.text.count("\ufffd") for p in pages)
    return total - replacement * 20


def extract_pages(
    pdf_path: Path,
    *,
    engine: str | None = None,
    ocr: bool = False,
    ocr_max_pages: int = 30,
) -> tuple[list[PageText], str, str, list[str]]:
    """Extract page texts, trying fallbacks when the primary result is poor.

    Returns ``(pages, engine_name, engine_version, warnings)``.
    """

    if engine and engine not in _EXTRACTORS:
        raise IngestError(f"Unknown PDF engine '{engine}'. Known: {', '.join(_EXTRACTORS)}")

    candidates: list[tuple[str, list[PageText], list[str]]] = []
    warnings: list[str] = []
    engines = [engine] if engine else list(_ENGINE_ORDER)
    errors: list[str] = []
    for name in engines:
        try:
            pages, extra_warnings = _EXTRACTORS[name](pdf_path)
        except IngestError as exc:
            errors.append(f"{name}: {exc.message}")
            continue
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        warnings.extend(extra_warnings)
        if not pages:
            errors.append(f"{name}: produced no pages")
            continue
        quality = assess_pages(pages)
        candidates.append((name, pages, extra_warnings))
        # Stop early when a good extraction is available; otherwise keep trying.
        if quality["quality"] == "good":
            break
        if engine:
            break

    if not candidates:
        if ocr:
            pages, ocr_warnings = _ocr_extract(pdf_path, max_pages=ocr_max_pages)
            return pages, "ocr", "tesseract", warnings + ocr_warnings
        raise IngestError(
            "No PDF engine could extract text. Install PyMuPDF (recommended), or use --ocr if tesseract is available.",
            detail={"errors": errors},
        )

    # Select best candidate: quality rank first, then text volume.
    def score(candidate: tuple[str, list[PageText], list[str]]) -> tuple[int, float]:
        quality = assess_pages(candidate[1])
        return (_ENGINE_RANK[quality["quality"]], _rough_text_score(candidate[1]))

    best_engine, best_pages, _ = max(candidates, key=score)
    quality = assess_pages(best_pages)
    if quality["quality"] == "poor" and ocr:
        try:
            ocr_pages, ocr_warnings = _ocr_extract(pdf_path, max_pages=ocr_max_pages)
            if _ENGINE_RANK[assess_pages(ocr_pages)["quality"]] >= _ENGINE_RANK[quality["quality"]]:
                return ocr_pages, "ocr", "tesseract", warnings + ocr_warnings
        except IngestError as exc:
            warnings.append(f"OCR fallback failed: {exc.message}")
    version = _engine_version(best_engine)
    return best_pages, best_engine, version, warnings


def _engine_version(engine: str) -> str:
    if engine == "pymupdf":
        return _module_version("fitz")
    if engine == "pdfplumber":
        return _module_version("pdfplumber")
    if engine == "pypdf":
        return _module_version("pypdf")
    if engine == "pdftotext":
        return "system"
    return "tesseract"


def _ocr_extract(pdf_path: Path, *, max_pages: int) -> tuple[list[PageText], list[str]]:
    status = ocr_status()
    if not status.get("available"):
        raise IngestError(f"OCR is not available: {status.get('error')}")
    try:
        import fitz  # type: ignore
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        import io
    except Exception as exc:  # pragma: no cover
        raise IngestError(f"OCR dependencies are incomplete: {exc}") from exc

    warnings: list[str] = []
    pages: list[PageText] = []
    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count > MAX_PDF_PAGES:
            raise IngestError(f"PDF has {doc.page_count} pages (limit {MAX_PDF_PAGES}).")
        if doc.page_count > max_pages:
            warnings.append(f"OCR limited to the first {max_pages} of {doc.page_count} pages.")
        for index in range(min(doc.page_count, max_pages)):
            page = doc[index]
            pix = page.get_pixmap(dpi=200)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                text = pytesseract.image_to_string(image)
            except Exception as exc:  # pragma: no cover
                warnings.append(f"page {index + 1}: OCR failed ({exc})")
                text = ""
            pages.append(PageText(page=index + 1, text=_clean_page_text(text), width=float(page.rect.width), height=float(page.rect.height)))
    finally:
        doc.close()
    return pages, warnings


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


def assess_pages(pages: Sequence[PageText]) -> dict[str, Any]:
    page_count = len(pages)
    total_chars = sum(len(p.text) for p in pages)
    chars_per_page = total_chars / max(1, page_count)
    empty_pages = sum(1 for p in pages if len(p.text.strip()) < 30)
    replacement = sum(p.text.count("\ufffd") for p in pages)
    replacement_ratio = replacement / max(1, total_chars)
    letters = sum(1 for p in pages for ch in p.text if ch.isalpha())
    letter_ratio = letters / max(1, total_chars)
    warnings: list[str] = []

    if page_count == 0:
        quality = "poor"
        warnings.append("PDF has no pages.")
    elif total_chars < 200 or chars_per_page < 120:
        quality = "poor"
        warnings.append(f"Very little text extracted ({total_chars} chars, {chars_per_page:.0f}/page); the PDF may be scanned.")
    elif empty_pages / page_count > 0.5:
        quality = "poor"
        warnings.append(f"{empty_pages}/{page_count} pages have almost no text.")
    elif replacement_ratio > 0.05 or letter_ratio < 0.35:
        quality = "poor"
        warnings.append(f"Extracted text looks garbled (replacement {replacement_ratio:.1%}, letters {letter_ratio:.1%}).")
    elif chars_per_page < 700 or replacement_ratio > 0.01 or empty_pages / page_count > 0.2:
        quality = "partial"
        warnings.append(f"Partial extraction quality ({chars_per_page:.0f} chars/page, {empty_pages}/{page_count} near-empty pages).")
    else:
        quality = "good"

    return {
        "quality": quality,
        "chars": total_chars,
        "chars_per_page": round(chars_per_page, 2),
        "pages": page_count,
        "empty_pages": empty_pages,
        "replacement_ratio": round(replacement_ratio, 4),
        "letter_ratio": round(letter_ratio, 4),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Margin / header-footer cleanup
# ---------------------------------------------------------------------------


def strip_repeated_margins(pages: list[PageText], *, min_share: float = 0.35) -> list[PageText]:
    """Remove repeated short header/footer lines and bare page numbers.

    This is deliberately conservative: only the first/last two lines of each
    page are considered, and a line must recur on at least ``min_share`` of
    pages to be removed. Page numbers are removed independently.
    """

    if len(pages) < 4:
        return pages
    counts: dict[str, int] = {}
    for page in pages:
        lines = [line.strip() for line in page.text.splitlines() if line.strip()]
        seen = set(lines[:2] + lines[-2:])
        for line in seen:
            if len(line) <= 140:
                counts[line] = counts.get(line, 0) + 1
    threshold = max(3, int(math.ceil(len(pages) * min_share)))
    repeated = {line for line, count in counts.items() if count >= threshold}
    page_number_re = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.IGNORECASE)

    cleaned: list[PageText] = []
    for page in pages:
        lines = page.text.splitlines()
        if not lines:
            cleaned.append(page)
            continue
        start, end = 0, len(lines)
        for _ in range(2):
            if start < end and (lines[start].strip() in repeated or page_number_re.match(lines[start])):
                start += 1
        for _ in range(2):
            if end > start and (lines[end - 1].strip() in repeated or page_number_re.match(lines[end - 1])):
                end -= 1
        text = "\n".join(lines[start:end]).strip("\n")
        cleaned.append(
            PageText(
                page=page.page,
                text=text,
                width=page.width,
                height=page.height,
                headings=page.headings,
            )
        )
    return cleaned


# ---------------------------------------------------------------------------
# Heading detection and sectionization
# ---------------------------------------------------------------------------

_KNOWN_HEADINGS = [
    ("abstract", r"abstract|摘要"),
    ("introduction", r"introduction|引言|绪论|前言"),
    ("related_work", r"related\s+works?|background|literature\s+review|prior\s+work|相关工作|研究现状|文献综述"),
    ("method", r"method(?:s|ology)?|approach|proposed\s+(?:method|framework|model)|our\s+(?:method|approach|framework)|方法|模型|框架"),
    ("theory", r"theory|theoretical\s+analysis|theorem|proof|analysis|理论|定理|证明"),
    ("experiments", r"experiments?|experimental\s+(?:setup|evaluation)|evaluation|实验|评估|实验设置"),
    ("results", r"results?|findings?|结果|发现"),
    ("discussion", r"discussion|讨论"),
    ("conclusion", r"conclusions?|concluding\s+remarks|总结|结论"),
    ("references", r"references?|bibliography|参考文献"),
    ("appendix", r"appendix|appendices|supplementary\s+(?:material|information)|附录|补充材料"),
]

_HEADING_NUMBER_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+)\.?\s+")
_HEADING_TRAILING_RE = re.compile(r"[.:;]\s*$")
_REFERENCE_LINE_RE = re.compile(r"^\s*(?:\[\d+\]|\d+\.)\s+\S")


def _heading_core(text: str) -> str:
    core = _HEADING_NUMBER_RE.sub("", text.strip())
    core = _HEADING_TRAILING_RE.sub("", core)
    return core.strip()


def classify_heading(text: str) -> str:
    core = _heading_core(text).lower()
    if not core:
        return "other"
    for kind, pattern in _KNOWN_HEADINGS:
        if re.fullmatch(pattern, core) or re.match(rf"^(?:{pattern})\b", core):
            return kind
    return "other"


def _looks_like_heading_line(text: str) -> bool:
    text = text.strip()
    if not (2 <= len(text) <= 120):
        return False
    if _HEADING_TRAILING_RE.search(text):
        return False
    if _REFERENCE_LINE_RE.match(text):
        return False
    if re.fullmatch(r"[\d\s.,%()\-]+", text):
        return False
    if text.count(" ") > 16:
        return False
    if "=" in text and len(text) > 40:
        return False
    return True


def _body_font_size(pages: Sequence[PageText]) -> float:
    sizes: list[float] = []
    for page in pages:
        for candidate in page.headings:
            if len(candidate.text) >= 30:
                sizes.append(candidate.size)
    if not sizes:
        return 10.0
    return float(statistics.median(sizes))


def _heading_candidates(pages: Sequence[PageText]) -> list[HeadingCandidate]:
    body_size = _body_font_size(pages)
    candidates: list[HeadingCandidate] = []
    for page in pages:
        # Font metadata first.
        for candidate in page.headings:
            text = candidate.text.strip()
            if not _looks_like_heading_line(text):
                continue
            kind = classify_heading(text)
            font_signal = candidate.size >= body_size * 1.12 or (candidate.bold and candidate.size >= body_size * 1.02)
            if kind != "other" or font_signal:
                candidates.append(HeadingCandidate(page=page.page, text=text, size=candidate.size, bold=candidate.bold, y=candidate.y))
        # Regex fallback for engines without font metadata and for headings the
        # font heuristic misses.
        for line in page.text.splitlines():
            text = line.strip()
            if not _looks_like_heading_line(text):
                continue
            kind = classify_heading(text)
            if kind == "other":
                continue
            if any(c.page == page.page and normalize_whitespace(c.text) == normalize_whitespace(text) for c in candidates):
                continue
            candidates.append(HeadingCandidate(page=page.page, text=text, size=body_size, bold=False))

    # Deduplicate by page + normalized text, preserving first occurrence.
    seen: set[tuple[int, str]] = set()
    out: list[HeadingCandidate] = []
    for candidate in sorted(candidates, key=lambda c: (c.page, c.y)):
        key = (candidate.page, normalize_whitespace(candidate.text).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _build_full_text(pages: Sequence[PageText]) -> tuple[str, dict[int, tuple[int, int]]]:
    parts: list[str] = []
    spans: dict[int, tuple[int, int]] = {}
    offset = 0
    for page in pages:
        marker = PAGE_MARKER_TEMPLATE.format(page=page.page)
        parts.append(marker)
        offset += len(marker)
        start = offset
        parts.append(page.text)
        offset += len(page.text)
        spans[page.page] = (start, offset)
        parts.append("\n")
        offset += 1
    return "".join(parts), spans


def _find_heading_offset(candidate: HeadingCandidate, page: PageText, page_span: tuple[int, int], full_text: str) -> int:
    start, _ = page_span
    needle = normalize_whitespace(candidate.text)
    page_text = page.text
    # Prefer a line-level exact match.
    cursor = 0
    for line in page_text.splitlines():
        if normalize_whitespace(line) == needle:
            return start + cursor + (len(line) - len(line.lstrip()))
        cursor += len(line) + 1
    # Fallback: substring search.
    idx = page_text.find(candidate.text)
    if idx >= 0:
        return start + idx
    idx = full_text.find(candidate.text, page_span[0], page_span[1])
    return idx if idx >= 0 else start


def _section_kind_from_heading(heading: str) -> str:
    return classify_heading(heading)


def _fallback_sections(pages: Sequence[PageText], full_text: str, spans: dict[int, tuple[int, int]]) -> list[Section]:
    sections: list[Section] = []
    window = 3
    for start_index in range(0, len(pages), window):
        group = pages[start_index : start_index + window]
        if not group:
            continue
        first, last = group[0], group[-1]
        char_start = spans[first.page][0]
        char_end = spans[last.page][1]
        text = full_text[char_start:char_end].strip("\n")
        heading = f"Pages {first.page}-{last.page}"
        sections.append(
            Section(
                id="",
                kind="other",
                heading=heading,
                text=text,
                page_start=first.page,
                page_end=last.page,
                char_start=char_start,
                char_end=char_end,
                tokens_est=estimate_tokens(text),
            )
        )
    return sections


def sectionize(pages: Sequence[PageText]) -> list[Section]:
    full_text, spans = _build_full_text(pages)
    candidates = _heading_candidates(pages)
    if not candidates:
        sections = _fallback_sections(pages, full_text, spans)
    else:
        # Drop candidates that are almost certainly title-page noise.
        filtered: list[HeadingCandidate] = []
        for candidate in candidates:
            if candidate.page == 1 and candidate.text.lower().startswith(("arxiv:", "doi:", "http")):
                continue
            filtered.append(candidate)
        candidates = filtered or candidates

        positions: list[tuple[int, HeadingCandidate]] = []
        for candidate in candidates:
            page = pages[candidate.page - 1]
            offset = _find_heading_offset(candidate, page, spans[candidate.page], full_text)
            positions.append((offset, candidate))
        positions.sort(key=lambda item: (item[0], item[1].page, item[1].y))
        # De-duplicate offsets that point into the same line.
        deduped: list[tuple[int, HeadingCandidate]] = []
        for offset, candidate in positions:
            if deduped and abs(offset - deduped[-1][0]) <= 2:
                continue
            deduped.append((offset, candidate))
        positions = deduped

        sections = []
        for index, (offset, candidate) in enumerate(positions):
            end = positions[index + 1][0] if index + 1 < len(positions) else len(full_text)
            text = full_text[offset:end].strip("\n")
            if not text:
                continue
            page_start = _page_for_offset(offset, spans, len(pages))
            page_end = _page_for_offset(max(offset, end - 1), spans, len(pages))
            if not PAGE_MARKER_RE.search(text.splitlines()[0] if text.splitlines() else ""):
                text = PAGE_MARKER_TEMPLATE.format(page=page_start).strip("\n") + "\n" + text
            sections.append(
                Section(
                    id="",
                    kind=_section_kind_from_heading(candidate.text),
                    heading=normalize_whitespace(candidate.text),
                    text=text,
                    page_start=page_start,
                    page_end=page_end,
                    char_start=offset,
                    char_end=end,
                    tokens_est=estimate_tokens(text),
                )
            )

    # Ensure an abstract section exists when the paper clearly has one.
    if not any(section.kind == "abstract" for section in sections):
        abstract_match = re.search(r"(?im)^\s*abstract\b[:\s]*", full_text)
        if abstract_match:
            start = abstract_match.start()
            next_heading = min((s.char_start for s in sections if s.char_start > start), default=len(full_text))
            text = full_text[start:next_heading].strip("\n")
            if text:
                page_start = _page_for_offset(start, spans, len(pages))
                page_end = _page_for_offset(max(start, next_heading - 1), spans, len(pages))
                if not PAGE_MARKER_RE.search(text.splitlines()[0] if text.splitlines() else ""):
                    text = PAGE_MARKER_TEMPLATE.format(page=page_start).strip("\n") + "\n" + text
                sections.insert(
                    0,
                    Section(
                        id="",
                        kind="abstract",
                        heading="Abstract",
                        text=text,
                        page_start=page_start,
                        page_end=page_end,
                        char_start=start,
                        char_end=next_heading,
                        tokens_est=estimate_tokens(text),
                    ),
                )

    if not sections:
        sections = _fallback_sections(pages, full_text, spans)

    # Sort by character offset and assign stable ids.
    sections.sort(key=lambda s: s.char_start)
    for index, section in enumerate(sections, start=1):
        section.id = f"S{index:02d}"
    return sections


def _page_for_offset(offset: int, spans: dict[int, tuple[int, int]], page_count: int) -> int:
    if not spans:
        return 1
    for page in range(1, page_count + 1):
        start, end = spans.get(page, (0, 0))
        if offset <= end:
            return page
        # Offsets in the newline gap between two page spans belong to the
        # page that just ended.
        next_start = spans.get(page + 1, (None, None))[0]
        if next_start is not None and offset < next_start:
            return page
    return page_count


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def _pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    """Read PDF document metadata; title/author are often better than font heuristics."""

    try:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        try:
            info = dict(doc.metadata or {})
        finally:
            doc.close()
        return {key: str(value).strip() for key, value in info.items() if value}
    except Exception:
        return {}


_WATERMARK_RE = re.compile(
    r"^(?:for\s+review\s+only|review\s+copy|confidential|anonymous\s+(?:submission|review)|"
    r"manuscript\s+under\s+review|preprint|draft\s+version|do\s+not\s+distribute|"
    r"(?:ieee\s+)?[a-z0-9 &/.\-]{0,80}paper\s*#\d+|"
    r"page\s+\d+\s+of\s+\d+)\b",
    re.IGNORECASE,
)


def _is_watermark_line(text: str) -> bool:
    return bool(_WATERMARK_RE.match(normalize_whitespace(text)))


def _extract_title(pages: Sequence[PageText]) -> str:
    if not pages:
        return "Untitled"
    first = pages[0]
    # Largest font line on page 1, excluding obvious banners and review
    # watermarks. Watermark text is often rendered larger than the real title
    # on review copies ("FOR REVIEW ONLY" on every page).
    candidates = [
        c
        for c in first.headings
        if len(c.text.strip()) >= 6
        and not c.text.lower().startswith(("arxiv", "http", "doi"))
        and not _is_watermark_line(c.text)
    ]
    if candidates:
        best = max(candidates, key=lambda c: (c.size, -c.y))
        title = normalize_whitespace(best.text)
        if 5 <= len(title) <= 300:
            return title
    for line in first.text.splitlines():
        text = normalize_whitespace(line)
        if len(text) < 8:
            continue
        if _is_watermark_line(text):
            continue
        if text.lower().startswith(("arxiv", "http", "doi", "copyright", "proceedings")):
            continue
        return text[:300]
    return "Untitled"


def _extract_authors(pages: Sequence[PageText]) -> list[str]:
    if not pages:
        return []
    lines = [normalize_whitespace(line) for line in pages[0].text.splitlines() if line.strip()]
    if not lines:
        return []
    # Heuristic: lines between title and abstract, with comma/and separators or
    # short name-like content.
    start = 0
    for index, line in enumerate(lines):
        if re.search(r"(?i)abstract|摘要", line):
            start = index
            break
    window = lines[1:start] if start > 1 else lines[1:4]
    authors: list[str] = []
    for line in window[:6]:
        if len(line) > 200 or line.lower().startswith(("abstract", "keywords")):
            continue
        if re.search(r"@|university|institute|department|laboratory|inc\.|ltd\.", line, re.IGNORECASE):
            continue
        if re.search(r",|\band\b|、|；|;", line) and len(line) <= 160:
            parts = re.split(r",|、|;|\band\b", line)
            authors.extend(part.strip() for part in parts if part.strip())
    return unique_preserve(authors)[:20]


def _extract_year(pages: Sequence[PageText]) -> int | None:
    text = "\n".join(page.text for page in pages[:2])
    years = [int(year) for year in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    years = [year for year in years if 1900 <= year <= 2100]
    if not years:
        return None
    # Prefer the most frequent recent year; ties go to the larger year.
    counts: dict[int, int] = {}
    for year in years:
        counts[year] = counts.get(year, 0) + 1
    return max(counts, key=lambda year: (counts[year], year))


_FIGURE_LINE_RE = re.compile(
    r"(?im)^\s*(Figure|Fig\.?|Table|图|表)\s*([0-9]+[A-Za-z]?)\s*[.:：]\s*(.{0,200})"
)
_FIGURE_INLINE_RE = re.compile(
    r"(?i)(?<![A-Za-z])(Figure|Fig\.?|Table|图|表)\s*([0-9]+[A-Za-z]?)\s*[:：]\s*(.{0,200})"
)


def _table_like_rows(text: str) -> int:
    """Count lines that look like table rows (label followed by numbers)."""

    return len(
        re.findall(
            r"(?m)^\s*[A-Za-z\u4e00-\u9fff][^\n]{0,80}?\s+[-–]?\d+(?:\.\d+)?",
            text,
        )
    )


def _extract_figures_tables(pages: Sequence[PageText]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    for page in pages:
        seen_on_page: set[tuple[str, int]] = set()
        for pattern in (_FIGURE_LINE_RE, _FIGURE_INLINE_RE):
            for match in pattern.finditer(page.text):
                label = match.group(1).lower()
                number = match.group(2)
                item_id = f"{'Table' if label.startswith(('table', '表')) else 'Figure'} {number}"
                if (item_id, page.page) in seen_on_page:
                    continue
                seen_on_page.add((item_id, page.page))
                caption = normalize_whitespace(match.group(3))[:300]
                item = {"id": item_id, "page": page.page, "caption": caption}
                # Captions can appear above, below, or beside the table/figure, and
                # two-column PDFs often interleave them with prose. Inspect a window
                # on both sides; a one-sided window misclassified complete tables
                # (e.g. Table 1 in the CVAE paper) as caption-only.
                start = max(0, match.start() - 1800)
                end = min(len(page.text), match.end() + 1800)
                window = page.text[start:end]
                numeric_tokens = len(re.findall(r"\b\d+(?:\.\d+)?(?:%|x)?\b", window))
                row_like = _table_like_rows(window)
                item["content_type"] = "text_maybe" if numeric_tokens >= 6 and row_like >= 2 else "caption_only"
                if label.startswith(("table", "表")):
                    tables.append(item)
                else:
                    figures.append(item)
    # Deduplicate.
    def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, int]] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            key = (item["id"], item["page"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    return dedupe(figures), dedupe(tables)


def select_vision_pages(
    figures: Sequence[dict[str, Any]],
    tables: Sequence[dict[str, Any]],
    *,
    page_count: int,
    mode: str = "auto",
    max_pages: int = 20,
) -> list[int]:
    """Choose pages that the host's multimodal model should inspect.

    ``auto`` targets pages where deterministic text extraction is weakest:
    caption-only or unknown tables/figures. ``all`` renders every page up to
    ``max_pages``. ``off`` disables the supplement.
    """

    if mode == "off":
        return []
    if mode == "all":
        return list(range(1, min(page_count, max_pages) + 1))
    # ``auto`` renders every page that contains a table or figure. Tables are
    # higher priority than figures, and caption-only/unknown items are higher
    # priority than text_maybe ones, because the former benefit most from
    # visual reading. This is deliberately broader than the old
    # caption-only-only selection: text_maybe tables (interleaved two-column
    # rows) also need a visual cross-check.
    ranked: list[tuple[int, int]] = []
    for item in tables:
        page = item.get("page")
        if isinstance(page, int) and 1 <= page <= page_count:
            content_type = str(item.get("content_type") or "unknown")
            ranked.append((0 if content_type in {"caption_only", "unknown"} else 1, page))
    for item in figures:
        page = item.get("page")
        if isinstance(page, int) and 1 <= page <= page_count:
            content_type = str(item.get("content_type") or "unknown")
            ranked.append((2 if content_type in {"caption_only", "unknown"} else 3, page))
    pages: list[int] = []
    for _, page in sorted(ranked):
        if page not in pages:
            pages.append(page)
    return pages[:max_pages]


def render_page_images(
    pdf_path: Path,
    out_dir: Path,
    pages: Sequence[int],
    *,
    figures: Sequence[dict[str, Any]] = (),
    tables: Sequence[dict[str, Any]] = (),
    dpi: int = 150,
    max_pages: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Render selected pages to PNG for the host's vision-capable model."""

    if not pages:
        return [], []
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        return [], [f"Page images were requested but PyMuPDF is unavailable: {exc}"]

    reasons: dict[int, list[str]] = {}
    for item in list(figures) + list(tables):
        page = item.get("page")
        if isinstance(page, int):
            reasons.setdefault(page, []).append(
                f"{item.get('id', 'table/figure')} ({item.get('content_type', 'unknown')})"
            )

    warnings: list[str] = []
    if len(pages) > max_pages:
        warnings.append(f"Page-image rendering limited to {max_pages} of {len(pages)} selected pages.")
    image_dir = ensure_dir(out_dir / "paper" / "pages")
    rendered: list[dict[str, Any]] = []
    doc = fitz.open(str(pdf_path))
    try:
        for page_number in list(pages)[:max_pages]:
            if page_number < 1 or page_number > doc.page_count:
                continue
            try:
                pix = doc[page_number - 1].get_pixmap(dpi=dpi)
                path = image_dir / f"page-{page_number:04d}.png"
                pix.save(str(path))
            except Exception as exc:  # pragma: no cover - malformed PDF
                warnings.append(f"page {page_number}: page image rendering failed ({exc})")
                continue
            rendered.append(
                {
                    "page": page_number,
                    "path": f"paper/pages/page-{page_number:04d}.png",
                    "dpi": dpi,
                    "reason": "; ".join(reasons.get(page_number, [])) or "page image",
                }
            )
    finally:
        doc.close()
    return rendered, warnings


def _count_references(sections: Sequence[Section]) -> int:
    for section in sections:
        if section.kind != "references":
            continue
        lines = [line for line in section.text.splitlines() if line.strip()]
        count = sum(1 for line in lines if _REFERENCE_LINE_RE.match(line))
        if count:
            return count
        return max(0, len(lines) - 1)
    return 0


# ---------------------------------------------------------------------------
# Public ingestion
# ---------------------------------------------------------------------------


def _validate_pdf_path(pdf_path: Path) -> Path:
    path = pdf_path.expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise IngestError(f"Not a PDF file (extension {path.suffix!r}): {path}")
    size = path.stat().st_size
    if size == 0:
        raise IngestError(f"PDF is empty: {path}")
    if size > MAX_PDF_BYTES:
        raise IngestError(f"PDF is {size / 1024 / 1024:.1f} MB (limit {MAX_PDF_BYTES / 1024 / 1024:.0f} MB): {path}")
    return path


def ingest_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    paper_id: str | None = None,
    engine: str | None = None,
    ocr: bool = False,
    ocr_max_pages: int = 30,
    allow_poor: bool = False,
    blind: bool = False,
    strip_margins: bool = True,
    vision_pages: str = "auto",
    vision_max_pages: int = 20,
    vision_dpi: int = 150,
) -> IngestedPaper:
    path = _validate_pdf_path(pdf_path)
    sha = sha256_file(path)
    paper_id = paper_id or sha[:16]
    pages, used_engine, engine_version, warnings = extract_pages(path, engine=engine, ocr=ocr, ocr_max_pages=ocr_max_pages)
    if strip_margins:
        pages = strip_repeated_margins(pages)
    quality = assess_pages(pages)
    warnings = unique_preserve(warnings + list(quality.get("warnings", [])))
    if quality["quality"] == "poor" and not allow_poor:
        raise IngestError(
            "PDF extraction quality is poor; refusing to start a review that would look complete but be based on unreadable text. "
            "Use --allow-poor-extraction to override, or --ocr if tesseract is installed.",
            detail={"quality": quality, "warnings": warnings, "engine": used_engine},
        )
    if quality["quality"] == "partial":
        warnings.append("Extraction quality is partial; reviewers must explicitly mark truncated or unreadable content and lower confidence.")
    if blind:
        warnings.append("blind_paper only redacts metadata shown to reviewers; the extracted body text is unchanged.")

    sections = sectionize(pages)
    full_text, _ = _build_full_text(pages)
    title = _extract_title(pages)
    metadata = _pdf_metadata(path)
    meta_title = metadata.get("title", "")
    if meta_title and len(meta_title) > len(title) + 5:
        title_words = re.findall(r"[a-z0-9]+", title.casefold())
        meta_words = re.findall(r"[a-z0-9]+", meta_title.casefold())
        if not title_words or meta_words[: len(title_words)] == title_words:
            title = meta_title
            warnings.append("Title completed from PDF metadata (font heuristic found only a partial title).")
    authors = [] if blind else _extract_authors(pages)
    if not authors and metadata.get("author"):
        authors = [part.strip() for part in re.split(r"[;]", metadata["author"]) if part.strip()]
        authors = [] if blind else authors
    year = _extract_year(pages)
    figures, tables = _extract_figures_tables(pages)
    vision_pages_list = select_vision_pages(
        figures,
        tables,
        page_count=len(pages),
        mode=vision_pages,
        max_pages=vision_max_pages,
    )
    page_images, vision_warnings = render_page_images(
        path,
        out_dir,
        vision_pages_list,
        figures=figures,
        tables=tables,
        dpi=vision_dpi,
        max_pages=vision_max_pages,
    )
    warnings.extend(vision_warnings)

    paper_dir = ensure_dir(out_dir / "paper")
    sections_dir = ensure_dir(paper_dir / "sections")
    atomic_write_text(paper_dir / "text.txt", full_text)
    atomic_write_json(paper_dir / "pages.json", [{"page": page.page, "text": page.text} for page in pages])

    manifest_sections: list[dict[str, Any]] = []
    for section in sections:
        section_path = sections_dir / f"{section.id}.md"
        atomic_write_text(section_path, section.text)
        manifest_sections.append(section.to_manifest(f"paper/sections/{section.id}.md"))

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper_id,
        "source": {
            "path": str(path),
            "sha256": sha,
            "bytes": path.stat().st_size,
            "pages": len(pages),
        },
        "title": title,
        "authors": authors,
        "year": year,
        "language": detect_language(full_text),
        "extraction": {
            "engine": used_engine,
            "engine_version": engine_version or None,
            "quality": quality["quality"],
            "chars": quality["chars"],
            "chars_per_page": quality["chars_per_page"],
            "warnings": warnings,
            "ocr_used": used_engine == "ocr",
            "page_texts_path": "paper/pages.json",
            "sectionizer_version": SECTIONIZER_VERSION,
            "quality_details": quality,
        },
        "sections": manifest_sections,
        "figures": figures,
        "tables": tables,
        "vision": {
            "mode": vision_pages,
            "dpi": vision_dpi,
            "max_pages": vision_max_pages,
            "page_images": page_images,
        },
        "references_count": _count_references(sections),
        "blind": blind,
        "created_at": utc_now_iso(),
    }
    atomic_write_json(paper_dir / "manifest.json", manifest)
    return IngestedPaper(
        paper_id=paper_id,
        pdf_path=path,
        title=title,
        authors=authors,
        year=year,
        pages=pages,
        sections=sections,
        full_text=full_text,
        manifest=manifest,
        quality=quality,
    )


# ---------------------------------------------------------------------------
# Quote verification
# ---------------------------------------------------------------------------

_CURLY = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2032": "'",
    "\u2033": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
}


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for old, new in _CURLY.items():
        text = text.replace(old, new)
    text = PAGE_MARKER_RE.sub(" ", text)
    text = re.sub(r"-\s*\n\s*", "", text)  # line-break hyphenation
    text = re.sub(r"\s+", " ", text)
    # A quote often ends at a hyphenated column break (``robust-``); the page
    # text has the joined word (``robustness``). Drop the dangling hyphen.
    text = re.sub(r"(?<=[A-Za-z])-\s*$", "", text)
    return text.strip().casefold()


def _segments(quote: str) -> list[str]:
    parts = re.split(r"(?:\.\.\.|…|\. \. \.)", quote)
    return [part.strip() for part in parts if len(part.strip()) >= 6]


def _fuzzy_match(quote_norm: str, page_text: str) -> tuple[float, str]:
    import difflib

    words = quote_norm.split()
    if len(words) < 4:
        return 0.0, ""
    anchor = " ".join(words[: min(6, len(words))])
    idx = page_text.find(anchor)
    if idx < 0:
        anchor = " ".join(words[-min(6, len(words)) :])
        idx = page_text.find(anchor)
    if idx < 0:
        return 0.0, ""
    window_size = max(len(quote_norm) * 2, 400)
    start = max(0, idx - len(quote_norm) // 4)
    window = page_text[start : start + window_size]
    matcher = difflib.SequenceMatcher(None, quote_norm, window, autojunk=False)
    match = matcher.find_longest_match(0, len(quote_norm), 0, len(window))
    score = match.size / max(1, len(quote_norm))
    snippet = window[:200]
    return score, snippet


class QuoteIndex:
    """Normalized, page-aware lookup for reviewer quotes."""

    def __init__(self, pages: Sequence[PageText | dict[str, Any]], page_count: int | None = None) -> None:
        self._raw: dict[int, str] = {}
        for page in pages:
            if isinstance(page, PageText):
                number, text = page.page, page.text
            else:
                number, text = int(page["page"]), str(page.get("text", ""))
            self._raw[number] = text
        self.page_count = page_count or max(self._raw, default=0)
        self._norm = {number: normalize_for_match(text) for number, text in self._raw.items()}

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> "QuoteIndex":
        manifest = json.loads((run_dir / "paper" / "manifest.json").read_text(encoding="utf-8"))
        pages = json.loads((run_dir / "paper" / "pages.json").read_text(encoding="utf-8"))
        return cls(pages, page_count=int(manifest["source"]["pages"]))

    def page_text(self, page: int) -> str:
        return self._raw.get(page, "")

    def verify(self, evidence: dict[str, Any], *, strict: bool = True) -> QuoteMatch:
        kind = evidence.get("kind")
        quote = str(evidence.get("quote") or "").strip()
        page = evidence.get("page")
        page_end = evidence.get("page_end") or page
        if kind == "absence":
            if not quote:
                if not isinstance(page, int) or not (1 <= page <= self.page_count):
                    return QuoteMatch("bad_page", page=page if isinstance(page, int) else None, detail="absence evidence has an invalid page")
                if not isinstance(page_end, int) or not (page <= page_end <= self.page_count):
                    return QuoteMatch("bad_page", page=page, detail="absence page_end is invalid")
                return QuoteMatch("absence", page=page, detail="absence evidence; note must describe what was checked")
            kind = "paper"
        if kind == "external":
            if not str(evidence.get("note") or "").strip():
                return QuoteMatch("missing_note", detail="external evidence requires a note describing the source")
            return QuoteMatch("external", detail="external evidence is not quote-verified")
        if not quote:
            return QuoteMatch("empty_quote", detail="paper evidence requires a non-empty quote")
        if not isinstance(page, int) or not (1 <= page <= self.page_count):
            return QuoteMatch("bad_page", page=page if isinstance(page, int) else None, detail=f"page must be within 1..{self.page_count}")
        if not isinstance(page_end, int) or page_end < page or page_end > self.page_count:
            return QuoteMatch("bad_page", page=page, detail="page_end is invalid")

        quote_norm = normalize_for_match(quote)
        if len(quote_norm) < 8:
            return QuoteMatch("too_short", page=page, detail="quote is shorter than 8 normalized characters")

        for number in range(page, page_end + 1):
            page_norm = self._norm.get(number, "")
            # Exact normalized substring, including line-break dehyphenation.
            idx = page_norm.find(quote_norm)
            if idx >= 0:
                status = "exact" if quote in self._raw.get(number, "") else "normalized"
                return QuoteMatch(status, page=number, score=1.0, snippet=page_norm[idx : idx + 200])
            # Segmented quotes (ellipsis) are accepted only when every segment is present in order.
            segments = _segments(quote)
            if len(segments) >= 2:
                cursor = 0
                ok = True
                for segment in segments:
                    seg_norm = normalize_for_match(segment)
                    found = page_norm.find(seg_norm, cursor)
                    if found < 0:
                        ok = False
                        break
                    cursor = found + len(seg_norm)
                if ok:
                    return QuoteMatch("segmented", page=number, score=0.95, snippet=page_norm[:200])
            # Fuzzy match is a warning-level result; strict callers should not
            # silently accept it as exact evidence.
            score, snippet = _fuzzy_match(quote_norm, page_norm)
            if score >= 0.88:
                return QuoteMatch("fuzzy", page=number, score=score, snippet=snippet, detail="quote is a close but not exact match")

        return QuoteMatch("not_found", page=page, detail="quote was not found on the cited page range")

    def verify_all(self, evidences: Iterable[dict[str, Any]], *, strict: bool = True) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for evidence in evidences:
            result = self.verify(evidence, strict=strict)
            results.append({"evidence": evidence, "match": result.to_dict()})
        return results
