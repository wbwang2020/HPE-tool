"""Deterministic issue-ledger construction.

The ledger is the single source of truth for "which problems are still open".
It is built only by this module; an LLM never writes it.

Merging has two layers:
1. explicit aliases (``related_issue_ids`` and previous canonical IDs), and
2. a conservative fingerprint similarity for issues that different blind
   reviewers described in nearly the same words. The fingerprint is deliberately
   strict: title similarity plus compatible category plus evidence-page overlap
   (or a very high title score). This prevents duplicate minor issues from
   inflating the deterministic decision policy.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .util import normalize_whitespace, unique_preserve, utc_now_iso

SCHEMA_VERSION = "hpe/v2"
SEVERITY_RANK = {"blocking": 5, "major": 4, "minor": 3, "question": 2, "nit": 1}
OPEN_STATUSES = {"open", "responded", "partially_resolved", "disputed"}
TERMINAL_STATUSES = {"resolved", "withdrawn", "out_of_scope"}

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "and",
    "or",
    "is",
    "are",
    "be",
    "with",
    "by",
    "this",
    "that",
    "it",
    "as",
    "at",
    "from",
    "we",
    "our",
    "paper",
    "does",
    "do",
    "not",
    "no",
    "missing",
    "lack",
    "lacks",
    "缺少",
    "没有",
    "不足",
    "问题",
    "论文",
}


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def add(self, item: str) -> None:
        if item not in self.parent:
            self.parent[item] = item
            self.rank[item] = 0

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in self.parent:
            out.setdefault(self.find(item), []).append(item)
        return out


# ---------------------------------------------------------------------------
# Raw issue collection
# ---------------------------------------------------------------------------


@dataclass
class RawIssue:
    node_id: str
    reviewer: str
    round: int
    order: tuple[int, int, int]
    data: dict[str, Any]


def _reviewer_index(reviewer_id: str) -> int:
    match = re.match(r"R([0-9]+)", reviewer_id)
    return int(match.group(1)) if match else 999


def _collect_raw_issues(reviews_by_round: dict[int, list[dict[str, Any]]]) -> list[RawIssue]:
    raw: list[RawIssue] = []
    for round_number in sorted(reviews_by_round):
        reviews = reviews_by_round[round_number]
        for review_index, review in enumerate(reviews):
            reviewer = str(review.get("reviewer_id", f"R{review_index + 1}"))
            collections: list[tuple[str, list[dict[str, Any]]]] = []
            if round_number == 0:
                collections.append(("issues", list(review.get("issues", []))))
            else:
                collections.append(("new_issues", list(review.get("new_issues", []))))
                collections.append(("issues", list(review.get("issues", []))))
            for collection_name, issues in collections:
                for issue_index, issue in enumerate(issues):
                    if not isinstance(issue, dict):
                        continue
                    node_id = str(issue.get("id") or f"{reviewer}-I{issue_index + 1}")
                    raw.append(
                        RawIssue(
                            node_id=node_id,
                            reviewer=reviewer,
                            round=round_number,
                            order=(round_number, _reviewer_index(reviewer), issue_index),
                            data=copy.deepcopy(issue),
                        )
                    )
    return raw


# ---------------------------------------------------------------------------
# Fingerprint similarity
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    text = normalize_whitespace(text).casefold()
    latin = [token for token in re.findall(r"[a-z0-9]+", text) if token not in _STOPWORDS and len(token) > 1]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return set(latin) | set(cjk)


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", normalize_whitespace(text).casefold())
    return {text[index : index + 2] for index in range(max(0, len(text) - 1))}


def title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    token_score = 0.0
    if left_tokens and right_tokens:
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    left_bigrams, right_bigrams = _bigrams(left), _bigrams(right)
    bigram_score = 0.0
    if left_bigrams and right_bigrams:
        bigram_score = len(left_bigrams & right_bigrams) / len(left_bigrams | right_bigrams)
    return max(token_score, bigram_score)


def _pages(issue: dict[str, Any]) -> set[int]:
    pages: set[int] = set()
    for evidence in issue.get("evidence", []) or []:
        page = evidence.get("page")
        if isinstance(page, int):
            pages.add(page)
            page_end = evidence.get("page_end")
            if isinstance(page_end, int) and page_end > page:
                pages.update(range(page, min(page_end, page + 20) + 1))
    return pages


def _category_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    if "other" in {left, right}:
        return True
    return {left, right} <= {"experiments", "statistics"}


# ---------------------------------------------------------------------------
# Status aggregation
# ---------------------------------------------------------------------------


@dataclass
class StatusVote:
    reviewer: str
    round: int
    status: str
    rationale: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    author_ref: str | None = None


def _aggregate_status(votes: Sequence[StatusVote], *, has_author_response: bool) -> str:
    if not votes:
        return "responded" if has_author_response else "open"
    statuses = {vote.status for vote in votes}
    if statuses <= {"withdrawn", "out_of_scope"}:
        return "withdrawn" if "withdrawn" in statuses else "out_of_scope"
    if statuses == {"resolved"}:
        return "resolved"
    if "disputed" in statuses:
        return "disputed"
    if "open" in statuses:
        return "open"
    if "partially_resolved" in statuses:
        return "partially_resolved"
    if "resolved" in statuses:
        return "resolved"
    return "responded" if has_author_response else "open"


# ---------------------------------------------------------------------------
# Ledger construction
# ---------------------------------------------------------------------------


def _evidence_key(evidence: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(evidence.get("kind", "")),
        str(evidence.get("page", "")),
        str(evidence.get("page_end", "")),
        normalize_whitespace(str(evidence.get("quote", ""))).casefold(),
        normalize_whitespace(str(evidence.get("note", ""))).casefold(),
    )


def _dedupe_evidence(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _evidence_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(copy.deepcopy(item))
    return out


def _summary(issues: Sequence[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "blocking_open": 0,
        "major_open": 0,
        "minor_open": 0,
        "resolved": 0,
        "disputed": 0,
        "total": len([issue for issue in issues if not issue.get("merged_into")]),
        "low_confidence_open": 0,
        "single_source_blocking": 0,
        "flickering": 0,
    }
    for issue in issues:
        status = str(issue.get("status", "open"))
        severity = str(issue.get("severity", ""))
        if issue.get("flickering"):
            summary["flickering"] += 1
        if status == "resolved":
            summary["resolved"] += 1
            continue
        if status == "disputed":
            summary["disputed"] += 1
        if status in OPEN_STATUSES:
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


def build_ledger(
    *,
    paper_id: str,
    reviews_by_round: dict[int, list[dict[str, Any]]],
    rebuttals: Sequence[dict[str, Any]] = (),
    coverage: dict[str, Any] | None = None,
    previous_ledger: dict[str, Any] | None = None,
    meta_resolutions: Sequence[dict[str, Any]] = (),
    rounds_completed: int = 0,
) -> dict[str, Any]:
    raw_issues = _collect_raw_issues(reviews_by_round)
    uf = UnionFind()
    raw_by_id: dict[str, RawIssue] = {}
    for item in raw_issues:
        uf.add(item.node_id)
        raw_by_id.setdefault(item.node_id, item)

    previous_alias_to_canonical: dict[str, str] = {}
    previous_components: dict[str, set[str]] = {}
    if previous_ledger:
        for issue in previous_ledger.get("issues", []):
            canonical = str(issue.get("id"))
            aliases = {str(alias) for alias in issue.get("aliases", [])}
            aliases.add(canonical)
            previous_components[canonical] = aliases
            for alias in aliases:
                previous_alias_to_canonical[alias] = canonical
            uf.add(canonical)
            for alias in aliases:
                if alias in raw_by_id:
                    uf.union(canonical, alias)
            # Keep previous aliases linked even if the raw issue is absent.
            alias_list = sorted(aliases)
            for alias in alias_list[1:]:
                uf.union(alias_list[0], alias)

    # Explicit related-issue links. Round 0 is blind: a reviewer can only link
    # its own issues, and models tend to over-use this as a clustering dump. Do
    # not allow round-0 self-links to collapse unrelated issues. Cross-review
    # (round >= 1) may link another reviewer's issue to support it.
    for item in raw_issues:
        if item.round == 0:
            continue
        for related in item.data.get("related_issue_ids", []) or []:
            related = str(related)
            if related in raw_by_id:
                uf.union(item.node_id, related)
            elif related in previous_alias_to_canonical:
                uf.union(item.node_id, previous_alias_to_canonical[related])

    # Conservative fingerprint merge across reviewers.
    nodes = list(raw_by_id.values())
    for left_index, left in enumerate(nodes):
        for right in nodes[left_index + 1 :]:
            if left.reviewer == right.reviewer:
                continue
            if uf.find(left.node_id) == uf.find(right.node_id):
                continue
            left_title = str(left.data.get("title", ""))
            right_title = str(right.data.get("title", ""))
            similarity = title_similarity(left_title, right_title)
            if similarity < 0.72:
                continue
            if not _category_compatible(str(left.data.get("category", "")), str(right.data.get("category", ""))):
                continue
            left_pages, right_pages = _pages(left.data), _pages(right.data)
            page_overlap = bool(left_pages & right_pages)
            if not page_overlap and similarity < 0.86:
                continue
            # Very short generic titles ("missing variance") are merged when
            # categories and pages agree; otherwise require a high score.
            if len(normalize_whitespace(left_title)) < 12 and similarity < 0.85:
                continue
            uf.union(left.node_id, right.node_id)

    # Group nodes and preserve previous canonical IDs where possible.
    groups = uf.groups()
    used_canonical: set[str] = set()
    next_index = 1
    components: list[dict[str, Any]] = []
    for root, members in groups.items():
        member_raw = [raw_by_id[item] for item in members if item in raw_by_id]
        if not member_raw:
            continue
        previous_ids = sorted(
            {
                previous_alias_to_canonical[item]
                for item in members
                if item in previous_alias_to_canonical
            }
        )
        canonical = previous_ids[0] if previous_ids else ""
        if canonical and canonical in used_canonical:
            canonical = ""
        if canonical:
            used_canonical.add(canonical)
        components.append(
            {
                "root": root,
                "members": members,
                "raw": sorted(member_raw, key=lambda item: item.order),
                "previous_ids": previous_ids,
                "canonical": canonical,
            }
        )

    # Assign IDs to components without a preserved ID, in first-appearance order.
    components.sort(key=lambda comp: min(item.order for item in comp["raw"]))
    existing_numbers = [int(item[2:]) for item in used_canonical if re.match(r"^I-[0-9]{3}$", item)]
    next_index = max(existing_numbers, default=0) + 1
    for comp in components:
        if not comp["canonical"]:
            while f"I-{next_index:03d}" in used_canonical:
                next_index += 1
            comp["canonical"] = f"I-{next_index:03d}"
            used_canonical.add(comp["canonical"])
            next_index += 1

    # Votes: reviewer updates and author responses.
    votes_by_component: dict[str, list[StatusVote]] = {}
    votes_by_round: dict[str, dict[int, list[str]]] = {}
    for round_number in sorted(reviews_by_round):
        for review in reviews_by_round[round_number]:
            reviewer = str(review.get("reviewer_id", ""))
            for update in review.get("issue_updates", []) or []:
                issue_id = str(update.get("issue_id"))
                root = uf.find(issue_id) if issue_id in uf.parent else None
                if root is None and issue_id in previous_alias_to_canonical:
                    root = uf.find(previous_alias_to_canonical[issue_id])
                if root is None:
                    continue
                comp = next((item for item in components if item["root"] == root), None)
                if not comp:
                    continue
                vote = StatusVote(
                    reviewer=reviewer,
                    round=round_number,
                    status=str(update.get("status", "open")),
                    rationale=str(update.get("rationale", "")),
                    evidence=list(update.get("evidence", []) or []),
                    author_ref=update.get("author_response_ref"),
                )
                votes_by_component.setdefault(comp["canonical"], []).append(vote)
                votes_by_round.setdefault(comp["canonical"], {}).setdefault(round_number, []).append(vote.status)

    responses_by_component: dict[str, list[dict[str, Any]]] = {}
    for rebuttal in rebuttals:
        author_id = str(rebuttal.get("author_id", "A1"))
        for response in rebuttal.get("responses", []) or []:
            issue_id = str(response.get("issue_id"))
            root = uf.find(issue_id) if issue_id in uf.parent else None
            if root is None and issue_id in previous_alias_to_canonical:
                root = uf.find(previous_alias_to_canonical[issue_id])
            if root is None:
                continue
            comp = next((item for item in components if item["root"] == root), None)
            if comp:
                responses_by_component.setdefault(comp["canonical"], []).append(
                    {
                        "response_id": response.get("response_id") or f"{author_id}-{issue_id}",
                        "issue_id": issue_id,
                        "status": response.get("status"),
                        "response": response.get("response"),
                        "paper_evidence": response.get("paper_evidence", []),
                        "revision_plan": response.get("revision_plan"),
                    }
                )

    meta_by_id = {str(item.get("issue_id")): item for item in meta_resolutions}

    issues: list[dict[str, Any]] = []
    for comp in components:
        canonical = comp["canonical"]
        raw = comp["raw"]
        aliases = unique_preserve([item.node_id for item in raw] + sorted(comp["members"]))
        aliases = [alias for alias in aliases if alias != canonical or alias in {item.node_id for item in raw}]
        # Choose representative metadata deterministically.
        severity = max((str(item.data.get("severity", "minor")) for item in raw), key=lambda value: SEVERITY_RANK.get(value, 0))
        ranked = sorted(raw, key=lambda item: (-SEVERITY_RANK.get(str(item.data.get("severity", "minor")), 0), item.order))
        representative = ranked[0].data if ranked else {}
        category_counts: dict[str, int] = {}
        for item in raw:
            category = str(item.data.get("category", "other"))
            category_counts[category] = category_counts.get(category, 0) + 1
        category = max(category_counts, key=lambda value: (category_counts[value], -next((i for i, item in enumerate(raw) if item.data.get("category") == value), 99)))
        confidence = max((float(item.data.get("confidence", 0.0)) for item in raw), default=0.0)
        evidence = _dedupe_evidence(
            evidence
            for item in raw
            for evidence in (item.data.get("evidence", []) or [])
        )
        raised_by = unique_preserve([item.reviewer for item in raw])
        supported_by: list[str] = []
        for item in raw:
            for related in item.data.get("related_issue_ids", []) or []:
                if related in {member.node_id for member in raw}:
                    supported_by.append(item.reviewer)
        for vote in votes_by_component.get(canonical, []):
            if vote.reviewer not in raised_by:
                supported_by.append(vote.reviewer)
        round_opened = min((item.round for item in raw), default=0)

        # Latest vote per reviewer determines their final stance.
        latest: dict[str, StatusVote] = {}
        for vote in votes_by_component.get(canonical, []):
            current = latest.get(vote.reviewer)
            if current is None or vote.round >= current.round:
                latest[vote.reviewer] = vote
        # A reviewer who raised an issue but never supplied a cross-review
        # update (timeout/failure/skip) must not silently make the issue look
        # resolved. Treat the missing stance as open; coverage will also flag it.
        latest_round = max((vote.round for vote in votes_by_component.get(canonical, [])), default=0)
        for reviewer in raised_by:
            if reviewer not in latest:
                latest[reviewer] = StatusVote(
                    reviewer=reviewer,
                    round=latest_round,
                    status="open",
                    rationale="no cross-review update from this reviewer (failed or skipped task)",
                )
        author_responses = responses_by_component.get(canonical, [])
        status = _aggregate_status(list(latest.values()), has_author_response=bool(author_responses))
        resolution_note = ""
        if latest:
            resolution_note = max(latest.values(), key=lambda vote: vote.round).rationale
        if meta_by_id.get(canonical):
            resolution = meta_by_id[canonical]
            status = str(resolution.get("final_status", status))
            resolution_note = str(resolution.get("rationale") or resolution_note)
        # Timeline and flicker detection.
        timeline = [status]
        if votes_by_round.get(canonical):
            for round_number in sorted(votes_by_round[canonical]):
                votes = votes_by_round[canonical][round_number]
                if "disputed" in votes:
                    timeline.append("disputed")
                elif "open" in votes:
                    timeline.append("open")
                elif "partially_resolved" in votes:
                    timeline.append("partially_resolved")
                elif votes and all(vote == "resolved" for vote in votes):
                    timeline.append("resolved")
        flickering = any(
            timeline[index] == "resolved" and timeline[index + 1] in {"open", "partially_resolved", "disputed"}
            for index in range(len(timeline) - 1)
        )
        round_closed = None
        if status == "resolved":
            round_closed = max((vote.round for vote in votes_by_component.get(canonical, []) if vote.status == "resolved"), default=None)
        author_response_ids = unique_preserve(
            [str(response.get("response_id")) for response in author_responses if response.get("response_id")]
        )
        issue: dict[str, Any] = {
            "id": canonical,
            "aliases": aliases,
            "title": str(representative.get("title") or "Untitled issue"),
            "category": category,
            "severity": severity,
            "status": status,
            "raised_by": raised_by,
            "supported_by": unique_preserve(supported_by),
            "evidence": evidence,
            "author_response_ids": author_response_ids,
            "round_opened": round_opened,
            "round_closed": round_closed,
            "confidence": round(confidence, 3),
            "flickering": flickering,
        }
        if resolution_note:
            issue["resolution_note"] = resolution_note
        if meta_by_id.get(canonical):
            issue["meta_note"] = str(meta_by_id[canonical].get("rationale", ""))
        issues.append(issue)

    # Deterministic ordering: first appearance.
    order_map = {comp["canonical"]: min(item.order for item in comp["raw"]) for comp in components}
    issues.sort(key=lambda issue: order_map.get(issue["id"], (99, 99, 99)))
    coverage_data = dict(coverage or {})
    coverage_data.setdefault("reviewers_expected", 1)
    coverage_data.setdefault("reviewers_done", 0)
    coverage_data.setdefault("complete", False)
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper_id,
        "generated_at": utc_now_iso(),
        "generated_by": "hpe.ledger",
        "rounds_completed": rounds_completed,
        "issues": issues,
        "summary": _summary(issues),
        "coverage": coverage_data,
    }
    return ledger


def apply_meta_resolutions(ledger: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Apply meta statuses and absorb duplicate canonical entries.

    ``merged_from`` may contain raw aliases (``R2-I1``) or canonical IDs
    (``I-013``). When it points at another ledger entry, that entry is marked
    ``out_of_scope`` with ``merged_into`` and its aliases/evidence move to the
    canonical target. This keeps the final summary and report free of duplicate
    entries instead of merely annotating them in prose.
    """

    result = copy.deepcopy(ledger)
    issues = result.get("issues", [])
    by_id = {str(issue.get("id")): issue for issue in issues}
    alias_to_canonical: dict[str, str] = {}
    for issue in issues:
        canonical = str(issue.get("id"))
        alias_to_canonical[canonical] = canonical
        for alias in issue.get("aliases", []):
            alias_to_canonical[str(alias)] = canonical

    resolutions = {str(item.get("issue_id")): item for item in meta.get("issue_resolution", [])}
    for issue in issues:
        if issue.get("merged_into"):
            continue
        resolution = resolutions.get(str(issue.get("id")))
        if not resolution:
            continue
        issue["status"] = resolution.get("final_status", issue.get("status"))
        issue["meta_note"] = resolution.get("rationale", "")
        if resolution.get("merged_from"):
            merged = [str(item) for item in resolution.get("merged_from", [])]
            issue["aliases"] = unique_preserve(list(issue.get("aliases", [])) + merged)
            for source_id in {alias_to_canonical.get(item) for item in merged}:
                if not source_id or source_id == issue.get("id"):
                    continue
                source = by_id.get(str(source_id))
                if not source or source.get("merged_into"):
                    continue
                source["status"] = "out_of_scope"
                source["merged_into"] = issue.get("id")
                source["meta_note"] = f"Merged into {issue.get('id')} by meta review."
                issue["aliases"] = unique_preserve(list(issue.get("aliases", [])) + list(source.get("aliases", [])))
                issue["evidence"] = _dedupe_evidence(list(issue.get("evidence", [])) + list(source.get("evidence", [])))
        if issue["status"] == "resolved" and issue.get("round_closed") is None:
            issue["round_closed"] = result.get("rounds_completed")
    result["summary"] = _summary(issues)
    result["generated_at"] = utc_now_iso()
    result["generated_by"] = "hpe.ledger+meta"
    return result


def ledger_open_issue_ids(ledger: dict[str, Any]) -> list[str]:
    return [
        str(issue.get("id"))
        for issue in ledger.get("issues", [])
        if str(issue.get("status")) in OPEN_STATUSES
    ]


def ledger_alias_map(ledger: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for issue in ledger.get("issues", []):
        canonical = str(issue.get("id"))
        mapping[canonical] = canonical
        for alias in issue.get("aliases", []):
            mapping[str(alias)] = canonical
    return mapping


def issue_by_id(ledger: dict[str, Any], issue_id: str) -> dict[str, Any] | None:
    for issue in ledger.get("issues", []):
        if issue.get("id") == issue_id:
            return issue
    return None


def summary_counts(ledger: dict[str, Any]) -> dict[str, int]:
    return dict(ledger.get("summary", {}))


def render_ledger_json(ledger: dict[str, Any]) -> str:
    return json.dumps(ledger, ensure_ascii=False, indent=2)
