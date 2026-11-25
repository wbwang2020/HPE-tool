"""Prompt registry, context packing, anonymization and task-prompt rendering."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import (
    PROMPT_VERSION,
    PROMPTS_DIR,
    SCHEMA_VERSION,
    get_persona,
    get_profile,
    section_priority,
)
from .domains import get_domain
from .errors import ConfigError
from .util import (
    atomic_write_json,
    atomic_write_text,
    estimate_tokens,
    read_json,
    sha256_text,
    unique_preserve,
    utc_now_iso,
)

ROLE_FILES = {
    "common": "00-common.md",
    "digest": "10-digest.md",
    "reviewer": "20-reviewer.md",
    "reviewer_update": "21-reviewer-update.md",
    "author": "30-author-rebuttal.md",
    "meta": "40-meta-review.md",
    "legacy": "50-legacy-summary.md",
    "summary_paper": "60-summary-paper.md",
    "summary_proposal": "61-summary-proposal.md",
    "summary_design": "62-summary-design.md",
}

SUMMARY_ROLE_BY_MATERIAL = {
    "paper": "summary_paper",
    "proposal": "summary_proposal",
    "design": "summary_design",
}

SUMMARY_STRUCTURE_FILES = {
    "paper": "prompts/upstream/paper-summary-20-sections.md",
    "proposal": "prompts/upstream/research-proposal-12-sections.md",
    "design": "prompts/upstream/experiment-design-14-sections.md",
}

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


@dataclass
class PromptTemplate:
    role: str
    meta: dict[str, str]
    system: str
    user: str
    raw: str


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = None
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            end = index
            break
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        meta[key.strip()] = value
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def split_system_user(body: str) -> tuple[str, str]:
    system_match = re.search(r"(?m)^##\s+System\s*$", body)
    user_match = re.search(r"(?m)^##\s+User\s*$", body)
    if system_match and user_match and user_match.start() > system_match.start():
        prefix = body[: system_match.start()].strip()
        system_body = body[system_match.end() : user_match.start()]
        system = (prefix + "\n\n" + system_body).strip() if prefix else system_body.strip()
        user = body[user_match.end() :]
        return system, user.strip()
    if user_match and not system_match:
        return "", body[user_match.end() :].strip()
    if system_match and not user_match:
        return body[system_match.end() :].strip(), ""
    return body.strip(), ""


def load_template(role: str) -> PromptTemplate:
    filename = ROLE_FILES.get(role)
    if not filename:
        raise ConfigError(f"Unknown prompt role '{role}'")
    path = PROMPTS_DIR / filename
    if not path.is_file():
        raise ConfigError(f"Prompt file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    system, user = split_system_user(body)
    return PromptTemplate(role=role, meta=meta, system=system, user=user, raw=body)


def load_common_body() -> str:
    template = load_template("common")
    return template.raw.strip()


def render_text(text: str, values: dict[str, str], *, where: str = "prompt") -> str:
    """Render a template.

    Only placeholders present in ``values`` are required. Unknown double-brace
    identifiers are left untouched because paper text can legitimately contain
    LaTeX-like or mathematical ``{{R}}`` / ``{{77}}`` fragments. Template
    placeholder typos are still caught by checking the template *before*
    inserting any paper content.
    """

    template_keys = set(PLACEHOLDER_RE.findall(text))
    missing = sorted(template_keys - set(values))
    if missing:
        raise ConfigError(f"Unresolved placeholders in {where}: {', '.join(missing)}")
    return PLACEHOLDER_RE.sub(lambda match: str(values[match.group(1)]), text)


def persona_card(persona_id: str) -> str:
    persona = get_persona(persona_id)
    lines = [str(persona.get("prompt_card", "")).strip()]
    focus = persona.get("focus") or []
    questions = persona.get("signature_questions") or []
    blind = persona.get("blind_spots") or []
    if focus:
        lines.append("\nFocus order:")
        lines.extend(f"- {item}" for item in focus)
    if questions:
        lines.append("\nQuestions you should ask:")
        lines.extend(f"- {item}" for item in questions)
    if blind:
        lines.append("\nBlind spots to guard against:")
        lines.extend(f"- {item}" for item in blind)
    return "\n".join(line for line in lines if line)


def material_label(material: str) -> str:
    return {
        "paper": "academic paper",
        "proposal": "research proposal / plan",
        "design": "project or experiment design",
    }.get(material, material)


def purpose_card(purpose: str) -> str:
    if purpose == "problem_finding":
        return (
            "This run is a **problem-finding** exercise, not an editorial decision. Your primary job is to find "
            "logic, technical and innovation loopholes. Still identify genuine innovation/technical highlights, "
            "but do not let a highlight excuse a serious gap. Every issue needs page/quote evidence, and you must "
            "state what evidence would change your mind."
        )
    if purpose == "summary":
        return "This run is a **detailed summary** task. Do not score or recommend; preserve the requested section structure."
    return (
        "This run is an **evidence-anchored review**. Find real innovation/technical highlights first, then focus "
        "on logic, technical and innovation loopholes that would change the main claim."
    )


def upstream_checklist() -> str:
    path = PROMPTS_DIR / "upstream" / "debate-checklist.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def summary_structure(material: str) -> str:
    rel = SUMMARY_STRUCTURE_FILES.get(material)
    if not rel:
        raise ConfigError(f"Unknown material type {material!r}")
    path = PROMPTS_DIR.parent / rel
    if not path.is_file():
        raise ConfigError(f"Summary structure file missing: {path}")
    return path.read_text(encoding="utf-8").strip()


def summary_prompt_role(material: str) -> str:
    if material not in SUMMARY_ROLE_BY_MATERIAL:
        raise ConfigError(f"Unknown material type {material!r}")
    return SUMMARY_ROLE_BY_MATERIAL[material]


# ---------------------------------------------------------------------------
# Paper metadata / sections
# ---------------------------------------------------------------------------


def _language_name(language: str) -> str:
    if language == "zh":
        return "Chinese"
    if language == "en":
        return "English"
    return "English"


def resolve_output_language(run: dict[str, Any], manifest: dict[str, Any]) -> str:
    resolved = str(run.get("output_language") or "").strip()
    if resolved in {"zh", "en"}:
        return resolved
    configured = str(run.get("language", "auto"))
    if configured in {"zh", "en"}:
        return configured
    detected = str(manifest.get("language", "en"))
    return "zh" if detected.startswith("zh") else "en"


def paper_meta(manifest: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    extraction = manifest.get("extraction", {})
    meta: dict[str, Any] = {
        "paper_id": manifest.get("paper_id"),
        "title": manifest.get("title", "Untitled"),
        "authors": [] if manifest.get("blind") else manifest.get("authors", []),
        "year": manifest.get("year"),
        "pages": manifest.get("source", {}).get("pages"),
        "language": manifest.get("language"),
        "blind": bool(manifest.get("blind")),
        "profile": run.get("profile"),
        "extraction": {
            "engine": extraction.get("engine"),
            "quality": extraction.get("quality"),
            "chars_per_page": extraction.get("chars_per_page"),
            "warnings": extraction.get("warnings", []),
        },
        "figures": [
            {"id": item.get("id"), "page": item.get("page"), "caption": item.get("caption"), "content_type": item.get("content_type")}
            for item in manifest.get("figures", [])[:40]
        ],
        "tables": [
            {"id": item.get("id"), "page": item.get("page"), "caption": item.get("caption"), "content_type": item.get("content_type")}
            for item in manifest.get("tables", [])[:40]
        ],
        "references_count": manifest.get("references_count", 0),
    }
    return meta


def _section_label(section: dict[str, Any]) -> str:
    heading = str(section.get("heading") or section.get("kind") or section.get("id"))
    return heading


def _section_block(section: dict[str, Any], text: str) -> str:
    heading = _section_label(section)
    pages = f"pages {section.get('page_start')}-{section.get('page_end')}"
    return f"### [{section.get('id')}] {heading} ({pages})\n{text.strip()}"


@dataclass
class SelectedSections:
    ids: list[str]
    text: str
    omitted: list[str]
    truncated: bool
    tokens: int
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)


def select_sections(
    manifest: dict[str, Any],
    run_dir: Path,
    *,
    persona: str,
    max_tokens: int,
    include_references: bool = False,
    preferred_ids: Iterable[str] = (),
) -> SelectedSections:
    sections = list(manifest.get("sections", []))
    if not sections:
        raise ConfigError("paper_manifest has no sections; cannot build a reviewer prompt")
    priorities = section_priority(persona)
    rank = {kind: index for index, kind in enumerate(priorities)}
    preferred = set(preferred_ids)

    # Load text and select in priority order; preferred sections get a boost.
    loaded: list[tuple[int, int, dict[str, Any], str]] = []
    for index, section in enumerate(sections):
        path = run_dir / str(section.get("text_path", ""))
        if not path.is_file():
            text = ""
        else:
            text = path.read_text(encoding="utf-8")
        kind = str(section.get("kind", "other"))
        if kind == "references" and not include_references:
            score = len(priorities) + 20
        else:
            score = rank.get(kind, len(priorities))
        if section.get("id") in preferred:
            score -= 3
        loaded.append((score, index, section, text))

    allowed = [
        item for item in loaded
        if include_references or item[2].get("kind") != "references"
    ]
    omitted_for_filter = [
        item[2] for item in loaded
        if not include_references and item[2].get("kind") == "references"
    ]
    total_tokens = sum(estimate_tokens(text) for _, _, _, text in allowed)
    if total_tokens <= max_tokens:
        chosen = sorted(allowed, key=lambda item: item[1])
        omitted: list[dict[str, Any]] = list(omitted_for_filter)
        truncated = False
    else:
        chosen = []
        used = 0
        omitted = []
        for score, index, section, text in sorted(loaded, key=lambda item: (item[0], item[1])):
            tokens = estimate_tokens(text)
            if section.get("kind") == "references" and not include_references:
                if section not in omitted:
                    omitted.append(section)
                continue
            if tokens == 0:
                continue
            if used + tokens <= max_tokens:
                chosen.append((score, index, section, text))
                used += tokens
            else:
                omitted.append(section)
        chosen.sort(key=lambda item: item[1])
        truncated = any(section.get("kind") != "references" for section in omitted)

    # Trim references to the requested number of entries if they made the cut.
    rendered: list[str] = []
    ids: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for _, _, section, text in chosen:
        if section.get("kind") == "references":
            lines = [line for line in text.splitlines() if line.strip()]
            keep = "\n".join(lines[:21])
            if len(lines) > 21:
                keep += "\n…[references truncated]"
            text = keep
        ids.append(str(section.get("id")))
        by_id[str(section.get("id"))] = section
        rendered.append(_section_block(section, text))
    return SelectedSections(
        ids=ids,
        text="\n\n".join(rendered),
        omitted=[str(section.get("id")) for section in omitted],
        truncated=truncated,
        tokens=sum(estimate_tokens(text) for text in rendered),
        by_id=by_id,
    )


# ---------------------------------------------------------------------------
# Digest / context compaction
# ---------------------------------------------------------------------------


def compact_digest(digest: dict[str, Any] | None, *, max_tokens: int = 1800) -> str:
    if not digest:
        return json.dumps({"available": False, "reason": "digest not available; read the paper sections directly"}, ensure_ascii=False)
    compact: dict[str, Any] = {
        "schema_version": digest.get("schema_version", SCHEMA_VERSION),
        "paper_id": digest.get("paper_id"),
        "problem": {
            "statement": (digest.get("problem") or {}).get("statement"),
            "motivation": (digest.get("problem") or {}).get("motivation"),
        },
        "claims": [
            {"id": item.get("id"), "statement": item.get("statement"), "type": item.get("type")}
            for item in digest.get("claims", [])[:20]
        ],
        "method": {
            "overview": (digest.get("method") or {}).get("overview"),
            "assumptions": [item.get("statement") for item in (digest.get("method") or {}).get("assumptions", [])[:10]],
            "components": [item.get("name") for item in (digest.get("method") or {}).get("components", [])[:10]],
        },
        "data": {
            "sources": (digest.get("data") or {}).get("sources", []),
            "sample_size": (digest.get("data") or {}).get("sample_size"),
            "availability": (digest.get("data") or {}).get("availability"),
        },
        "experiments": {
            "baselines": (digest.get("experiments") or {}).get("baselines", []),
            "ablations": (digest.get("experiments") or {}).get("ablations", []),
            "metrics": (digest.get("experiments") or {}).get("metrics", []),
            "compute": (digest.get("experiments") or {}).get("compute"),
        },
        "results": {
            "findings": (digest.get("results") or {}).get("findings", []),
            "numbers": (digest.get("results") or {}).get("numbers", [])[:20],
        },
        "limitations": (digest.get("limitations") or {}).get("authors_stated", []),
        "unreported": digest.get("unreported", []),
        "not_extractable": digest.get("not_extractable", []),
        "extraction_confidence": digest.get("extraction_confidence"),
    }
    payload = json.dumps(compact, ensure_ascii=False, indent=2)
    if estimate_tokens(payload) <= max_tokens:
        return payload
    # Progressive reduction while staying valid JSON.
    for key in ("results", "experiments", "method", "claims", "data"):
        compact.pop(key, None)
        payload = json.dumps(compact, ensure_ascii=False, indent=2)
        if estimate_tokens(payload) <= max_tokens:
            return payload
    compact["note"] = "digest omitted most details because of the context budget; read the paper sections directly"
    payload = json.dumps(compact, ensure_ascii=False, indent=2)
    if estimate_tokens(payload) <= max_tokens:
        return payload
    return json.dumps(
        {
            "available": True,
            "paper_id": digest.get("paper_id"),
            "note": "digest omitted due to context budget; read the paper sections directly",
            "unreported": digest.get("unreported", []),
            "not_extractable": digest.get("not_extractable", []),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Context budgeting
# ---------------------------------------------------------------------------

_EXPENSIVE_KEYS = {
    "model",
    "prompt_version",
    "created_at",
    "summary",
    "strengths",
    "quality_details",
    "warnings",
    "sections_provided",
    "sections_rechecked",
}
_SHORTEN_KEYS = {
    "rationale",
    "response",
    "claim",
    "why_it_matters",
    "requested_action",
    "resolution_note",
    "editor_rationale",
    "action",
    "note",
}


def _prune_for_context(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _EXPENSIVE_KEYS:
                continue
            if key in {"evidence", "paper_evidence"} and isinstance(item, list):
                out[key] = [
                    {sub_key: sub_value for sub_key, sub_value in evidence.items() if sub_key in {"kind", "page", "page_end", "section", "note"}}
                    for evidence in item[:3]
                    if isinstance(evidence, dict)
                ]
            elif key in _SHORTEN_KEYS and isinstance(item, str):
                out[key] = item[:500]
            else:
                out[key] = _prune_for_context(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_prune_for_context(item, depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return value[:1200]
    return value


def bounded_json(value: Any, max_tokens: int, *, note: str = "prior context compacted to fit the context budget") -> str:
    """Serialize context, compacting only if the budget is exceeded."""

    text = json.dumps(value, ensure_ascii=False, indent=2)
    if estimate_tokens(text) <= max_tokens:
        return text
    pruned = _prune_for_context(value)
    text = json.dumps(pruned, ensure_ascii=False, indent=2)
    if estimate_tokens(text) <= max_tokens:
        return text
    ids: list[str] = []
    if isinstance(value, dict):
        for key in ("issues", "new_issues", "responses", "issue_resolution", "tasks"):
            for item in value.get(key, []) or []:
                if isinstance(item, dict):
                    identifier = item.get("id") or item.get("issue_id") or item.get("response_id") or item.get("task_id")
                    if identifier:
                        ids.append(str(identifier))
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                identifier = item.get("id") or item.get("issue_id") or item.get("response_id") or item.get("task_id")
                if identifier:
                    ids.append(str(identifier))
    return json.dumps({"truncated": True, "note": note, "ids": unique_preserve(ids)[:80]}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Review loading, anonymization and ledger views
# ---------------------------------------------------------------------------


def review_path(run_dir: Path, reviewer_id: str, round_number: int) -> Path:
    return run_dir / "artifacts" / "reviews" / f"{reviewer_id}.round{round_number}.review.json"


def rebuttal_path(run_dir: Path, author_id: str, round_number: int) -> Path:
    return run_dir / "artifacts" / "rebuttals" / f"{author_id}.round{round_number}.rebuttal.json"


def ledger_path(run_dir: Path, *, final: bool = False) -> Path:
    return run_dir / "artifacts" / ("ledger.final.json" if final else "ledger.json")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def reviewer_label_map(run: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for index, reviewer in enumerate(run.get("reviewers", []), start=1):
        rid = str(reviewer.get("id", f"R{index}"))
        labels[rid] = str(reviewer.get("label") or f"Reviewer {chr(ord('A') + index - 1)}")
    return labels


def load_reviews(run_dir: Path, run: dict[str, Any], round_number: int) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for reviewer in run.get("reviewers", []):
        path = review_path(run_dir, str(reviewer.get("id")), round_number)
        data = load_json(path)
        if data:
            data.setdefault("reviewer_id", reviewer.get("id"))
            reviews.append(data)
    return reviews


def _issue_view(issue: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "title",
        "category",
        "severity",
        "claim_type",
        "claim",
        "why_it_matters",
        "requested_action",
        "evidence",
        "related_issue_ids",
        "confidence",
        "status",
    )
    return {key: copy.deepcopy(issue.get(key)) for key in keys if key in issue}


def anonymize_review(review: dict[str, Any], label: str, *, mode: str = "cross") -> dict[str, Any]:
    """Return a review view with reviewer identity removed.

    ``mode="cross"`` hides scores and recommendations to reduce anchoring.
    ``mode="author"`` hides scores/recommendations and persona.
    ``mode="meta"`` keeps scores because the editor needs the full picture.
    """

    base: dict[str, Any] = {
        "reviewer_label": label,
        "round": review.get("round"),
    }
    if mode == "meta":
        base.update(
            {
                "reviewer_id": review.get("reviewer_id"),
                "persona": review.get("persona"),
                "recommendation": review.get("recommendation"),
                "score": copy.deepcopy(review.get("score")),
                "confidence": review.get("confidence"),
                "summary": review.get("summary"),
            }
        )
    elif mode == "cross":
        base["issues"] = [_issue_view(issue) for issue in review.get("issues", [])]
        base["new_issues"] = [_issue_view(issue) for issue in review.get("new_issues", [])]
        base["issue_updates"] = [
            {
                "issue_id": update.get("issue_id"),
                "status": update.get("status"),
                "rationale": update.get("rationale"),
            }
            for update in review.get("issue_updates", [])
        ]
    else:  # author
        base["issues"] = [_issue_view(issue) for issue in review.get("issues", [])]
        base["new_issues"] = [_issue_view(issue) for issue in review.get("new_issues", [])]
        base["issue_updates"] = [
            {
                "issue_id": update.get("issue_id"),
                "status": update.get("status"),
                "rationale": update.get("rationale"),
            }
            for update in review.get("issue_updates", [])
        ]
    return base


def anonymized_reviews(reviews: Sequence[dict[str, Any]], labels: dict[str, str], *, mode: str = "cross") -> list[dict[str, Any]]:
    return [anonymize_review(review, labels.get(str(review.get("reviewer_id")), str(review.get("reviewer_id"))), mode=mode) for review in reviews]


def ledger_view(ledger: dict[str, Any], *, include_evidence: bool = False, label_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    label_map = label_map or {}
    out: list[dict[str, Any]] = []
    for issue in ledger.get("issues", []):
        item: dict[str, Any] = {
            "id": issue.get("id"),
            "aliases": issue.get("aliases", []),
            "title": issue.get("title"),
            "category": issue.get("category"),
            "severity": issue.get("severity"),
            "status": issue.get("status"),
            "raised_by": [label_map.get(rid, rid) for rid in issue.get("raised_by", [])],
            "round_opened": issue.get("round_opened"),
            "round_closed": issue.get("round_closed"),
            "confidence": issue.get("confidence"),
        }
        if include_evidence:
            item["evidence"] = issue.get("evidence", [])
        out.append(item)
    return out


def open_issues_for_author(ledger: dict[str, Any], reviews: Sequence[dict[str, Any]], label_map: dict[str, str]) -> list[dict[str, Any]]:
    """Join ledger issues with the original issue text the author must answer."""

    details: dict[str, dict[str, Any]] = {}
    for review in reviews:
        for issue in list(review.get("issues", [])) + list(review.get("new_issues", [])):
            key = str(issue.get("id"))
            details[key] = _issue_view(issue)
    out: list[dict[str, Any]] = []
    for issue in ledger.get("issues", []):
        if issue.get("status") in {"resolved", "withdrawn", "out_of_scope"}:
            continue
        detail = None
        for alias in issue.get("aliases", []):
            if alias in details:
                detail = details[alias]
                break
        item: dict[str, Any] = {
            "id": issue.get("id"),
            "aliases": issue.get("aliases", []),
            "title": issue.get("title"),
            "category": issue.get("category"),
            "severity": issue.get("severity"),
            "status": issue.get("status"),
            "raised_by": [label_map.get(rid, rid) for rid in issue.get("raised_by", [])],
            "evidence": issue.get("evidence", []),
        }
        if detail:
            item.update(
                {
                    "claim": detail.get("claim"),
                    "why_it_matters": detail.get("why_it_matters"),
                    "requested_action": detail.get("requested_action"),
                    "claim_type": detail.get("claim_type"),
                    "confidence": detail.get("confidence"),
                }
            )
        out.append(item)
    return out


def compact_rebuttal(rebuttal: dict[str, Any] | None, *, max_response_chars: int = 260) -> dict[str, Any]:
    """Compact author responses for cross-review/meta context.

    The full rebuttal can exceed the prior-context budget; reducing it to only
    issue IDs destroys the reviewer's ability to check the response. Keep the
    status, a bounded response, the revision plan and evidence locations.
    """

    if not rebuttal:
        return {"available": False, "reason": "no author rebuttal was provided for this round"}
    responses: list[dict[str, Any]] = []
    for response in rebuttal.get("responses", []) or []:
        if not isinstance(response, dict):
            continue
        evidence = []
        for item in response.get("paper_evidence", []) or []:
            if isinstance(item, dict):
                evidence.append(
                    {
                        "kind": item.get("kind"),
                        "page": item.get("page"),
                        "page_end": item.get("page_end"),
                        "section": item.get("section"),
                        "note": (str(item.get("note") or ""))[:100],
                    }
                )
        responses.append(
            {
                "response_id": response.get("response_id"),
                "issue_id": response.get("issue_id"),
                "status": response.get("status"),
                "response": str(response.get("response") or "")[:max_response_chars],
                "revision_plan": str(response.get("revision_plan") or "")[:110],
                "paper_evidence": evidence,
                "confidence": response.get("confidence"),
            }
        )
    return {
        "schema_version": rebuttal.get("schema_version"),
        "paper_id": rebuttal.get("paper_id"),
        "author_id": rebuttal.get("author_id"),
        "round": rebuttal.get("round"),
        "responses": responses,
        "unresolved": rebuttal.get("unresolved", []),
    }


def evidence_report(run_dir: Path) -> dict[str, Any]:
    """Aggregate per-task evidence verification results written by submit."""

    directory = run_dir / "artifacts" / "evidence"
    rows: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            data = load_json(path)
            if not data:
                continue
            rows.append(data)
    summary = {"tasks": len(rows), "exact": 0, "normalized": 0, "segmented": 0, "fuzzy": 0, "not_found": 0, "absence": 0, "external": 0, "bad_page": 0}
    for row in rows:
        for result in row.get("results", []):
            status = str((result.get("match") or {}).get("status", "unknown"))
            summary[status] = summary.get(status, 0) + 1
    return {"summary": summary, "tasks": rows}


def compact_evidence_report(run_dir: Path, *, max_problems: int = 40) -> dict[str, Any]:
    """A meta-review-friendly evidence report: per-task counts + only problems.

    The full per-evidence report is intentionally not injected into the meta
    prompt; it is large and mostly ``exact``. The editor needs the aggregate and
    the exceptions (fuzzy/not_found/bad_page), which are much smaller.
    """

    full = evidence_report(run_dir)
    summary = dict(full.get("summary", {}))
    problems: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for row in full.get("tasks", []):
        task_summary: dict[str, int] = {}
        for result in row.get("results", []):
            status = str((result.get("match") or {}).get("status", "unknown"))
            task_summary[status] = task_summary.get(status, 0) + 1
            if status not in {"exact", "normalized", "segmented", "absence", "external"}:
                match = result.get("match") or {}
                problems.append(
                    {
                        "task_id": row.get("task_id"),
                        "actor": row.get("actor"),
                        "where": result.get("where"),
                        "status": status,
                        "page": match.get("page"),
                        "score": match.get("score"),
                    }
                )
        tasks.append({"task_id": row.get("task_id"), "actor": row.get("actor"), "counts": task_summary})
    return {
        "summary": summary,
        "tasks": tasks,
        "problems": problems[:max_problems],
        "problems_truncated": max(0, len(problems) - max_problems),
    }


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


@dataclass
class RenderedPrompt:
    system: str
    user: str
    combined: str
    sha256: str
    context: dict[str, Any]
    values: dict[str, str] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)


def _load_digest(run_dir: Path) -> dict[str, Any] | None:
    return load_json(run_dir / "digest" / "digest.json")


def _load_ledger(run_dir: Path) -> dict[str, Any] | None:
    return load_json(ledger_path(run_dir))


def _human_round(round_number: int) -> str:
    return "round 0 (independent)" if round_number == 0 else f"round {round_number} (cross-review)"


def _page_images_text(manifest: dict[str, Any]) -> str:
    images = ((manifest.get("vision") or {}).get("page_images") or [])
    if not images:
        return "None. Use extracted text only; mark table/figure cells as not extractable."
    lines = [
        "The host rendered these page images. Read them with the host's multimodal "
        "file/image reader if available; they are supplementary to the extracted text.",
        "",
    ]
    for item in images:
        lines.append(f"- page {item.get('page')}: `{item.get('path')}` ({item.get('reason', 'page image')})")
    lines.append("")
    lines.append(
        "Rules: page numbers are physical PDF pages. An image can support an inference, but a "
        "`paper` evidence quote must still be a contiguous substring of extracted text. Describe "
        "image-only values in the evidence `note`; do not invent a quote or a number the image does not show. "
        "If the host cannot read images, ignore them, set coverage.images_read/[] (or summary images_read/[]), "
        "mark table/figure cells as not extractable, and lower confidence; do not invent values."
    )
    return "\n".join(lines)


def _not_extractable_lines(manifest: dict[str, Any], digest: dict[str, Any] | None) -> str:
    lines: list[str] = []
    for item in (digest or {}).get("not_extractable", []):
        lines.append(f"- {item}")
    for kind in ("figures", "tables"):
        for item in manifest.get(kind, []):
            if item.get("content_type") == "caption_only":
                lines.append(f"- {item.get('id')} on page {item.get('page')}: caption only; cell values are not in the extracted text")
    return "\n".join(lines) if lines else "- None reported."


def _context_note(selected: SelectedSections, *, extraction_quality: str, isolation: str, extra: str = "") -> str:
    parts: list[str] = []
    if extraction_quality != "good":
        parts.append(f"PDF extraction quality is **{extraction_quality}**. Lower confidence for claims that depend on tables, figures or noisy text.")
    if selected.truncated:
        parts.append(f"Some sections were omitted for the context budget: {', '.join(selected.omitted) or 'unknown'}. Set `coverage.truncated=true` and lower confidence where the missing section matters.")
    if isolation == "shared":
        parts.append("WARNING: this task is running in a shared host context, so the reviewer's independence may be compromised. The final report will record this; keep confidence honest.")
    if extra:
        parts.append(extra)
    return " ".join(parts) if parts else "No context warning."


def _prior_round_reviews(run_dir: Path, run: dict[str, Any], round_number: int) -> list[dict[str, Any]]:
    return load_reviews(run_dir, run, round_number)


def load_reviews_by_round(run_dir: Path, run: dict[str, Any], max_round: int) -> list[dict[str, Any]]:
    """Every reviewer report for every completed round, in round order."""

    rounds: list[dict[str, Any]] = []
    for round_number in range(0, max_round + 1):
        reviews = load_reviews(run_dir, run, round_number)
        if reviews:
            rounds.append({"round": round_number, "reviews": reviews})
    return rounds


def load_rebuttals_by_round(run_dir: Path, run: dict[str, Any], max_round: int) -> list[dict[str, Any]]:
    """Every author rebuttal for every round, in round order."""

    author_id = str((run.get("author") or {}).get("id", "A1"))
    rounds: list[dict[str, Any]] = []
    for round_number in range(1, max_round + 1):
        data = load_json(rebuttal_path(run_dir, author_id, round_number))
        if data:
            rounds.append({"round": round_number, "rebuttal": data})
    return rounds


def load_final_reviews(run_dir: Path, run: dict[str, Any], max_round: int) -> list[dict[str, Any]]:
    """Latest available review per reviewer (used when a later round failed)."""

    out: list[dict[str, Any]] = []
    for reviewer in run.get("reviewers", []):
        rid = str(reviewer.get("id"))
        chosen: dict[str, Any] | None = None
        for round_number in range(max_round, -1, -1):
            data = load_json(review_path(run_dir, rid, round_number))
            if data:
                chosen = data
                break
        if chosen:
            chosen.setdefault("reviewer_id", rid)
            out.append(chosen)
    return out


def _load_rebuttal(run_dir: Path, run: dict[str, Any], round_number: int) -> dict[str, Any] | None:
    author_id = str((run.get("author") or {}).get("id", "A1"))
    return load_json(rebuttal_path(run_dir, author_id, round_number))


def _review_issue_ids(review: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for issue in list(review.get("issues", [])) + list(review.get("new_issues", [])):
        if issue.get("id"):
            ids.append(str(issue["id"]))
    return unique_preserve(ids)


def _prior_open_issue_ids(prior: dict[str, Any]) -> list[str]:
    resolved: set[str] = set()
    for update in prior.get("issue_updates", []):
        if update.get("status") in {"resolved", "withdrawn", "out_of_scope"}:
            resolved.add(str(update.get("issue_id")))
    return [issue_id for issue_id in _review_issue_ids(prior) if issue_id not in resolved]


def _section_ids_for_issues(reviews: Sequence[dict[str, Any]], ledger: dict[str, Any] | None) -> list[str]:
    # Section IDs are not stored on issues in v2; prefer core sections. This
    # hook exists so a future retrieval index can add exact sections.
    return []


def build_prompt_for_task(
    run_dir: Path,
    state: Any,
    task: Any,
    run: dict[str, Any],
    *,
    max_context_tokens: int | None = None,
) -> RenderedPrompt:
    manifest = load_json(run_dir / "paper" / "manifest.json")
    if not manifest:
        raise ConfigError("paper/manifest.json is missing; run ingest first")
    context_limits = run.get("context") or {}
    max_paper_tokens = int(max_context_tokens or context_limits.get("max_paper_tokens", 60000))
    max_prior_tokens = int(context_limits.get("max_prior_tokens", 12000))
    max_digest_tokens = int(context_limits.get("max_digest_tokens", 1800))
    allow_truncation = bool(context_limits.get("allow_truncation", False))
    include_references = bool(context_limits.get("include_references", False))
    language = resolve_output_language(run, manifest)
    profile_name = str(run.get("profile", "conference"))
    profile = get_profile(profile_name)
    weights = profile.get("weights", {})
    labels = reviewer_label_map(run)
    author = run.get("author") or {}
    author_mode = str(author.get("mode", "simulated"))
    author_id = str(author.get("id", "A1"))
    prompt_version = str(run.get("prompt_set", PROMPT_VERSION))
    purpose = str(run.get("purpose", "review"))
    material = str(run.get("material", "paper"))
    domain_id = str(run.get("domain", "ai-ict"))
    domain = get_domain(domain_id)
    common_body = load_common_body()
    values: dict[str, str] = {
        "OUTPUT_LANGUAGE": _language_name(language),
        "PAPER_ID": str(manifest.get("paper_id")),
        "PAPER_TITLE": str(manifest.get("title", "Untitled")),
        "PAPER_PAGES": str(manifest.get("source", {}).get("pages", 0)),
        "PAPER_TEXT": "",
        "PROMPT_VERSION": prompt_version,
        "PROFILE": profile_name,
        "PROFILE_WEIGHTS_JSON": json.dumps(weights, ensure_ascii=False, sort_keys=True),
        "ROUND": str(task.round),
        "REVIEWER_ID": "",
        "REVIEWER_LABEL": "",
        "PERSONA_ID": "",
        "PERSONA_NAME": "",
        "PERSONA_CARD": "",
        "AUTHOR_ID": author_id,
        "AUTHOR_MODE": author_mode,
        "EDITOR_ID": "E1",
        "PAPER_META_JSON": json.dumps(paper_meta(manifest, run), ensure_ascii=False, indent=2),
        "DIGEST_JSON": compact_digest(_load_digest(run_dir), max_tokens=max_digest_tokens),
        "PAPER_SECTIONS": "",
        "NOT_EXTRACTABLE": _not_extractable_lines(manifest, _load_digest(run_dir)),
        "CONTEXT_NOTE": "",
        "OWN_PRIOR_REVIEW_JSON": "null",
        "OTHER_REVIEWS_ANONYMIZED": "[]",
        "REBUTTAL_JSON": "null",
        "LEDGER_SUMMARY": "{}",
        "LEDGER_OPEN_ISSUES": "[]",
        "LEDGER_JSON": "{}",
        "RELEVANT_SECTIONS": "",
        "EVIDENCE_VERIFICATION": "{}",
        "ANONYMIZED_REVIEWS": "[]",
        "FINAL_REVIEWS_JSON": "[]",
        "REBUTTALS_JSON": "[]",
        "POLICY_DECISION": "accept",
        "POLICY_COUNTS_JSON": "{}",
        "SCORE_SUMMARY_JSON": "{}",
        "COVERAGE_JSON": "{}",
        "EVIDENCE_VERIFICATION_JSON": "{}",
        "PURPOSE": purpose,
        "PURPOSE_CARD": purpose_card(purpose),
        "MATERIAL": material,
        "MATERIAL_LABEL": material_label(material),
        "DOMAIN_ID": domain_id,
        "DOMAIN_LABEL": domain.label,
        "DOMAIN_CARD": domain.card(),
        "UPSTREAM_CHECKLIST": upstream_checklist(),
        "PAGE_IMAGES": _page_images_text(manifest),
        "SUMMARY_STRUCTURE": "",
    }

    kind = task.kind
    ledger = _load_ledger(run_dir)

    if kind == "digest":
        selected = select_sections(
            manifest,
            run_dir,
            persona="methodology",
            max_tokens=max_paper_tokens,
            include_references=True,
        )
    elif kind == "reviewer_report" and task.round == 0:
        persona_id = task.persona
        persona = get_persona(persona_id)
        selected = select_sections(
            manifest,
            run_dir,
            persona=persona_id,
            max_tokens=max_paper_tokens,
            include_references=include_references and persona_id == "novelty",
        )
        values["REVIEWER_ID"] = task.actor_id
        values["REVIEWER_LABEL"] = task.label
        values["PERSONA_ID"] = persona_id
        values["PERSONA_NAME"] = str(persona.get("name", persona_id))
        values["PERSONA_CARD"] = persona_card(persona_id)
    elif kind == "reviewer_report" and task.round >= 1:
        persona_id = task.persona
        persona = get_persona(persona_id)
        prior = load_json(review_path(run_dir, task.actor_id, task.round - 1))
        if not prior:
            raise ConfigError(f"Prior review missing for {task.actor_id} round {task.round - 1}")
        prior_all = _prior_round_reviews(run_dir, run, task.round - 1)
        other = [review for review in prior_all if str(review.get("reviewer_id")) != task.actor_id]
        rebuttal = _load_rebuttal(run_dir, run, task.round)
        selected = select_sections(
            manifest,
            run_dir,
            persona=persona_id,
            max_tokens=max_paper_tokens,
            include_references=include_references and persona_id == "novelty",
            preferred_ids=_section_ids_for_issues([prior] + other, ledger),
        )
        values["REVIEWER_ID"] = task.actor_id
        values["REVIEWER_LABEL"] = task.label
        values["PERSONA_ID"] = persona_id
        values["PERSONA_NAME"] = str(persona.get("name", persona_id))
        values["PERSONA_CARD"] = persona_card(persona_id)
        values["OWN_PRIOR_REVIEW_JSON"] = bounded_json(prior, int(max_prior_tokens * 0.35), note="own prior review compacted")
        values["OTHER_REVIEWS_ANONYMIZED"] = bounded_json(anonymized_reviews(other, labels, mode="cross"), int(max_prior_tokens * 0.30), note="other reviews compacted")
        values["REBUTTAL_JSON"] = bounded_json(
            compact_rebuttal(rebuttal),
            max(6000, int(max_prior_tokens * 0.60)),
            note="author rebuttal compacted (statuses/responses kept)",
        )
        values["LEDGER_SUMMARY"] = bounded_json(ledger_view(ledger, include_evidence=False, label_map=labels), int(max_prior_tokens * 0.15), note="ledger summary compacted") if ledger else "{}"
        evidence = evidence_report(run_dir)
        values["EVIDENCE_VERIFICATION"] = bounded_json(evidence.get("summary", {}), max(200, int(max_prior_tokens * 0.10)), note="verification summary compacted")
    elif kind == "author_rebuttal":
        selected = select_sections(
            manifest,
            run_dir,
            persona="methodology",
            max_tokens=max_paper_tokens,
            include_references=include_references,
        )
        prior_all = _prior_round_reviews(run_dir, run, task.round - 1)
        values["ANONYMIZED_REVIEWS"] = bounded_json(anonymized_reviews(prior_all, labels, mode="author"), int(max_prior_tokens * 0.55), note="anonymized reviews compacted")
        if ledger:
            open_issues = open_issues_for_author(ledger, prior_all, labels)
            values["LEDGER_OPEN_ISSUES"] = bounded_json(open_issues, int(max_prior_tokens * 0.35), note="open issues compacted")
        else:
            values["LEDGER_OPEN_ISSUES"] = "[]"
    elif kind == "summary_report":
        selected = select_sections(
            manifest,
            run_dir,
            persona="methodology",
            max_tokens=max_paper_tokens,
            include_references=True,
        )
        values["SUMMARY_STRUCTURE"] = summary_structure(material)
    elif kind == "meta_review":
        final_round = max(0, task.round)
        final_reviews = load_final_reviews(run_dir, run, final_round)
        rebuttals: list[dict[str, Any]] = []
        for round_number in range(1, final_round + 1):
            item = _load_rebuttal(run_dir, run, round_number)
            if item:
                rebuttals.append(item)
        selected = SelectedSections(ids=[], text="", omitted=[], truncated=False, tokens=0)
        values["FINAL_REVIEWS_JSON"] = bounded_json(anonymized_reviews(final_reviews, labels, mode="meta"), int(max_prior_tokens * 0.45), note="final reviews compacted")
        values["REBUTTALS_JSON"] = bounded_json(
            [compact_rebuttal(item) for item in rebuttals],
            max(8000, int(max_prior_tokens * 0.75)),
            note="rebuttals compacted (statuses/responses kept)",
        )
        values["LEDGER_JSON"] = bounded_json(ledger, int(max_prior_tokens * 0.20), note="ledger compacted") if ledger else "{}"
        values["CONTEXT_NOTE"] = (
            "You are the meta reviewer and do not receive the full paper. Use the ledger, final reviews, "
            "author rebuttal and script-generated evidence verification. Do not invent paper text or quotations."
        )
        values["POLICY_DECISION"] = str((state.policy or {}).get("policy_decision", "accept"))
        values["POLICY_COUNTS_JSON"] = json.dumps((state.policy or {}).get("counts", {}), ensure_ascii=False, indent=2)
        values["SCORE_SUMMARY_JSON"] = json.dumps((state.policy or {}).get("scores", {}), ensure_ascii=False, indent=2)
        values["COVERAGE_JSON"] = json.dumps(state.coverage, ensure_ascii=False, indent=2)
        values["EVIDENCE_VERIFICATION_JSON"] = bounded_json(
            compact_evidence_report(run_dir),
            max(600, int(max_prior_tokens * 0.20)),
            note="evidence verification compacted",
        )
    elif kind == "legacy_summary":
        selected = select_sections(manifest, run_dir, persona="methodology", max_tokens=max_paper_tokens, include_references=True)
        full_text = (run_dir / "paper" / "text.txt").read_text(encoding="utf-8")
        values["PAPER_TEXT"] = full_text
    else:
        raise ConfigError(f"Unsupported task kind/round: {kind}/{task.round}")

    if kind != "meta_review":
        values["PAPER_SECTIONS"] = selected.text
        values["RELEVANT_SECTIONS"] = selected.text
        if not selected.ids:
            raise ConfigError(
                "No paper section fits the configured context budget. Increase --max-paper-tokens, "
                "enable --allow-truncation, or use a model with a larger context window."
            )
        values["CONTEXT_NOTE"] = _context_note(
            selected,
            extraction_quality=str(manifest.get("extraction", {}).get("quality", "unknown")),
            isolation=state.isolation,
            extra=f"This is {_human_round(task.round)}.",
        )
        if selected.truncated and not allow_truncation:
            raise ConfigError(
                "The paper does not fit the configured context budget and truncation is disabled. "
                f"Increase --max-paper-tokens (current {max_paper_tokens}) or pass --allow-truncation. "
                f"Omitted sections: {', '.join(selected.omitted) or 'unknown'}."
            )

    role = task.prompt_role
    template = load_template(role)
    common_rendered = render_text(common_body, values, where="00-common.md")
    system_body = template.system.replace("{{COMMON_RULES}}", common_rendered)
    system = render_text(system_body, values, where=f"{role}.system")
    user = render_text(template.user, values, where=f"{role}.user")
    combined = f"[SYSTEM]\n{system}\n\n[USER]\n{user}\n"
    task_context = {
        "paper_manifest": "paper/manifest.json",
        "sections": selected.ids,
        "omitted_sections": selected.omitted,
        "digest": "digest/digest.json" if (run_dir / "digest" / "digest.json").is_file() else None,
        "prior_reviews": [],
        "other_reviews": [],
        "rebuttal": None,
        "ledger": "artifacts/ledger.json" if ledger_path(run_dir).is_file() else None,
        "evidence_report": "artifacts/evidence",
        "truncated": selected.truncated,
        "purpose": purpose,
        "material": material,
        "domain": domain_id,
        "page_images": list((manifest.get("vision") or {}).get("page_images") or []),
        "vision": {
            "page_images": list((manifest.get("vision") or {}).get("page_images") or []),
            "fallback": "text_only_with_lower_confidence",
            "note": "If the host/model cannot read page images, use extracted text only, mark table/figure cells as not extractable, and lower confidence.",
        },
    }
    if kind == "reviewer_report" and task.round >= 1:
        task_context["prior_reviews"] = [f"artifacts/reviews/{task.actor_id}.round{task.round - 1}.review.json"]
        task_context["other_reviews"] = [
            f"artifacts/reviews/{review.get('reviewer_id')}.round{task.round - 1}.review.json"
            for review in _prior_round_reviews(run_dir, run, task.round - 1)
            if str(review.get("reviewer_id")) != task.actor_id
        ]
        task_context["rebuttal"] = f"artifacts/rebuttals/{author_id}.round{task.round}.rebuttal.json" if _load_rebuttal(run_dir, run, task.round) else None
    return RenderedPrompt(
        system=system,
        user=user,
        combined=combined,
        sha256=sha256_text(combined),
        context=task_context,
        values=values,
        meta=template.meta,
    )


def write_prompt_files(run_dir: Path, task_id: str, rendered: RenderedPrompt) -> tuple[str, str]:
    prompt_dir = run_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    md_rel = f"prompts/{task_id}.prompt.md"
    json_rel = f"prompts/{task_id}.prompt.json"
    atomic_write_text(run_dir / md_rel, rendered.combined)
    atomic_write_json(
        run_dir / json_rel,
        {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "sha256": rendered.sha256,
            "meta": rendered.meta,
            "system": rendered.system,
            "user": rendered.user,
            "context": rendered.context,
            "created_at": utc_now_iso(),
        },
    )
    return md_rel, json_rel


def estimate_prompt_tokens(rendered: RenderedPrompt) -> int:
    return estimate_tokens(rendered.combined)
