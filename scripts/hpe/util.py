"""Small, dependency-free helpers shared across the HPE v2 implementation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


# ---------------------------------------------------------------------------
# Time / hashing / filesystem
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            try:
                os.chmod(temp_path, mode)
            except OSError:
                pass
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    _atomic_write(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    _atomic_write(path, payload.encode("utf-8"), mode=mode)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    # A single append is atomic enough for the line sizes used here on POSIX;
    # concurrent writers are serialized by the run lock in the CLI.
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def is_within(root: Path, candidate: Path) -> bool:
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    return candidate_resolved == root_resolved or root_resolved in candidate_resolved.parents


def safe_join(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    if not is_within(root, candidate):
        raise ValueError(f"path escapes run directory: {candidate}")
    return candidate


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def strip_control_chars(text: str) -> str:
    return _CONTROL_RE.sub("", text)


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_CJK_RE.findall(text)) / max(1, len(text))


def detect_language(text: str) -> str:
    ratio = cjk_ratio(text)
    if ratio >= 0.10:
        return "zh"
    if ratio > 0.02:
        return "mixed"
    return "en"


def estimate_tokens(text: str) -> int:
    """Cheap provider-agnostic estimate.

    CJK characters are counted as one token each; the remaining characters as
    roughly four per token. This intentionally over-estimates prose so budget
    decisions fail safe.
    """

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return int(math.ceil(cjk + other / 4.0))


def truncate_to_tokens(text: str, max_tokens: int, *, suffix: str = "\n…[truncated]") -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # Binary search by characters is adequate for the estimate above.
    low, high = 0, len(text)
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return text[:best] + suffix


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def dedupe_dicts(items: Sequence[dict[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = tuple(str(item.get(k, "")) for k in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def word_count(text: str) -> int:
    """Mixed CJK/Latin word count used only for soft prompt-length warnings."""

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin


def slugify(text: str, *, max_length: int = 60) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:max_length] or "paper"


def safe_path_component(text: str, *, max_length: int = 80) -> str:
    """Make a filename component safe on POSIX and Windows."""

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(text))
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "paper"
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned[:max_length].rstrip(" .") or "paper"


def default_run_dir(pdf_path: str | Path, *, resume: bool = False) -> Path:
    """Default run directory: ``<pdf_dir>/<pdf_stem>.hpe``.

    All intermediate artifacts and the final report live inside this directory,
    next to the reviewed paper. If it already exists, a timestamp suffix is
    added unless ``resume`` is true and it contains a run.
    """

    pdf = Path(pdf_path).expanduser().resolve()
    stem = safe_path_component(pdf.stem)
    base = pdf.parent / f"{stem}.hpe"
    if resume and (base / "run.json").is_file():
        return base
    if not base.exists():
        return base
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = pdf.parent / f"{stem}.hpe-{stamp}"
    counter = 2
    while candidate.exists():
        candidate = pdf.parent / f"{stem}.hpe-{stamp}-{counter}"
        counter += 1
    return candidate


def redact_paths(text: str, replacements: Iterable[tuple[str, str]] = ()) -> str:
    for old, new in replacements:
        if old:
            text = text.replace(old, new)
    return text


def now_age_seconds(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    try:
        return (utc_now() - parse_iso8601(iso_timestamp)).total_seconds()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CLI formatting
# ---------------------------------------------------------------------------


def format_table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def iter_chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Prompt-injection heuristics (advisory only; never used to modify content)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?(the\s+)?(previous|above|prior)\s+instructions?",
    r"disregard\s+(all\s+)?(the\s+)?(previous|above|prior)\s+instructions?",
    r"system\s+prompt",
    r"you\s+are\s+now\s+(a|an|the)\b",
    r"give\s+(me\s+)?(a\s+)?(10|ten|full\s+marks|high\s+score)",
    r"output\s+(accept|10/10|10)",
    r"忽略.{0,12}(以上|之前|上述)?.{0,6}(指令|提示|要求)",
    r"给.{0,8}(高分|满分|10\s*分)",
    r"系统提示词?",
    r"你现在是",
)


def scan_injection(text: str) -> list[str]:
    """Return matched snippets for advisory injection warnings."""

    if not text:
        return []
    findings: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            snippet = text[max(0, match.start() - 40) : match.end() + 40].replace("\n", " ")
            findings.append(snippet[:240])
    return unique_preserve(findings)
