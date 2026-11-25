"""Schema + semantic validation for every LLM artifact.

Schema validation is enforced when ``jsonschema`` is installed (recommended).
Semantic validation never depends on an LLM and is the part that actually makes
the protocol auditable: page bounds, contiguous quotes, issue coverage, score
changes, policy overrides, and author/reviewer role boundaries.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Sequence

from .config import SCHEMAS_DIR, get_profile
from .errors import ValidationResult
from .ingest import QuoteIndex
from .util import scan_injection, word_count

try:  # optional; the CLI still runs semantic checks without it
    from jsonschema import Draft202012Validator  # type: ignore

    _JSONSCHEMA = True
except Exception:  # pragma: no cover - environment dependent
    Draft202012Validator = None  # type: ignore
    _JSONSCHEMA = False


SCHEMA_VERSION = "hpe/v2"
REVIEWER_ID_RE = re.compile(r"^R[0-9]{1,2}$")
ISSUE_ID_RE = re.compile(r"^(R[0-9]{1,2}-I[0-9]{1,3}|I-[0-9]{3})$")
CANONICAL_ISSUE_RE = re.compile(r"^I-[0-9]{3}$")
DECISION_RANK = {"reject": 0, "major_revision": 1, "minor_revision": 2, "accept": 3}
DIMENSIONS = (
    "novelty",
    "technical_soundness",
    "evidence_strength",
    "reproducibility",
    "clarity",
    "significance",
)
SEVERITIES = ("blocking", "major", "minor", "question", "nit")
TERMINAL_STATUSES = {"resolved", "withdrawn", "out_of_scope"}


def schema_validation_available() -> dict[str, Any]:
    return {"available": _JSONSCHEMA, "implementation": "jsonschema" if _JSONSCHEMA else "structural-fallback"}


def load_schema(schema_name: str) -> dict[str, Any]:
    path = SCHEMAS_DIR / schema_name
    if not path.is_file():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema(payload: Any, schema_name: str) -> list[tuple[str, str]]:
    """Return ``(where, message)`` schema errors."""

    schema = load_schema(schema_name)
    if not isinstance(payload, dict):
        return [("root", "output must be a JSON object")]
    if not _JSONSCHEMA:
        # Minimal fallback: required keys + top-level type. The semantic layer
        # still checks the fields that matter for the protocol.
        missing = [key for key in schema.get("required", []) if key not in payload]
        return [("root", f"missing required field: {key}") for key in missing]
    validator = Draft202012Validator(schema)
    errors: list[tuple[str, str]] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path)):
        where = ".".join(str(part) for part in error.absolute_path) or "root"
        errors.append((where, error.message))
    return errors


def parse_json_output(raw: str) -> tuple[Any, list[str]]:
    """Parse model output with a narrow, documented repair pass."""

    warnings: list[str] = []
    text = raw.strip()
    if text.startswith("```"):
        # Strip a Markdown code fence, preserving the inner payload.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        warnings.append("stripped Markdown code fence")
    try:
        return json.loads(text), warnings
    except json.JSONDecodeError as first_error:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            try:
                value = json.loads(candidate)
                warnings.append(f"recovered JSON object after parse error: {first_error.msg}")
                return value, warnings
            except json.JSONDecodeError:
                pass
        raise ValueError(f"invalid JSON: {first_error.msg} at line {first_error.lineno}, column {first_error.colno}") from first_error


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _page_count(manifest: dict[str, Any]) -> int:
    return int(manifest.get("source", {}).get("pages", 0) or 0)


def _evidence_count_by_kind(issue: dict[str, Any]) -> dict[str, int]:
    counts = {"paper": 0, "absence": 0, "external": 0}
    for evidence in issue.get("evidence", []) or []:
        kind = str(evidence.get("kind"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _weighted_overall(score: dict[str, Any], weights: dict[str, float]) -> float:
    total = 0.0
    weight_sum = 0.0
    for dimension in DIMENSIONS:
        weight = float(weights.get(dimension, 0.0))
        total += float(score.get(dimension, 0.0)) * weight
        weight_sum += weight
    if weight_sum <= 0:
        return float(score.get("overall", 0.0))
    return round(total / weight_sum, 2)


def _rec_rank(recommendation: str) -> int:
    return DECISION_RANK.get(recommendation, -1)


def _issue_key(issue_id: str) -> str:
    return issue_id.strip()


def _open_statuses() -> set[str]:
    return {"open", "responded", "partially_resolved", "disputed"}


def _count_summary(issues: Sequence[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "blocking_open": 0,
        "major_open": 0,
        "minor_open": 0,
        "resolved": 0,
        "disputed": 0,
        "total": len(issues),
        "low_confidence_open": 0,
        "single_source_blocking": 0,
    }
    for issue in issues:
        status = str(issue.get("status", "open"))
        severity = str(issue.get("severity", ""))
        if status == "resolved":
            summary["resolved"] += 1
            continue
        if status == "disputed":
            summary["disputed"] += 1
        if status in _open_statuses():
            if severity == "blocking":
                summary["blocking_open"] += 1
                if len(issue.get("raised_by", [])) <= 1:
                    summary["single_source_blocking"] += 1
            elif severity == "major":
                summary["major_open"] += 1
            elif severity == "minor":
                summary["minor_open"] += 1
            if float(issue.get("confidence", 1.0)) < 0.4 and severity in {"blocking", "major"}:
                summary["low_confidence_open"] += 1
    return summary


def policy_from_summary(summary: dict[str, int]) -> str:
    if summary.get("blocking_open", 0) > 0:
        return "reject"
    if summary.get("major_open", 0) > 0:
        return "major_revision"
    if summary.get("minor_open", 0) >= 3:
        return "minor_revision"
    return "accept"


def apply_meta_to_issues(issues: Sequence[dict[str, Any]], resolutions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("issue_id")): item for item in resolutions}
    out: list[dict[str, Any]] = []
    for issue in issues:
        item = copy.deepcopy(issue)
        resolution = by_id.get(str(issue.get("id")))
        if resolution:
            item["status"] = resolution.get("final_status", issue.get("status"))
            if resolution.get("rationale"):
                item["meta_note"] = resolution.get("rationale")
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Evidence validation
# ---------------------------------------------------------------------------


def _verify_evidence(
    evidence: Any,
    quote_index: QuoteIndex,
    result: ValidationResult,
    *,
    where: str,
    strict: bool,
    issue_severity: str | None = None,
    siblings: Sequence[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    if not isinstance(evidence, dict):
        result.error(where, "evidence must be an object")
        return None
    kind = str(evidence.get("kind", ""))
    if kind not in {"paper", "absence", "external"}:
        result.error(f"{where}.kind", f"invalid evidence kind {kind!r}")
        return None
    match = quote_index.verify(evidence)
    record = {"where": where, "evidence": evidence, "match": match.to_dict()}
    if kind == "external":
        if not str(evidence.get("note") or "").strip():
            result.error(f"{where}.note", "external evidence requires a note")
        if issue_severity in {"blocking", "major"}:
            kinds = [str(item.get("kind")) for item in siblings]
            if not any(item in {"paper", "absence"} for item in kinds):
                result.error(
                    f"{where}",
                    "external evidence cannot be the only support for a blocking/major issue",
                )
        return record
    if match.status in {"bad_page", "empty_quote", "missing_note", "too_short"}:
        result.error(f"{where}", f"{match.status}: {match.detail}")
    elif match.status == "not_found":
        if strict:
            result.error(f"{where}", "quote was not found on the cited page range; it must be a contiguous substring")
        else:
            result.warning(f"{where}", "quote was not found on the cited page range")
    elif match.status == "fuzzy":
        result.warning(f"{where}", f"quote is only a fuzzy match (score {match.score:.2f}); verify it manually")
    return record


def _collect_evidence_records(
    payload: dict[str, Any],
    quote_index: QuoteIndex,
    result: ValidationResult,
    *,
    strict: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def check_list(items: Any, where: str, *, severity: str | None = None) -> None:
        if not isinstance(items, list):
            return
        for index, evidence in enumerate(items):
            record = _verify_evidence(
                evidence,
                quote_index,
                result,
                where=f"{where}[{index}]",
                strict=strict,
                issue_severity=severity,
                siblings=items,
            )
            if record:
                records.append(record)

    # Digest evidence (facts, claims, method, data, experiments, results, limitations).
    problem = payload.get("problem")
    if isinstance(problem, dict):
        check_list(problem.get("evidence"), "problem.evidence")
    for index, claim in enumerate(payload.get("claims", []) or []):
        if isinstance(claim, dict):
            check_list(claim.get("evidence"), f"claims[{index}].evidence")
    method = payload.get("method")
    if isinstance(method, dict):
        check_list(method.get("evidence"), "method.evidence")
        for index, assumption in enumerate(method.get("assumptions", []) or []):
            if isinstance(assumption, dict):
                check_list(assumption.get("evidence"), f"method.assumptions[{index}].evidence")
        for index, component in enumerate(method.get("components", []) or []):
            if isinstance(component, dict):
                check_list(component.get("evidence"), f"method.components[{index}].evidence")
        for index, equation in enumerate(method.get("equations", []) or []):
            if isinstance(equation, dict):
                check_list(equation.get("evidence"), f"method.equations[{index}].evidence")
    data_block = payload.get("data")
    if isinstance(data_block, dict):
        check_list(data_block.get("evidence"), "data.evidence")
    experiments = payload.get("experiments")
    if isinstance(experiments, dict):
        check_list(experiments.get("evidence"), "experiments.evidence")
    results = payload.get("results")
    if isinstance(results, dict):
        check_list(results.get("evidence"), "results.evidence")
        for index, number in enumerate(results.get("numbers", []) or []):
            if isinstance(number, dict):
                check_list(number.get("evidence"), f"results.numbers[{index}].evidence")
    limitations = payload.get("limitations")
    if isinstance(limitations, dict):
        check_list(limitations.get("evidence"), "limitations.evidence")

    for index, issue in enumerate(payload.get("issues", []) or []):
        if isinstance(issue, dict):
            check_list(issue.get("evidence"), f"issues[{index}].evidence", severity=str(issue.get("severity", "")))
    for index, issue in enumerate(payload.get("new_issues", []) or []):
        if isinstance(issue, dict):
            check_list(issue.get("evidence"), f"new_issues[{index}].evidence", severity=str(issue.get("severity", "")))
    for index, strength in enumerate(payload.get("strengths", []) or []):
        if isinstance(strength, dict):
            check_list(strength.get("evidence"), f"strengths[{index}].evidence")
    for index, highlight in enumerate(payload.get("highlights", []) or []):
        if isinstance(highlight, dict):
            check_list(highlight.get("evidence"), f"highlights[{index}].evidence")
    domain_assessment = payload.get("domain_assessment")
    if isinstance(domain_assessment, dict):
        for index, criterion in enumerate(domain_assessment.get("criteria", []) or []):
            if isinstance(criterion, dict):
                check_list(criterion.get("evidence"), f"domain_assessment.criteria[{index}].evidence")
    for index, update in enumerate(payload.get("issue_updates", []) or []):
        if isinstance(update, dict):
            check_list(update.get("evidence"), f"issue_updates[{index}].evidence")
    for index, response in enumerate(payload.get("responses", []) or []):
        if isinstance(response, dict):
            check_list(response.get("paper_evidence"), f"responses[{index}].paper_evidence")
    for index, clarification in enumerate(payload.get("global_clarifications", []) or []):
        if isinstance(clarification, dict):
            check_list(clarification.get("paper_evidence"), f"global_clarifications[{index}].paper_evidence")
    for index, resolution in enumerate(payload.get("issue_resolution", []) or []):
        if isinstance(resolution, dict):
            check_list(resolution.get("evidence"), f"issue_resolution[{index}].evidence")
    return records


def _evidence_summary(records: Sequence[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for record in records:
        status = str((record.get("match") or {}).get("status", "unknown"))
        summary[status] = summary.get(status, 0) + 1
    return summary


def _scan_untrusted_text(text: str, result: ValidationResult, where: str) -> None:
    findings = scan_injection(text)
    if findings:
        result.warning(where, f"possible prompt-injection text detected: {findings[0][:160]}")


# ---------------------------------------------------------------------------
# Reviewer report
# ---------------------------------------------------------------------------


def validate_review(
    payload: dict[str, Any],
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    manifest: dict[str, Any],
    quote_index: QuoteIndex,
    provided_section_ids: Sequence[str] = (),
    prior_review: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    rebuttal: dict[str, Any] | None = None,
    strict: bool = True,
) -> ValidationResult:
    result = ValidationResult(sanitized=copy.deepcopy(payload))
    for where, message in validate_schema(payload, "review.schema.json"):
        result.error(where, message)
    sanitized = result.sanitized
    data = payload if isinstance(payload, dict) else {}
    paper_id = str(manifest.get("paper_id"))
    reviewer_id = str(task.get("actor_id"))
    round_number = int(task.get("round", 0))
    persona = str(task.get("persona"))
    label = str(task.get("label", reviewer_id))

    # Identity
    if data.get("paper_id") != paper_id:
        result.error("paper_id", f"expected {paper_id!r}")
    if data.get("reviewer_id") != reviewer_id:
        result.error("reviewer_id", f"expected {reviewer_id!r}")
    if data.get("persona") != persona:
        result.error("persona", f"expected {persona!r}")
    if int(data.get("round", -1)) != round_number:
        result.error("round", f"expected {round_number}")
    expected_mode = "independent" if round_number == 0 else "cross_review"
    if data.get("mode") != expected_mode:
        result.error("mode", f"expected {expected_mode!r}")
    if not data.get("reviewer_label"):
        sanitized["reviewer_label"] = label
    elif str(data.get("reviewer_label")) != label:
        result.warning("reviewer_label", f"expected {label!r}; the host label will be used")

    # Scores
    score = _as_dict(data.get("score"))
    for dimension in ("overall",) + DIMENSIONS:
        value = score.get(dimension)
        if not isinstance(value, (int, float)) or not (1 <= float(value) <= 10):
            result.error(f"score.{dimension}", "must be a number between 1 and 10")
    profile = get_profile(str(run.get("profile", "conference")))
    weights = profile.get("weights", {})
    if all(isinstance(score.get(dimension), (int, float)) for dimension in DIMENSIONS):
        weighted = _weighted_overall(score, weights)
        overall = score.get("overall")
        if isinstance(overall, (int, float)) and abs(float(overall) - weighted) > 1.5:
            result.warning(
                "score.overall",
                f"overall {overall} differs from the profile-weighted score {weighted} by more than 1.5",
            )
    if not (0 <= float(data.get("confidence", 0)) <= 1):
        result.error("confidence", "must be between 0 and 1")
    summary = str(data.get("summary", ""))
    summary_words = word_count(summary)
    if summary_words < 80:
        result.warning("summary", f"summary is short ({summary_words} words); it may not explain the judgment")
    if summary_words > 400:
        result.warning("summary", f"summary is long ({summary_words} words)")

    # Issue IDs and issue-level checks
    seen_ids: set[str] = set()
    all_issues: list[dict[str, Any]] = []
    for collection_name in ("issues", "new_issues"):
        for index, issue in enumerate(data.get(collection_name, []) or []):
            if not isinstance(issue, dict):
                continue
            all_issues.append(issue)
            issue_id = str(issue.get("id", ""))
            if issue_id in seen_ids:
                result.error(f"{collection_name}[{index}].id", f"duplicate issue id {issue_id!r}")
            seen_ids.add(issue_id)
            if not issue_id.startswith(f"{reviewer_id}-I"):
                result.error(f"{collection_name}[{index}].id", f"issue id must start with {reviewer_id}-I")
            evidence = issue.get("evidence") or []
            if not evidence:
                result.error(f"{collection_name}[{index}].evidence", "at least one evidence object is required")
            severity = str(issue.get("severity", ""))
            counts = _evidence_count_by_kind(issue)
            if severity in {"blocking", "major"} and counts.get("paper", 0) + counts.get("absence", 0) == 0:
                result.error(f"{collection_name}[{index}].evidence", "a blocking/major issue needs paper or absence evidence")
            if severity in {"blocking", "major"} and float(issue.get("confidence", 0)) < 0.4:
                result.warning(f"{collection_name}[{index}].confidence", "low confidence for a blocking/major issue")
            if severity in {"blocking", "major"} and not issue.get("claim_type"):
                result.warning(f"{collection_name}[{index}].claim_type", "mark paper_claim/reviewer_inference/external_knowledge")
            if issue.get("status") is None:
                sanitized[collection_name][index]["status"] = "open"
            if str(issue.get("requested_action", "")).strip() == "":
                result.error(f"{collection_name}[{index}].requested_action", "must be specific")
            for related in issue.get("related_issue_ids", []) or []:
                if not ISSUE_ID_RE.match(str(related)):
                    result.error(f"{collection_name}[{index}].related_issue_ids", f"invalid issue id {related!r}")

    # Strengths
    seen_strength: set[str] = set()
    for index, strength in enumerate(data.get("strengths", []) or []):
        if not isinstance(strength, dict):
            continue
        sid = str(strength.get("id", ""))
        if sid in seen_strength:
            result.error(f"strengths[{index}].id", f"duplicate strength id {sid!r}")
        seen_strength.add(sid)
        if not sid.startswith(f"{reviewer_id}-S"):
            result.error(f"strengths[{index}].id", f"strength id must start with {reviewer_id}-S")
        if not strength.get("evidence"):
            result.error(f"strengths[{index}].evidence", "at least one evidence object is required")

    # Highlights / domain assessment
    highlights = [item for item in _as_list(data.get("highlights")) if isinstance(item, dict)]
    if not highlights:
        result.warning("highlights", "no innovation/technical highlight was reported; explain in summary/missing_evidence")
    seen_highlights: set[str] = set()
    for index, highlight in enumerate(highlights):
        hid = str(highlight.get("id", ""))
        if not hid.startswith(f"{reviewer_id}-H"):
            result.error(f"highlights[{index}].id", f"highlight id must start with {reviewer_id}-H")
        if hid in seen_highlights:
            result.error(f"highlights[{index}].id", f"duplicate highlight id {hid!r}")
        seen_highlights.add(hid)
        if not highlight.get("evidence"):
            result.error(f"highlights[{index}].evidence", "at least one evidence object is required")
        if str(highlight.get("kind")) not in {"innovation", "technical", "significance", "methodological"}:
            result.error(f"highlights[{index}].kind", "invalid highlight kind")
    domain_assessment = data.get("domain_assessment")
    if isinstance(domain_assessment, dict):
        domain_id = str(domain_assessment.get("domain") or "")
        expected_domain = str(run.get("domain", ""))
        if domain_id and expected_domain and domain_id != expected_domain:
            result.warning("domain_assessment.domain", f"expected {expected_domain!r}, got {domain_id!r}")
        for index, criterion in enumerate(_as_list(domain_assessment.get("criteria"))):
            if not isinstance(criterion, dict):
                result.error(f"domain_assessment.criteria[{index}]", "criterion must be an object")
                continue
            score = criterion.get("score")
            if not isinstance(score, (int, float)) or not (1 <= float(score) <= 10):
                result.error(f"domain_assessment.criteria[{index}].score", "score must be 1–10")
            if not str(criterion.get("comment") or "").strip():
                result.error(f"domain_assessment.criteria[{index}].comment", "criterion needs a comment")

    # Coverage
    coverage = _as_dict(data.get("coverage"))
    sections_read = coverage.get("sections_read") or []
    if not sections_read:
        result.error("coverage.sections_read", "must list the section IDs actually read")
    provided = set(map(str, provided_section_ids))
    unknown = [section for section in sections_read if provided and str(section) not in provided]
    if unknown:
        result.warning("coverage.sections_read", f"section IDs not in the provided context: {', '.join(unknown)}")
    if coverage.get("truncated") is True and float(data.get("confidence", 0)) > 0.6:
        result.warning("confidence", "context was truncated, but confidence is above 0.6")
    if coverage.get("truncated") is True and not sections_read:
        result.error("coverage", "truncated review must list the sections actually read")
    images_read = coverage.get("images_read") or []
    available_pages = {
        int(item.get("page"))
        for item in ((manifest.get("vision") or {}).get("page_images") or [])
        if isinstance(item.get("page"), int)
    }
    unknown_images = sorted({int(page) for page in images_read if isinstance(page, int)} - available_pages)
    if unknown_images:
        result.warning("coverage.images_read", f"pages not offered as page images: {unknown_images}")
    if available_pages and not images_read:
        result.warning(
            "coverage.images_read",
            "page images were available but the review reports none inspected; if the host/model cannot read images, "
            "say so in missing_evidence/limitations and lower confidence",
        )

    # Recommendation consistency
    severities = {str(issue.get("severity")) for issue in all_issues}
    recommendation = str(data.get("recommendation", ""))
    if "blocking" in severities and recommendation != "reject":
        result.warning("recommendation", "a blocking issue is present; reject is the default consistent recommendation")
    if "major" in severities and recommendation == "accept":
        result.warning("recommendation", "an open major issue is present; accept is inconsistent")
    if not all_issues and recommendation in {"reject", "major_revision"} and not data.get("issue_updates"):
        result.warning("recommendation", "no issue is reported, but the recommendation is negative")

    # Round-specific checks
    if round_number == 0:
        if data.get("issue_updates"):
            result.error("issue_updates", "round 0 must not contain issue_updates")
        if data.get("new_issues"):
            result.error("new_issues", "round 0 must not contain new_issues; use issues")
        if data.get("position_changes"):
            result.warning("position_changes", "round 0 has no previous position; this field should be empty")
        if any(issue.get("related_issue_ids") for issue in all_issues):
            result.warning(
                "related_issue_ids",
                "round-0 reviewers are blind; related_issue_ids must be empty because only own issues are visible",
            )
    else:
        if not prior_review:
            result.error("round", "a prior review is required for cross-review")
            prior_ids: list[str] = []
            prior_scores: dict[str, Any] = {}
        else:
            prior_ids = []
            for issue in list(prior_review.get("issues", [])) + list(prior_review.get("new_issues", [])):
                if issue.get("id"):
                    prior_ids.append(str(issue["id"]))
            resolved_prior = {
                str(update.get("issue_id"))
                for update in prior_review.get("issue_updates", [])
                if update.get("status") in TERMINAL_STATUSES
            }
            prior_ids = [issue_id for issue_id in prior_ids if issue_id not in resolved_prior]
            prior_scores = prior_review.get("score") or {}
        updates = [item for item in _as_list(data.get("issue_updates")) if isinstance(item, dict)]
        update_ids = [str(update.get("issue_id")) for update in updates]
        # The prompt permits canonical ledger IDs (`I-001`) in cross-review.
        # Accept either the reviewer's raw ID (`R1-I1`) or its canonical alias.
        canonical_by_raw: dict[str, str] = {}
        if ledger:
            for issue in ledger.get("issues", []):
                canonical = str(issue.get("id"))
                for alias in issue.get("aliases", []):
                    canonical_by_raw[str(alias)] = canonical
        missing_updates: list[str] = []
        for issue_id in prior_ids:
            canonical = canonical_by_raw.get(issue_id)
            if issue_id not in update_ids and (canonical is None or canonical not in update_ids):
                missing_updates.append(issue_id)
        if missing_updates:
            result.error("issue_updates", f"prior issue(s) have no update: {', '.join(missing_updates)}")
        if len(update_ids) != len(set(update_ids)):
            result.error("issue_updates", "duplicate issue update entries")
        known_ids = set(prior_ids)
        if ledger:
            for issue in ledger.get("issues", []):
                known_ids.add(str(issue.get("id")))
                known_ids.update(str(alias) for alias in issue.get("aliases", []))
        for index, update in enumerate(updates):
            issue_id = str(update.get("issue_id"))
            if known_ids and issue_id not in known_ids:
                result.error(f"issue_updates[{index}].issue_id", f"unknown issue {issue_id!r}")
            status = str(update.get("status"))
            if status in {"resolved", "partially_resolved", "disputed"} and not update.get("evidence"):
                result.warning(f"issue_updates[{index}].evidence", "status change has no evidence; cite the page/quote or author response")
            ref = update.get("author_response_ref")
            if ref and rebuttal:
                valid_refs = set()
                for response in rebuttal.get("responses", []):
                    valid_refs.add(str(response.get("issue_id")))
                    if response.get("response_id"):
                        valid_refs.add(str(response.get("response_id")))
                if str(ref) not in valid_refs:
                    result.error(f"issue_updates[{index}].author_response_ref", f"unknown author response reference {ref!r}")
        new_issues = data.get("new_issues") or []
        if len(new_issues) > 3:
            result.warning("new_issues", f"{len(new_issues)} new issues in a cross-review round; justify each late addition")
        for index, issue in enumerate(new_issues):
            if not str(issue.get("why_new", "")).strip():
                result.error(f"new_issues[{index}].why_new", "new issues need a why_new explanation")
        if not data.get("remaining_concerns"):
            result.warning("remaining_concerns", "cross-review should list remaining concerns (may be empty only if none)")

        # Score changes need an explicit position_changes entry.
        changes = data.get("position_changes") or []
        by_dimension = {str(change.get("dimension")): change for change in changes}
        current_score = data.get("score") or {}
        for dimension in ("overall",) + DIMENSIONS:
            before = prior_scores.get(dimension)
            after = current_score.get(dimension)
            if before is None or after is None:
                continue
            if abs(float(before) - float(after)) > 0.01 and dimension not in by_dimension:
                result.error("position_changes", f"{dimension} changed from {before} to {after} without a position_changes entry")
            if dimension in by_dimension:
                change = by_dimension[dimension]
                if abs(float(change.get("from", -999)) - float(before)) > 0.01:
                    result.error("position_changes", f"{dimension} 'from' does not match the prior score")
                if abs(float(change.get("to", -999)) - float(after)) > 0.01:
                    result.error("position_changes", f"{dimension} 'to' does not match the current score")
        prior_rec = prior_review.get("recommendation") if prior_review else None
        if prior_rec and prior_rec != recommendation and "recommendation" not in by_dimension:
            result.error("position_changes", "recommendation changed without a position_changes entry")

    # Evidence verification (after structural checks)
    _scan_untrusted_text(summary, result, "summary")
    for issue in all_issues:
        _scan_untrusted_text(
            " ".join(str(issue.get(key, "")) for key in ("title", "claim", "why_it_matters", "requested_action")),
            result,
            f"issues.{issue.get('id', '?')}",
        )
    records = _collect_evidence_records(data, quote_index, result, strict=strict)
    result.extra["evidence_verification"] = records
    result.extra["evidence_summary"] = _evidence_summary(records)
    result.extra["provided_sections"] = list(provided_section_ids)
    return result


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def validate_digest(
    payload: dict[str, Any],
    *,
    manifest: dict[str, Any],
    quote_index: QuoteIndex,
    strict: bool = True,
) -> ValidationResult:
    result = ValidationResult(sanitized=copy.deepcopy(payload))
    for where, message in validate_schema(payload, "digest.schema.json"):
        result.error(where, message)
    data = payload if isinstance(payload, dict) else {}
    if data.get("paper_id") != str(manifest.get("paper_id")):
        result.error("paper_id", "does not match the manifest")
    if not _as_dict(data.get("problem")).get("statement"):
        result.error("problem.statement", "must be present")
    claim_ids = [str(item.get("id")) for item in data.get("claims", []) if isinstance(item, dict)]
    if len(claim_ids) != len(set(claim_ids)):
        result.error("claims", "claim IDs must be unique")
    quality = str(manifest.get("extraction", {}).get("quality", "unknown"))
    confidence = float(data.get("extraction_confidence", 0.0))
    if quality != "good" and confidence > 0.6:
        result.warning("extraction_confidence", f"extraction quality is {quality}; confidence should be <= 0.6")
    caption_only = [
        item.get("id")
        for key in ("figures", "tables")
        for item in manifest.get(key, [])
        if item.get("content_type") == "caption_only"
    ]
    reported = set(map(str, data.get("not_extractable", [])))
    missing_caption_only = [
        item for item in caption_only
        if item and not any(item in entry for entry in reported)
    ]
    if missing_caption_only:
        result.warning(
            "not_extractable",
            "caption-only figures/tables not listed in not_extractable: " + ", ".join(missing_caption_only[:12]),
        )
    text_blob = json.dumps(
        {
            "problem": data.get("problem"),
            "method": data.get("method"),
            "results": data.get("results"),
            "limitations": data.get("limitations"),
        },
        ensure_ascii=False,
    )
    for word in ("insufficient", "weak", "should be rejected", "not novel", "poor quality"):
        if word in text_blob.lower():
            result.warning("digest", f"evaluative phrase {word!r} found in a fact-only digest")
    records = _collect_evidence_records(data, quote_index, result, strict=strict)
    result.extra["evidence_verification"] = records
    result.extra["evidence_summary"] = _evidence_summary(records)
    return result


# ---------------------------------------------------------------------------
# Author rebuttal
# ---------------------------------------------------------------------------


def validate_summary(
    payload: dict[str, Any],
    *,
    run: dict[str, Any],
    manifest: dict[str, Any],
    quote_index: QuoteIndex,
    strict: bool = True,
) -> ValidationResult:
    result = ValidationResult(sanitized=copy.deepcopy(payload))
    for where, message in validate_schema(payload, "summary_report.schema.json"):
        result.error(where, message)
    data = payload if isinstance(payload, dict) else {}
    if data.get("paper_id") != str(manifest.get("paper_id")):
        result.error("paper_id", "does not match the manifest")
    if data.get("purpose") != "summary":
        result.error("purpose", "summary report must set purpose=summary")
    if data.get("material") != str(run.get("material", "paper")):
        result.error("material", "does not match the run")
    if data.get("domain") != str(run.get("domain", "ai-ict")):
        result.error("domain", "does not match the run")
    expected_sections = {"paper": 20, "proposal": 12, "design": 14}.get(str(run.get("material", "paper")), 20)
    markdown = str(data.get("summary_markdown") or "")
    headings = [int(match.group(1)) for match in re.finditer(r"(?m)^#{1,4}\s*(\d{1,2})[.、)]?\s*", markdown)]
    missing = [number for number in range(1, expected_sections + 1) if number not in headings]
    if missing:
        result.warning("summary_markdown", f"missing expected section numbers: {missing[:10]}")
    if len(markdown) < 800:
        result.warning("summary_markdown", f"summary is short ({len(markdown)} chars)")
    highlights = [item for item in _as_list(data.get("highlights")) if isinstance(item, dict)]
    seen: set[str] = set()
    for index, highlight in enumerate(highlights):
        hid = str(highlight.get("id", ""))
        if not re.fullmatch(r"H[0-9]{1,3}", hid):
            result.error(f"highlights[{index}].id", "highlight id must be H1..H999")
        if hid in seen:
            result.error(f"highlights[{index}].id", f"duplicate highlight id {hid!r}")
        seen.add(hid)
        if not highlight.get("evidence"):
            result.warning(f"highlights[{index}].evidence", "highlight has no evidence")
    images_read = data.get("images_read") or []
    available_pages = {
        int(item.get("page"))
        for item in ((manifest.get("vision") or {}).get("page_images") or [])
        if isinstance(item.get("page"), int)
    }
    unknown_images = sorted({int(page) for page in images_read if isinstance(page, int)} - available_pages)
    if unknown_images:
        result.warning("images_read", f"pages not offered as page images: {unknown_images}")
    if available_pages and not images_read:
        result.warning(
            "images_read",
            "page images were available but the summary reports none inspected; if the host/model cannot read images, "
            "say so in missing_information/limitations and lower confidence",
        )
    records = _collect_evidence_records(data, quote_index, result, strict=strict)
    result.extra["evidence_verification"] = records
    result.extra["evidence_summary"] = _evidence_summary(records)
    return result


def validate_rebuttal(
    payload: dict[str, Any],
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    manifest: dict[str, Any],
    quote_index: QuoteIndex,
    ledger: dict[str, Any],
    strict: bool = True,
    allow_incomplete: bool = False,
) -> ValidationResult:
    result = ValidationResult(sanitized=copy.deepcopy(payload))
    for where, message in validate_schema(payload, "rebuttal.schema.json"):
        result.error(where, message)
    data = payload if isinstance(payload, dict) else {}
    author_id = str((run.get("author") or {}).get("id", "A1"))
    if data.get("paper_id") != str(manifest.get("paper_id")):
        result.error("paper_id", "does not match the manifest")
    if data.get("author_id") != author_id:
        result.error("author_id", f"expected {author_id!r}")
    if int(data.get("round", -1)) != int(task.get("round", 0)):
        result.error("round", f"expected {task.get('round')}")

    canonical_to_aliases: dict[str, set[str]] = {}
    alias_to_canonical: dict[str, str] = {}
    for issue in ledger.get("issues", []):
        canonical = str(issue.get("id"))
        aliases = {canonical} | {str(alias) for alias in issue.get("aliases", [])}
        canonical_to_aliases[canonical] = aliases
        for alias in aliases:
            alias_to_canonical[alias] = canonical
    open_issues = [
        issue
        for issue in ledger.get("issues", [])
        if str(issue.get("status")) not in TERMINAL_STATUSES
    ]
    responses = [item for item in _as_list(data.get("responses")) if isinstance(item, dict)]
    responded: dict[str, int] = {}
    sanitized = result.sanitized
    for index, response in enumerate(responses):
        issue_id = str(response.get("issue_id"))
        canonical = alias_to_canonical.get(issue_id)
        if canonical is None:
            result.error(f"responses[{index}].issue_id", f"unknown ledger issue {issue_id!r}")
            continue
        responded[canonical] = responded.get(canonical, 0) + 1
        status = str(response.get("status"))
        evidence = response.get("paper_evidence") or []
        if status in {"clarify", "reject"} and not evidence:
            result.error(f"responses[{index}].paper_evidence", f"{status} requires paper evidence")
        if status in {"accept", "partially_accept"}:
            revision = str(response.get("revision_plan") or "")
            top_level = [
                item for item in data.get("revision_plan", [])
                if isinstance(item, dict) and alias_to_canonical.get(str(item.get("issue_id"))) == canonical
            ]
            if not revision.strip() and not top_level:
                result.warning(f"responses[{index}].revision_plan", "accept/partially_accept should include a concrete revision plan")
        if not response.get("response_id"):
            try:
                sanitized["responses"][index]["response_id"] = f"{author_id}-{canonical}"
            except (KeyError, IndexError):
                pass
    for issue in open_issues:
        canonical = str(issue.get("id"))
        count = responded.get(canonical, 0)
        if count == 0:
            message = f"open issue {canonical} has no author response"
            if allow_incomplete:
                result.warning("responses", message)
            else:
                result.error("responses", message)
        elif count > 1:
            result.error("responses", f"open issue {canonical} has {count} responses; expected one")

    plan_ids = [str(item.get("issue_id")) for item in data.get("revision_plan", []) if isinstance(item, dict)]
    for issue_id in plan_ids:
        if issue_id not in alias_to_canonical:
            result.error("revision_plan", f"unknown issue {issue_id!r}")
    unresolved = [str(item) for item in data.get("unresolved", [])]
    for issue_id in unresolved:
        if issue_id not in alias_to_canonical:
            result.error("unresolved", f"unknown issue {issue_id!r}")
    if data.get("new_claims"):
        result.warning("new_claims", "new_claims are not evidence; meta review will ignore claims that are not in the paper")
    for index, response in enumerate(responses):
        _scan_untrusted_text(str(response.get("response", "")), result, f"responses[{index}].response")
    records = _collect_evidence_records(data, quote_index, result, strict=strict)
    result.extra["evidence_verification"] = records
    result.extra["evidence_summary"] = _evidence_summary(records)
    return result


# ---------------------------------------------------------------------------
# Meta review
# ---------------------------------------------------------------------------


def _computed_scores(final_reviews: Sequence[dict[str, Any]], profile_name: str) -> dict[str, Any]:
    profile = get_profile(profile_name)
    weights = profile.get("weights", {})
    matrix: list[dict[str, Any]] = []
    dimensions: dict[str, list[float]] = {dimension: [] for dimension in DIMENSIONS}
    overalls: list[float] = []
    for review in final_reviews:
        score = review.get("score") or {}
        if not score:
            continue
        overall = score.get("overall")
        if not isinstance(overall, (int, float)):
            overall = _weighted_overall(score, weights)
        overalls.append(float(overall))
        for dimension in DIMENSIONS:
            if isinstance(score.get(dimension), (int, float)):
                dimensions[dimension].append(float(score[dimension]))
        matrix.append(
            {
                "reviewer_id": review.get("reviewer_id"),
                "overall": round(float(overall), 2),
                "recommendation": review.get("recommendation"),
            }
        )
    if not overalls:
        return {"overall_score": 0.0, "mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "by_dimension": {}, "matrix": matrix}
    overalls_sorted = sorted(overalls)
    mid = len(overalls_sorted) // 2
    median = overalls_sorted[mid] if len(overalls_sorted) % 2 else (overalls_sorted[mid - 1] + overalls_sorted[mid]) / 2
    by_dimension = {
        dimension: round(sum(values) / len(values), 2)
        for dimension, values in dimensions.items()
        if values
    }
    return {
        "overall_score": round(sum(overalls) / len(overalls), 2),
        "mean": round(sum(overalls) / len(overalls), 2),
        "median": round(median, 2),
        "min": round(min(overalls), 2),
        "max": round(max(overalls), 2),
        "by_dimension": by_dimension,
        "matrix": matrix,
    }


def validate_meta(
    payload: dict[str, Any],
    *,
    run: dict[str, Any],
    task: dict[str, Any],
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    final_reviews: Sequence[dict[str, Any]],
    quote_index: QuoteIndex,
    policy_pre: str,
    coverage: dict[str, Any],
    strict: bool = True,
) -> ValidationResult:
    result = ValidationResult(sanitized=copy.deepcopy(payload))
    for where, message in validate_schema(payload, "meta_review.schema.json"):
        result.error(where, message)
    data = payload if isinstance(payload, dict) else {}
    if data.get("paper_id") != str(manifest.get("paper_id")):
        result.error("paper_id", "does not match the manifest")
    if data.get("editor_id") != str(task.get("actor_id", "E1")):
        result.error("editor_id", f"expected {task.get('actor_id', 'E1')!r}")

    resolutions = [item for item in _as_list(data.get("issue_resolution")) if isinstance(item, dict)]
    resolution_ids = [str(item.get("issue_id")) for item in resolutions]
    ledger_ids = [str(issue.get("id")) for issue in ledger.get("issues", [])]
    for issue_id in ledger_ids:
        if issue_id not in resolution_ids:
            result.error("issue_resolution", f"ledger issue {issue_id} has no final resolution")
    for issue_id in resolution_ids:
        if issue_id not in ledger_ids:
            result.error("issue_resolution", f"unknown ledger issue {issue_id!r}")
    if len(resolution_ids) != len(set(resolution_ids)):
        result.error("issue_resolution", "duplicate issue resolutions")
    aliases = {str(alias) for issue in ledger.get("issues", []) for alias in issue.get("aliases", [])}
    for index, resolution in enumerate(resolutions):
        for merged in _as_list(resolution.get("merged_from")):
            if str(merged) not in aliases:
                result.warning(f"issue_resolution[{index}].merged_from", f"unknown alias {merged!r}")

    # Apply resolutions and compute the final policy.
    resolved_issues = apply_meta_to_issues(ledger.get("issues", []), resolutions)
    summary_final = _count_summary(resolved_issues)
    policy_final = policy_from_summary(summary_final)
    result.extra["policy_final"] = policy_final
    result.extra["counts_final"] = summary_final

    decision = str(data.get("decision", ""))
    if data.get("policy_decision") != policy_pre:
        result.warning("policy_decision", f"expected the script-provided {policy_pre!r}; the script will use its own value")
        result.sanitized["policy_decision"] = policy_pre
    if decision != policy_final:
        if not data.get("override"):
            result.error("decision", f"decision {decision!r} differs from the final policy {policy_final!r} without override=true")
        else:
            if not str(data.get("override_reason") or "").strip() or len(str(data.get("override_reason"))) < 10:
                result.error("override_reason", "an override needs a substantive reason (at least 10 characters)")
            if float(data.get("confidence", 0.0)) < 0.70:
                result.error("confidence", "an override needs confidence >= 0.70")
            if summary_final.get("blocking_open", 0) > 0 and decision != "reject":
                result.error("decision", "a remaining blocking issue cannot be overridden to a non-reject decision")
            if decision == "accept" and summary_final.get("major_open", 0) > 0:
                result.error("decision", "cannot accept with an open major issue")
    elif data.get("override"):
        result.warning("override", "override=true but the decision matches the policy; the override flag is unnecessary")

    # Scores are computed, not negotiated. Replace inconsistent values and warn.
    computed = _computed_scores(final_reviews, str(run.get("profile", "conference")))
    for key in ("overall_score", "score_summary", "score_matrix"):
        supplied = data.get(key)
        if supplied is None:
            continue
        if key == "overall_score":
            if abs(float(supplied) - float(computed["overall_score"])) > 0.5:
                result.warning(key, f"supplied {supplied} differs from computed {computed['overall_score']}; using computed")
        elif key == "score_summary":
            supplied = _as_dict(supplied)
            if abs(float(supplied.get("mean", 0)) - float(computed["mean"])) > 0.5:
                result.warning(key, "supplied score summary differs from computed; using computed")
        elif key == "score_matrix":
            if len(_as_list(supplied)) != len(computed["matrix"]):
                result.warning(key, "score matrix length differs from the final reviews; using computed")
    result.sanitized["overall_score"] = computed["overall_score"]
    result.sanitized["score_summary"] = {
        "mean": computed["mean"],
        "median": computed["median"],
        "min": computed["min"],
        "max": computed["max"],
        "by_dimension": computed["by_dimension"],
    }
    result.sanitized["score_matrix"] = computed["matrix"]

    if not coverage.get("complete", False):
        result.warning("coverage", "reviewer coverage is incomplete; the decision is provisional")
        result.sanitized["decision_provisional"] = True
    elif data.get("decision_provisional"):
        any_truncated = any(
            bool((review.get("coverage") or {}).get("truncated"))
            for review in final_reviews
        )
        if not any_truncated:
            result.warning("decision_provisional", "coverage is complete; the decision need not be provisional")

    # Required revisions should point at canonical issues.
    for index, item in enumerate(_as_list(data.get("required_revisions"))):
        if not isinstance(item, dict):
            result.error(f"required_revisions[{index}]", "must be an object")
            continue
        if str(item.get("issue_id")) not in ledger_ids:
            result.error(f"required_revisions[{index}].issue_id", f"unknown canonical issue {item.get('issue_id')!r}")
    open_issue_ids = {
        str(issue.get("id"))
        for issue in resolved_issues
        if str(issue.get("status")) in _open_statuses()
        and str(issue.get("severity")) in {"blocking", "major"}
    }
    required_ids = {str(item.get("issue_id")) for item in _as_list(data.get("required_revisions")) if isinstance(item, dict)}
    for issue_id in sorted(open_issue_ids - required_ids):
        result.warning("required_revisions", f"open issue {issue_id} is not in required_revisions")

    # Dissent should be preserved when a reviewer is far from the final call.
    dissent_ids = {str(item.get("reviewer_id")) for item in data.get("dissent", []) if isinstance(item, dict)}
    final_rank = _rec_rank(decision)
    for review in final_reviews:
        reviewer_id = str(review.get("reviewer_id"))
        rec = str(review.get("recommendation"))
        if final_rank >= 0 and abs(_rec_rank(rec) - final_rank) >= 2 and reviewer_id not in dissent_ids:
            result.warning("dissent", f"{reviewer_id} recommendation {rec!r} differs from the decision by at least two levels")

    for word in ("simulated author", "author response", "we will add"):
        if word in json.dumps(data.get("consensus", []), ensure_ascii=False).lower():
            result.warning("consensus", "consensus text references the author response; keep paper evidence primary")
    records = _collect_evidence_records(data, quote_index, result, strict=strict)
    result.extra["evidence_verification"] = records
    result.extra["evidence_summary"] = _evidence_summary(records)
    return result


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------


def validate_artifact(
    kind: str,
    payload: dict[str, Any],
    **kwargs: Any,
) -> ValidationResult:
    if kind == "digest":
        return validate_digest(payload, **kwargs)
    if kind == "reviewer_report":
        return validate_review(payload, **kwargs)
    if kind == "author_rebuttal":
        return validate_rebuttal(payload, **kwargs)
    if kind == "summary_report":
        return validate_summary(payload, **kwargs)
    if kind == "meta_review":
        return validate_meta(payload, **kwargs)
    raise ValueError(f"Unknown artifact kind {kind!r}")
