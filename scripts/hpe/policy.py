"""Deterministic score aggregation, decision policy and dissent."""

from __future__ import annotations

from typing import Any, Sequence

from .config import get_decision_policy, get_profile

DECISION_RANK = {"reject": 0, "major_revision": 1, "minor_revision": 2, "accept": 3}
DECISION_LABEL = {
    "accept": "Accept",
    "minor_revision": "Minor Revision",
    "major_revision": "Major Revision",
    "reject": "Reject",
}
DIMENSIONS = (
    "novelty",
    "technical_soundness",
    "evidence_strength",
    "reproducibility",
    "clarity",
    "significance",
)
OPEN_STATUSES = {"open", "responded", "partially_resolved", "disputed"}


def decision_rank(decision: str) -> int:
    return DECISION_RANK.get(decision, -1)


def weighted_overall(score: dict[str, Any], weights: dict[str, float]) -> float:
    total = 0.0
    weight_sum = 0.0
    for dimension in DIMENSIONS:
        weight = float(weights.get(dimension, 0.0))
        total += float(score.get(dimension, 0.0)) * weight
        weight_sum += weight
    if weight_sum <= 0:
        return float(score.get("overall", 0.0))
    return round(total / weight_sum, 2)


def aggregate_scores(reviews: Sequence[dict[str, Any]], profile_name: str) -> dict[str, Any]:
    profile = get_profile(profile_name)
    weights = profile.get("weights", {})
    overalls: list[float] = []
    dimensions: dict[str, list[float]] = {dimension: [] for dimension in DIMENSIONS}
    matrix: list[dict[str, Any]] = []
    for review in reviews:
        score = review.get("score") or {}
        if not score:
            continue
        overall = score.get("overall")
        if not isinstance(overall, (int, float)):
            overall = weighted_overall(score, weights)
        overalls.append(float(overall))
        for dimension in DIMENSIONS:
            value = score.get(dimension)
            if isinstance(value, (int, float)):
                dimensions[dimension].append(float(value))
        matrix.append(
            {
                "reviewer_id": review.get("reviewer_id"),
                "overall": round(float(overall), 2),
                "recommendation": review.get("recommendation"),
            }
        )
    if not overalls:
        return {
            "overall_score": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "min": 0.0,
            "max": 0.0,
            "by_dimension": {},
            "matrix": matrix,
        }
    ordered = sorted(overalls)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "overall_score": round(sum(overalls) / len(overalls), 2),
        "mean": round(sum(overalls) / len(overalls), 2),
        "median": round(median, 2),
        "min": round(min(overalls), 2),
        "max": round(max(overalls), 2),
        "by_dimension": {
            dimension: round(sum(values) / len(values), 2)
            for dimension, values in dimensions.items()
            if values
        },
        "matrix": matrix,
    }


def counts_from_issues(issues: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "blocking_open": 0,
        "major_open": 0,
        "minor_open": 0,
        "resolved": 0,
        "disputed": 0,
        "total": len(issues),
        "low_confidence_open": 0,
        "single_source_blocking": 0,
        "flickering": 0,
    }
    for issue in issues:
        status = str(issue.get("status", "open"))
        severity = str(issue.get("severity", ""))
        if issue.get("flickering"):
            counts["flickering"] += 1
        if status == "resolved":
            counts["resolved"] += 1
            continue
        if status == "disputed":
            counts["disputed"] += 1
        if status in OPEN_STATUSES:
            if severity == "blocking":
                counts["blocking_open"] += 1
                if len(issue.get("raised_by", [])) <= 1:
                    counts["single_source_blocking"] += 1
            elif severity == "major":
                counts["major_open"] += 1
            elif severity == "minor":
                counts["minor_open"] += 1
            if float(issue.get("confidence", 1.0)) < 0.4 and severity in {"blocking", "major"}:
                counts["low_confidence_open"] += 1
    return counts


def policy_decision(
    issues: Sequence[dict[str, Any]],
    *,
    policy_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = policy_config or get_decision_policy()
    counts = counts_from_issues(issues)
    blocking_threshold = int((config.get("blocking_open") or {}).get("threshold", 1))
    major_threshold = int((config.get("major_open_gte") or {}).get("threshold", 1))
    minor_threshold = int((config.get("minor_open_gte") or {}).get("threshold", 3))
    if counts["blocking_open"] >= blocking_threshold:
        decision = "reject"
    elif counts["major_open"] >= major_threshold:
        decision = "major_revision"
    elif counts["minor_open"] >= minor_threshold:
        decision = "minor_revision"
    else:
        decision = str(config.get("default", "accept"))
    notes: list[str] = []
    if counts["single_source_blocking"]:
        notes.append(
            f"{counts['single_source_blocking']} blocking issue(s) are raised by a single reviewer; "
            "meta review should verify the evidence before treating them as proven."
        )
    if counts["low_confidence_open"]:
        notes.append(
            f"{counts['low_confidence_open']} open major/blocking issue(s) have confidence < 0.40; "
            "verify before relying on them."
        )
    if counts["disputed"]:
        notes.append(f"{counts['disputed']} issue(s) are disputed and count as open.")
    return {
        "policy_decision": decision,
        "counts": counts,
        "notes": notes,
    }


def policy_bundle(
    *,
    ledger: dict[str, Any],
    final_reviews: Sequence[dict[str, Any]],
    profile_name: str,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    policy = policy_decision(ledger.get("issues", []))
    scores = aggregate_scores(final_reviews, profile_name)
    provisional = not bool(coverage.get("complete", False))
    return {
        "policy_decision": policy["policy_decision"],
        "counts": policy["counts"],
        "notes": policy["notes"],
        "scores": scores,
        "coverage": coverage,
        "provisional": provisional,
    }


def compute_dissent(
    final_reviews: Sequence[dict[str, Any]],
    decision: str,
    *,
    threshold: int = 2,
) -> list[dict[str, Any]]:
    final_rank = decision_rank(decision)
    dissent: list[dict[str, Any]] = []
    for review in final_reviews:
        recommendation = str(review.get("recommendation", ""))
        rank = decision_rank(recommendation)
        if final_rank >= 0 and rank >= 0 and abs(rank - final_rank) >= threshold:
            dissent.append(
                {
                    "reviewer_id": review.get("reviewer_id"),
                    "summary": review.get("summary", "")[:600],
                    "recommendation": recommendation,
                    "severity": "high" if abs(rank - final_rank) >= 2 else "medium",
                }
            )
    return dissent


def decision_provisional(decision: str, *, coverage_complete: bool, policy_decision: str) -> bool:
    if not coverage_complete:
        return True
    return decision != policy_decision
