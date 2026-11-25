"""Bilingual review-material preparation and verification.

The deterministic renderer writes the primary-language ``review-rounds.md`` and
``final-opinion.md``.  This module mirrors the primary language and creates a
translation task packet for the other language.  Translation is performed by a
fresh host subagent (or an API provider), because the deterministic script must
not call a model in host mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import atomic_write_json, atomic_write_text, read_json, sha256_text

LANGS = ("zh", "en")
LANG_NAMES = {"zh": "Chinese", "en": "English"}
MATERIALS = (
    ("report", "report.md", "最终报告"),
    ("review-rounds", "review-rounds.md", "各轮审稿意见与作者回复"),
    ("final-opinion", "final-opinion.md", "最终审查意见"),
)


def _run_language(run_dir: Path) -> str:
    report = read_json(run_dir / "output" / "report.json") if (run_dir / "output" / "report.json").is_file() else {}
    if isinstance(report, dict):
        language = str(report.get("output_language") or "").strip()
        if language in LANGS:
            return language
    run = read_json(run_dir / "run.json") if (run_dir / "run.json").is_file() else {}
    if isinstance(run, dict):
        language = str(run.get("output_language") or run.get("language") or "").strip()
        if language in LANGS:
            return language
    return "en"


def _task_prompt(base: str, source: Path, target: Path, target_lang: str) -> str:
    target_name = LANG_NAMES[target_lang]
    return f"""# Bilingual translation task

Translate the Markdown document below into **{target_name}**.

- Source: `{source.as_posix()}`
- Target: `{target.as_posix()}`
- Material: `{base}`

## Requirements

1. Read the complete source Markdown. Do not summarize or shorten it.
2. Translate every section, heading, table, bullet, page reference, number, and
   disclaimer into {target_name}.
3. Keep technical terms, model names, dataset names, abbreviations, JSON field
   names, and quoted paper text in their original form where appropriate.
4. Preserve the original structure and numbering. Do not add or remove claims,
   and do not soften criticism.
5. Write the complete translated Markdown to the target path above. Do not call
   `hpe.py submit` or `hpe.py advance`.
6. Reply only with `written <target>` when finished.
"""


def prepare_bilingual_files(run_dir: str | Path, *, primary: str | None = None) -> dict[str, Any]:
    """Mirror the primary language and create translation tasks for the other.

    Expected files after the host completes the tasks:

    - ``output/report-zh.md`` / ``output/report-en.md``
    - ``output/review-rounds-zh.md`` / ``output/review-rounds-en.md``
    - ``output/final-opinion-zh.md`` / ``output/final-opinion-en.md``
    """

    run_dir = Path(run_dir).expanduser().resolve()
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    primary_lang = primary if primary in LANGS else _run_language(run_dir)
    other_lang = "en" if primary_lang == "zh" else "zh"
    tasks: list[dict[str, Any]] = []
    files: dict[str, str | None] = {}
    missing: list[str] = []

    for base, filename, label in MATERIALS:
        source = output_dir / filename
        if not source.is_file():
            missing.append(str(source))
            continue
        source_text = source.read_text(encoding="utf-8")
        for language in LANGS:
            target = output_dir / f"{base}-{language}.md"
            files[f"{base}_{language}"] = str(target.relative_to(run_dir))
            if language == primary_lang:
                if target != source:
                    atomic_write_text(target, source_text)
                continue
            if target.is_file() and target.stat().st_size > 0:
                continue
            prompt_path = run_dir / "prompts" / f"bilingual-{base}-{language}.prompt.md"
            prompt = _task_prompt(base, source.relative_to(run_dir), target.relative_to(run_dir), language)
            atomic_write_text(prompt_path, prompt)
            tasks.append(
                {
                    "material": base,
                    "label": label,
                    "target_language": language,
                    "source_path": str(source.relative_to(run_dir)),
                    "output_path": str(target.relative_to(run_dir)),
                    "prompt_path": str(prompt_path.relative_to(run_dir)),
                    "prompt_sha256": sha256_text(prompt),
                }
            )
    payload = {
        "primary_language": primary_lang,
        "other_language": other_lang,
        "tasks": tasks,
        "files": files,
        "missing_sources": missing,
        "ok": not missing,
    }
    atomic_write_json(output_dir / "bilingual-tasks.json", payload)
    return payload


def check_bilingual_files(run_dir: str | Path, *, primary: str | None = None) -> dict[str, Any]:
    """Check whether both Chinese and English materials exist."""

    run_dir = Path(run_dir).expanduser().resolve()
    output_dir = run_dir / "output"
    primary_lang = primary if primary in LANGS else _run_language(run_dir)
    expected: list[str] = []
    present: list[str] = []
    missing: list[str] = []
    for base, _, _ in MATERIALS:
        for language in LANGS:
            path = output_dir / f"{base}-{language}.md"
            expected.append(str(path.relative_to(run_dir)))
            if path.is_file() and path.stat().st_size > 0:
                present.append(str(path.relative_to(run_dir)))
            else:
                missing.append(str(path.relative_to(run_dir)))
    return {
        "primary_language": primary_lang,
        "expected": expected,
        "present": present,
        "missing": missing,
        "ok": not missing,
    }
