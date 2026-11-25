"""Deterministic Markdown/JSON rendering. No LLM calls happen here."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import PROMPT_VERSION, SCHEMA_VERSION
from .ledger import apply_meta_resolutions, ledger_alias_map
from .policy import aggregate_scores, compute_dissent, counts_from_issues, policy_decision
from .prompts import load_rebuttals_by_round, load_reviews_by_round
from .util import atomic_write_json, atomic_write_text, read_json, utc_now_iso


SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "question": 3, "nit": 4}
STATUS_LABEL = {
    "open": "Open",
    "responded": "Responded",
    "resolved": "Resolved",
    "partially_resolved": "Partially resolved",
    "disputed": "Disputed",
    "withdrawn": "Withdrawn",
    "out_of_scope": "Out of scope",
}
STATUS_LABEL_ZH = {
    "open": "未解决",
    "responded": "已回应",
    "resolved": "已解决",
    "partially_resolved": "部分解决",
    "disputed": "有争议",
    "withdrawn": "已撤回",
    "out_of_scope": "超出范围",
}


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _author_responses(rebuttals: Sequence[dict[str, Any]], alias_map: dict[str, str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rebuttal in rebuttals:
        for response in rebuttal.get("responses", []) or []:
            canonical = alias_map.get(str(response.get("issue_id")), str(response.get("issue_id")))
            out[canonical] = response
    return out


def _final_reviews(run_dir: Path, run: dict[str, Any], round_number: int) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for reviewer in run.get("reviewers", []):
        path = run_dir / "artifacts" / "reviews" / f"{reviewer.get('id')}.round{round_number}.review.json"
        data = _load(path)
        if data:
            reviews.append(data)
    return reviews


def _rebuttals(run_dir: Path, run: dict[str, Any], max_round: int) -> list[dict[str, Any]]:
    author_id = str((run.get("author") or {}).get("id", "A1"))
    out: list[dict[str, Any]] = []
    for round_number in range(1, max_round + 1):
        data = _load(run_dir / "artifacts" / "rebuttals" / f"{author_id}.round{round_number}.rebuttal.json")
        if data:
            out.append(data)
    return out


def _synthetic_meta(
    *,
    paper_id: str,
    decision: str,
    ledger: dict[str, Any],
    scores: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    issues = ledger.get("issues", [])
    resolutions = [
        {
            "issue_id": issue.get("id"),
            "final_status": issue.get("status") if issue.get("status") in {"resolved", "partially_resolved", "disputed", "open", "out_of_scope"} else "open",
            "merged_from": issue.get("aliases", []),
            "rationale": reason,
        }
        for issue in issues
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper_id,
        "editor_id": "E1",
        "decision": decision,
        "policy_decision": decision,
        "decision_provisional": True,
        "override": False,
        "override_reason": None,
        "overall_score": scores.get("overall_score", 0.0),
        "score_summary": {
            "mean": scores.get("mean", 0.0),
            "median": scores.get("median", 0.0),
            "min": scores.get("min", 0.0),
            "max": scores.get("max", 0.0),
            "by_dimension": scores.get("by_dimension", {}),
        },
        "score_matrix": scores.get("matrix", []),
        "issue_resolution": resolutions,
        "consensus": [],
        "contested": [],
        "dissent": [],
        "required_revisions": [
            {"issue_id": issue.get("id"), "action": issue.get("title", ""), "priority": "must"}
            for issue in issues
            if issue.get("severity") in {"blocking", "major"} and issue.get("status") in {"open", "responded", "partially_resolved", "disputed"}
        ],
        "optional_suggestions": [],
        "confidence": 0.3,
        "limitations": [reason],
    }


def build_report(
    *,
    run_dir: Path,
    run: dict[str, Any],
    state: Any,
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    meta: dict[str, Any] | None,
    final_reviews: Sequence[dict[str, Any]],
    rebuttals: Sequence[dict[str, Any]],
    usage: dict[str, Any],
) -> dict[str, Any]:
    profile_name = str(run.get("profile", "conference"))
    scores = aggregate_scores(final_reviews, profile_name)
    coverage = dict(state.coverage or ledger.get("coverage", {}))
    final_ledger = apply_meta_resolutions(ledger, meta) if meta else ledger
    final_issues = [issue for issue in final_ledger.get("issues", []) if not issue.get("merged_into")]
    counts = counts_from_issues(final_issues)
    policy_final = policy_decision(final_issues)["policy_decision"]
    if meta:
        decision = str(meta.get("decision", policy_final))
        override = bool(meta.get("override", False))
        override_reason = meta.get("override_reason")
        meta_confidence = float(meta.get("confidence", 0.5))
        provisional = bool(meta.get("decision_provisional", not coverage.get("complete", False)))
        dissent = list(meta.get("dissent", []))
        active_ids = {str(issue.get("id")) for issue in final_issues}
        required = []
        for item in meta.get("required_revisions", []):
            issue_id = str(item.get("issue_id"))
            if issue_id not in active_ids:
                continue
            line = f"{issue_id}: {item.get('action')}"
            if line not in required:
                required.append(line)
    else:
        decision = policy_final
        override = False
        override_reason = None
        meta_confidence = 0.3
        provisional = True
        dissent = compute_dissent(final_reviews, decision)
        required = [
            f"{issue.get('id')}: {issue.get('title')}"
            for issue in final_issues
            if issue.get("severity") in {"blocking", "major"}
            and issue.get("status") in {"open", "responded", "partially_resolved", "disputed"}
        ]

    review_rounds = load_reviews_by_round(run_dir, run, int(getattr(state, "round", 0)))
    rebuttal_rounds = load_rebuttals_by_round(run_dir, run, int(getattr(state, "round", 0)))
    final_opinion = {
        "decision": decision,
        "policy_decision": policy_final,
        "override": override,
        "override_reason": override_reason,
        "provisional": provisional,
        "confidence": round(meta_confidence, 3),
        "consensus": list((meta or {}).get("consensus", [])),
        "contested": list((meta or {}).get("contested", [])),
        "dissent": list(dissent),
        "required_revisions": list(required),
        "optional_suggestions": list((meta or {}).get("optional_suggestions", [])),
        "limitations": list((meta or {}).get("limitations", [])),
        "rounds_completed": int(getattr(state, "round", 0)),
        "reviewers_expected": int(coverage.get("reviewers_expected", 0) or 0),
        "reviewers_done": int(coverage.get("reviewers_done", 0) or 0),
    }

    warnings: list[dict[str, Any]] = []
    seen_warnings: set[tuple[str, str]] = set()

    def add_warning(code: str, message: str, severity: str = "warning") -> None:
        key = (str(code), str(message))
        if key in seen_warnings:
            return
        seen_warnings.add(key)
        warnings.append({"code": str(code), "message": str(message), "severity": severity})

    for warning in state.warnings:
        add_warning(str(warning.get("code")), str(warning.get("message")), str(warning.get("severity", "warning")))
    for warning in manifest.get("extraction", {}).get("warnings", []) or []:
        add_warning("extraction", str(warning), "warning")
    if not coverage.get("complete", False):
        add_warning(
            "coverage.incomplete",
            f"Only {coverage.get('reviewers_done', 0)}/{coverage.get('reviewers_expected', '?')} reviewers completed; the decision is provisional.",
        )
    if state.isolation == "shared":
        add_warning("isolation.shared", "Reviewer tasks ran in a shared host context; independence is not guaranteed.")
    if (run.get("author") or {}).get("mode") == "simulated":
        add_warning("author.simulated", "The author rebuttal was simulated by a model; it is not a real author response.", "info")
    if not usage.get("known", False):
        add_warning("usage.unknown", "Token usage was not reported by the host; cost is not verified.", "info")

    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.get("run_id"),
        "created_at": utc_now_iso(),
        "output_language": run.get("output_language") or ("zh" if str((manifest.get("language") or "en")).startswith("zh") else "en"),
        "paper": {
            "paper_id": manifest.get("paper_id"),
            "title": manifest.get("title", "Untitled"),
            "authors": manifest.get("authors", []),
            "year": manifest.get("year"),
            "venue": "",
            "pages": int(manifest.get("source", {}).get("pages", 1)),
            "sha256": manifest.get("source", {}).get("sha256", ""),
            "language": manifest.get("language", ""),
            "extraction_quality": manifest.get("extraction", {}).get("quality"),
            "extraction_engine": manifest.get("extraction", {}).get("engine"),
        },
        "profile": profile_name,
        "purpose": run.get("purpose", "review"),
        "material": run.get("material", "paper"),
        "domain": run.get("domain", "ai-ict"),
        "domain_label": run.get("domain_label", run.get("domain", "ai-ict")),
        "highlights": [
            dict(item, reviewer_id=review.get("reviewer_id"), reviewer_label=review.get("reviewer_label"))
            for review in final_reviews
            for item in (review.get("highlights") or [])
        ],
        "author_mode": (run.get("author") or {}).get("mode", "simulated"),
        "context_isolation": "shared" if state.isolation == "shared" else "fresh" if state.isolation == "subagent" else "unknown",
        "decision": {
            "recommendation": decision,
            "policy_decision": policy_final,
            "override": override,
            "override_reason": override_reason,
            "overall_score": scores.get("overall_score", 0.0),
            "confidence": round(meta_confidence, 3),
            "provisional": provisional,
        },
        "scores": {
            "mean": scores.get("mean", 0.0),
            "median": scores.get("median", 0.0),
            "min": scores.get("min", 0.0),
            "max": scores.get("max", 0.0),
            "by_dimension": scores.get("by_dimension", {}),
            "matrix": scores.get("matrix", []),
        },
        "issue_summary": {
            "blocking_open": counts["blocking_open"],
            "major_open": counts["major_open"],
            "minor_open": counts["minor_open"],
            "resolved": counts["resolved"],
            "disputed": counts["disputed"],
            "total": counts["total"],
            "low_confidence_open": counts["low_confidence_open"],
            "single_source_blocking": counts["single_source_blocking"],
        },
        "issues": [
            {
                "id": issue.get("id"),
                "aliases": issue.get("aliases", []),
                "title": issue.get("title"),
                "category": issue.get("category"),
                "severity": issue.get("severity"),
                "status": issue.get("status"),
                "raised_by": issue.get("raised_by", []),
                "evidence": issue.get("evidence", []),
                "confidence": issue.get("confidence"),
                "resolution_note": issue.get("resolution_note"),
                "meta_note": issue.get("meta_note"),
                "flickering": issue.get("flickering", False),
            }
            for issue in final_issues
        ],
        "dissent": dissent,
        "required_revisions": required,
        "coverage": coverage,
        "artifacts": {
            "run_dir": run.get("run_id"),
            "report_md": "output/report.md",
            "report_json": "output/report.json",
            "ledger": "artifacts/ledger.final.json" if meta else "artifacts/ledger.json",
            "meta_review": "artifacts/meta_review.json" if meta else "",
            "review_rounds_md": "output/review-rounds.md",
            "final_opinion_md": "output/final-opinion.md",
            "prompts": [],
        },
        "usage": {
            "calls": int(usage.get("calls", 0)),
            "tokens_in": int(usage.get("tokens_in", 0)),
            "tokens_out": int(usage.get("tokens_out", 0)),
            "cost_usd": float(usage.get("cost_usd", 0.0)),
            "known": bool(usage.get("known", False)),
        },
        "warnings": warnings,
        "review_rounds": review_rounds,
        "rebuttal_rounds": rebuttal_rounds,
        "final_opinion": final_opinion,
        "agent": run.get("agent"),
        "backend_consistency": run.get("backend_consistency"),
        "vision": run.get("vision"),
        "runtime": {
            "prompt_version": run.get("prompt_set", PROMPT_VERSION),
            "mode": run.get("mode"),
            "output_language": run.get("output_language"),
            "agent_backend": (run.get("agent") or {}).get("label"),
            "isolation": state.isolation,
            "author_mode": (run.get("author") or {}).get("mode", "simulated"),
            "stop_reason": state.stop_reason,
            "rounds": int(state.round),
            "reviewers": [
                {
                    "id": reviewer.get("id"),
                    "persona": reviewer.get("persona"),
                    "label": reviewer.get("label"),
                    "provider": reviewer.get("provider"),
                    "model": reviewer.get("model"),
                }
                for reviewer in run.get("reviewers", [])
            ],
            "author": run.get("author"),
        },
    }
    return report


def output_language(report: dict[str, Any]) -> str:
    """Resolved output language of the report, not the paper's source language."""

    resolved = str(report.get("output_language") or "").strip()
    if resolved in {"zh", "en"}:
        return resolved
    runtime = report.get("runtime") or {}
    resolved = str(runtime.get("output_language") or "").strip()
    if resolved in {"zh", "en"}:
        return resolved
    paper_language = str((report.get("paper") or {}).get("language") or "en")
    return "zh" if paper_language.startswith("zh") else "en"


def _t(report: dict[str, Any]) -> dict[str, str]:
    if output_language(report) == "zh":
        return {
            "report": "HPE v3 审稿报告",
            "problem_report": "HPE v3 问题查找报告",
            "disclaimer": "本报告由模型辅助生成，仅供研究/写作参考，不构成同行评审结论。所有事实性判断应回到原文与证据页码核验；最终学术判断由人类作者/编辑负责。",
            "summary": "裁决摘要",
            "recommendation": "建议",
            "policy": "政策决定",
            "override": "覆盖政策",
            "provisional": "临时决定",
            "score": "综合分",
            "confidence": "置信度",
            "coverage": "审稿覆盖",
            "paper": "论文信息",
            "scores": "评分矩阵",
            "issues": "问题台账",
            "strengths": "优点",
            "highlights": "创新 / 技术亮点",
            "dissent": "分歧与少数意见",
            "revisions": "必须修订",
            "warnings": "警告与局限",
            "manifest": "运行清单",
            "evidence": "证据索引",
            "status": "状态",
            "severity": "严重度",
            "raised_by": "提出者",
            "response": "作者回应",
            "editor": "编辑判断",
            "no_issues": "未发现问题。",
            "none": "无",
            "yes": "是",
            "no": "否",
            "title": "标题",
            "authors": "作者",
            "year": "年份",
            "pages": "页数",
            "language": "论文语言",
            "extraction": "提取",
            "profile": "评审配置",
            "author_mode": "作者模式",
            "isolation": "上下文隔离",
            "reviewer": "审稿人",
            "overall": "综合",
            "novelty": "创新性",
            "soundness": "技术可靠性",
            "evidence_strength": "证据强度",
            "repro": "可复现性",
            "clarity": "清晰度",
            "significance": "重要性",
            "recommendation_col": "建议",
            "mean": "均值",
            "median": "中位数",
            "min": "最小",
            "max": "最大",
            "flickering": "状态反复",
            "revision_plan": "修订计划",
            "evidence_label": "证据",
            "prompt_version": "提示词版本",
            "mode": "模式",
            "rounds": "轮次",
            "stop": "停止原因",
            "reviewers": "审稿人",
            "usage": "用量",
            "calls": "调用",
            "tokens_in": "输入 token",
            "tokens_out": "输出 token",
            "known": "用量已知",
            "sha": "SHA-256",
            "run": "运行",
            "purpose": "目的",
            "domain": "领域",
            "page": "页码",
            "section": "章节",
            "quote_note": "引文 / 说明",
            "round": "轮次",
            "reviews": "审稿意见",
            "author_rebuttal": "作者回复",
            "questions": "问题",
            "missing_evidence": "缺失证据",
            "issue_updates": "问题状态更新",
            "new_issues": "新问题",
            "position_changes": "立场变化",
            "remaining_concerns": "剩余关切",
            "global_clarifications": "全局澄清",
            "unresolved": "未解决项",
            "no_rebuttal": "本轮没有作者回复。",
            "no_reviews": "本轮没有审稿意见。",
            "final_opinion": "最终审查意见",
            "decision": "决定",
            "consensus": "共识",
            "contested": "争议",
            "optional_suggestions": "可选建议",
            "limitations": "限制",
            "issue_summary": "问题统计",
            "reviewers_done": "完成审稿人数",
            "author": "作者",
            "claim": "主张",
            "why_it_matters": "重要性",
            "requested_action": "要求动作",
            "rounds_file": "多轮审稿意见",
            "final_opinion_file": "最终审查意见",
        }
    return {
        "report": "HPE v3 Review Report",
        "problem_report": "HPE v3 Problem-Finding Report",
        "disclaimer": "This report is model-assisted and is for research/writing reference only; it is not a peer-review decision. Verify every factual claim against the paper and the cited pages; the final academic judgment belongs to the human author/editor.",
        "summary": "Decision summary",
        "recommendation": "Recommendation",
        "policy": "Policy decision",
        "override": "Override",
        "provisional": "Provisional",
        "score": "Overall score",
        "confidence": "Confidence",
        "coverage": "Review coverage",
        "paper": "Paper",
        "scores": "Score matrix",
        "issues": "Issue ledger",
        "strengths": "Strengths",
        "highlights": "Innovation / technical highlights",
        "dissent": "Dissent and contested points",
        "revisions": "Required revisions",
        "warnings": "Warnings and limitations",
        "manifest": "Run manifest",
        "evidence": "Evidence index",
        "status": "Status",
        "severity": "Severity",
        "raised_by": "Raised by",
        "response": "Author response",
        "editor": "Editor rationale",
        "no_issues": "No issues reported.",
        "none": "None",
        "yes": "Yes",
        "no": "No",
        "title": "Title",
        "authors": "Authors",
        "year": "Year",
        "pages": "Pages",
        "language": "Paper language",
        "extraction": "Extraction",
        "profile": "Profile",
        "author_mode": "Author mode",
        "isolation": "Isolation",
        "reviewer": "Reviewer",
        "overall": "Overall",
        "novelty": "Novelty",
        "soundness": "Soundness",
        "evidence_strength": "Evidence",
        "repro": "Repro",
        "clarity": "Clarity",
        "significance": "Significance",
        "recommendation_col": "Recommendation",
        "mean": "Mean",
        "median": "Median",
        "min": "Min",
        "max": "Max",
        "flickering": "flickering",
        "revision_plan": "Revision plan",
        "evidence_label": "Evidence",
        "prompt_version": "Prompt version",
        "mode": "Mode",
        "rounds": "Rounds",
        "stop": "Stop",
        "reviewers": "Reviewers",
        "usage": "Usage",
        "calls": "calls",
        "tokens_in": "in",
        "tokens_out": "out",
        "known": "known",
        "sha": "SHA-256",
        "run": "Run",
        "purpose": "Purpose",
        "domain": "Domain",
        "page": "Page",
        "section": "Section",
        "quote_note": "Quote / note",
        "round": "Round",
        "reviews": "Reviews",
        "author_rebuttal": "Author rebuttal",
        "questions": "Questions",
        "missing_evidence": "Missing evidence",
        "issue_updates": "Issue updates",
        "new_issues": "New issues",
        "position_changes": "Position changes",
        "remaining_concerns": "Remaining concerns",
        "global_clarifications": "Global clarifications",
        "unresolved": "Unresolved",
        "no_rebuttal": "No author rebuttal was recorded for this round.",
        "no_reviews": "No reviews were recorded for this round.",
        "final_opinion": "Final review opinion",
        "decision": "Decision",
        "consensus": "Consensus",
        "contested": "Contested",
        "optional_suggestions": "Optional suggestions",
        "limitations": "Limitations",
        "issue_summary": "Issue summary",
        "reviewers_done": "Reviewers completed",
        "author": "Author",
        "claim": "Claim",
        "why_it_matters": "Why it matters",
        "requested_action": "Requested action",
        "rounds_file": "Multi-round reviews",
        "final_opinion_file": "Final review opinion",
    }


def _status_label(report: dict[str, Any], status: str) -> str:
    labels = STATUS_LABEL_ZH if output_language(report) == "zh" else STATUS_LABEL
    return labels.get(status, status)


def _escape_table(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(
    report: dict[str, Any],
    *,
    run: dict[str, Any],
    ledger: dict[str, Any],
    meta: dict[str, Any] | None,
    rebuttals: Sequence[dict[str, Any]],
    final_reviews: Sequence[dict[str, Any]],
) -> str:
    t = _t(report)
    paper = report.get("paper", {})
    decision = report.get("decision", {})
    scores = report.get("scores", {})
    issues = report.get("issues", [])
    alias_map = ledger_alias_map(ledger)
    responses = _author_responses(rebuttals, alias_map)
    meta_by_id = {str(item.get("issue_id")): item for item in (meta or {}).get("issue_resolution", [])}
    final_by_id = {str(review.get("reviewer_id")): review for review in final_reviews}

    lines: list[str] = []
    title_prefix = t["problem_report"] if report.get("purpose") == "problem_finding" else t["report"]
    lines.append(f"# {title_prefix}: {paper.get('title', 'Untitled')}")
    lines.append("")
    lines.append(
        f"> HPE v3 · {t['purpose']}={report.get('purpose', 'review')} · "
        f"{t['domain']}={report.get('domain', 'ai-ict')} · {t['run']} `{report.get('run_id')}` · {report.get('created_at')}"
    )
    lines.append(f"> {t['disclaimer']}")
    lines.append("")

    # Decision summary
    lines.append(f"## 0. {t['summary']}")
    lines.append("")
    lines.append(f"- **{t['recommendation']}**: `{decision.get('recommendation')}`")
    lines.append(f"- **{t['policy']}**: `{decision.get('policy_decision')}`")
    lines.append(f"- **{t['override']}**: {t['yes'] if decision.get('override') else t['no']}")
    if decision.get("override_reason"):
        lines.append(f"  - {decision.get('override_reason')}")
    lines.append(f"- **{t['provisional']}**: {t['yes'] if decision.get('provisional') else t['no']}")
    lines.append(f"- **{t['score']}**: {decision.get('overall_score')} / 10 (median {scores.get('median')})")
    lines.append(f"- **{t['confidence']}**: {decision.get('confidence')}")
    summary = report.get("issue_summary", {})
    lines.append(
        f"- **{t['issues']}**: blocking {summary.get('blocking_open', 0)} · major {summary.get('major_open', 0)} · "
        f"minor {summary.get('minor_open', 0)} · resolved {summary.get('resolved', 0)} · disputed {summary.get('disputed', 0)}"
    )
    coverage = report.get("coverage", {})
    lines.append(
        f"- **{t['coverage']}**: {coverage.get('reviewers_done', 0)}/{coverage.get('reviewers_expected', '?')}"
    )
    lines.append("")

    # Paper
    lines.append(f"## 1. {t['paper']}")
    lines.append("")
    lines.append(f"- {t['title']}: {paper.get('title')}")
    if paper.get("authors"):
        lines.append(f"- {t['authors']}: {', '.join(paper.get('authors', []))}")
    lines.append(f"- {t['year']}: {paper.get('year') if paper.get('year') is not None else t['none']}")
    lines.append(f"- {t['pages']}: {paper.get('pages')} · {t['language']}: {paper.get('language')}")
    lines.append(f"- {t['extraction']}: {paper.get('extraction_engine')} / {paper.get('extraction_quality')}")
    lines.append(f"- {t['sha']}: `{paper.get('sha256')}`")
    lines.append(
        f"- {t['profile']}: `{report.get('profile')}` · {t['author_mode']}: `{report.get('author_mode')}` · "
        f"{t['isolation']}: `{report.get('context_isolation')}`"
    )
    agent = report.get("agent") or {}
    if agent.get("label"):
        lines.append(f"- Agent backend: `{agent.get('label')}` ({agent.get('source', 'unknown')})")
    lines.append("")

    # Scores
    lines.append(f"## 2. {t['scores']}")
    lines.append("")
    lines.append(
        f"| {t['reviewer']} | {t['overall']} | {t['novelty']} | {t['soundness']} | {t['evidence_strength']} | "
        f"{t['repro']} | {t['clarity']} | {t['significance']} | {t['recommendation_col']} |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    matrix_by_id = {str(item.get("reviewer_id")): item for item in scores.get("matrix", [])}
    for reviewer in run.get("reviewers", []):
        rid = str(reviewer.get("id"))
        matrix = matrix_by_id.get(rid, {})
        final_review = final_by_id.get(rid) or {}
        score = final_review.get("score", {})
        lines.append(
            "| {label} ({persona}) | {overall} | {novelty} | {soundness} | {evidence} | {repro} | {clarity} | {significance} | {rec} |".format(
                label=reviewer.get("label", rid),
                persona=reviewer.get("persona", ""),
                overall=matrix.get("overall", score.get("overall", "—")),
                novelty=score.get("novelty", "—"),
                soundness=score.get("technical_soundness", "—"),
                evidence=score.get("evidence_strength", "—"),
                repro=score.get("reproducibility", "—"),
                clarity=score.get("clarity", "—"),
                significance=score.get("significance", "—"),
                rec=matrix.get("recommendation", "—"),
            )
        )
    lines.append("")
    lines.append(
        f"{t['mean']} {scores.get('mean')} · {t['median']} {scores.get('median')} · "
        f"{t['min']} {scores.get('min')} · {t['max']} {scores.get('max')}"
    )
    lines.append("")

    # Issues
    lines.append(f"## 3. {t['issues']}")
    lines.append("")
    if not issues:
        lines.append(t["no_issues"])
    else:
        for issue in issues:
            severity = str(issue.get("severity", ""))
            status = str(issue.get("status", ""))
            lines.append(f"### {issue.get('id')} · {issue.get('title')}")
            lines.append("")
            lines.append(
                f"- **{t['severity']}**: `{severity}` · **{t['status']}**: `{_status_label(report, status)}` · "
                f"**{t['raised_by']}**: {', '.join(issue.get('raised_by', [])) or t['none']}"
            )
            if issue.get("flickering"):
                lines.append(f"- **{t['flickering']}**: status changed back and forth across rounds")
            for evidence in issue.get("evidence", [])[:6]:
                page = evidence.get("page")
                section = evidence.get("section") or ""
                quote = evidence.get("quote") or evidence.get("note") or ""
                lines.append(
                    f"- {t['evidence_label']} ({evidence.get('kind')}, p.{page}{', ' + section if section else ''}): “{quote[:400]}”"
                )
            if issue.get("resolution_note"):
                lines.append(f"- {t['response']}: {issue.get('resolution_note')}")
            response = responses.get(str(issue.get("id")))
            if response:
                lines.append(f"- {t['response']} ({response.get('status')}): {str(response.get('response', ''))[:1200]}")
                if response.get("revision_plan"):
                    lines.append(f"  - {t['revision_plan']}: {response.get('revision_plan')}")
            meta_resolution = meta_by_id.get(str(issue.get("id")))
            if meta_resolution:
                lines.append(f"- {t['editor']}: {meta_resolution.get('rationale')}")
            lines.append("")

    # Strengths
    lines.append(f"## 4. {t['strengths']}")
    lines.append("")
    strengths: list[dict[str, Any]] = []
    seen_strengths: set[str] = set()
    for reviewer in run.get("reviewers", []):
        final_review = final_by_id.get(str(reviewer.get("id"))) or {}
        for strength in final_review.get("strengths", []) or []:
            claim = str(strength.get("claim", "")).strip()
            if claim and claim not in seen_strengths:
                seen_strengths.add(claim)
                strengths.append({"claim": claim, "importance": strength.get("importance"), "reviewer": reviewer.get("label")})
    if strengths:
        for item in strengths[:20]:
            lines.append(f"- ({item.get('importance')}, {item.get('reviewer')}) {item.get('claim')}")
    else:
        lines.append(t["none"])
    lines.append("")

    # Highlights
    highlights = report.get("highlights") or []
    if highlights:
        lines.append(f"## 4b. {t['highlights']}")
        lines.append("")
        for item in highlights[:12]:
            pages = ", ".join(str(e.get("page")) for e in item.get("evidence", []) if e.get("page")) or "-"
            lines.append(f"- ({item.get('kind')}, {item.get('reviewer_label')}, p.{pages}) {item.get('claim')}")
        lines.append("")

    # Dissent
    lines.append(f"## 5. {t['dissent']}")
    lines.append("")
    if report.get("dissent"):
        for item in report.get("dissent", []):
            lines.append(f"- {item.get('reviewer_id')}: {item.get('summary')} (severity: {item.get('severity')})")
    else:
        lines.append(t["none"])
    lines.append("")

    # Revisions
    lines.append(f"## 6. {t['revisions']}")
    lines.append("")
    if report.get("required_revisions"):
        for item in report.get("required_revisions", []):
            lines.append(f"- [ ] {item}")
    else:
        lines.append(t["none"])
    lines.append("")

    # Warnings
    lines.append(f"## 7. {t['warnings']}")
    lines.append("")
    if report.get("warnings"):
        for warning in report.get("warnings", []):
            lines.append(f"- `{warning.get('code')}` ({warning.get('severity')}): {warning.get('message')}")
    else:
        lines.append(t["none"])
    lines.append("")

    # Manifest
    lines.append(f"## 8. {t['manifest']}")
    lines.append("")
    runtime = report.get("runtime", {})
    lines.append(f"- {t['prompt_version']}: `{runtime.get('prompt_version')}`")
    lines.append(
        f"- {t['mode']}: `{runtime.get('mode')}` · {t['rounds']}: {runtime.get('rounds')} · "
        f"{t['stop']}: `{runtime.get('stop_reason')}`"
    )
    lines.append(f"- {t['author_mode']}: `{runtime.get('author_mode')}` · {t['isolation']}: `{runtime.get('isolation')}`")
    if runtime.get("agent_backend"):
        lines.append(f"- Agent backend: `{runtime.get('agent_backend')}`")
    lines.append(
        f"- {t['reviewers']}: "
        + ", ".join(f"{r.get('label')} ({r.get('persona')}, {r.get('model') or 'host'})" for r in runtime.get("reviewers", []))
    )
    usage = report.get("usage", {})
    lines.append(
        f"- {t['usage']}: {usage.get('calls')} {t['calls']} · {usage.get('tokens_in')} {t['tokens_in']} / "
        f"{usage.get('tokens_out')} {t['tokens_out']} · ${usage.get('cost_usd')} · {t['known']}={usage.get('known')}"
    )
    lines.append(f"- {t['rounds_file']}: `output/review-rounds.md`")
    lines.append(f"- {t['final_opinion_file']}: `output/final-opinion.md`")
    lines.append("")

    # Evidence index
    lines.append(f"## 9. {t['evidence']}")
    lines.append("")
    lines.append(f"| {t['issues']} | {t['page']} | {t['section']} | {t['quote_note']} |")
    lines.append("|---|---:|---|---|")
    for issue in issues:
        for evidence in issue.get("evidence", []):
            lines.append(
                "| {issue} | {page} | {section} | {quote} |".format(
                    issue=_escape_table(issue.get("id")),
                    page=_escape_table(evidence.get("page")),
                    section=_escape_table(evidence.get("section", "")),
                    quote=_escape_table(evidence.get("quote") or evidence.get("note") or ""),
                )
            )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _markdown_evidence(evidence: dict[str, Any]) -> str:
    kind = evidence.get("kind")
    page = evidence.get("page")
    page_end = evidence.get("page_end")
    section = evidence.get("section") or ""
    quote = str(evidence.get("quote") or evidence.get("note") or "")
    pages = f"p.{page}" if page == page_end or page_end is None else f"pp.{page}-{page_end}"
    where = f"{pages}, {section}" if section else pages
    return f"- `{kind}` ({where}): “{quote[:360]}”"


def _render_review_block(review: dict[str, Any], t: dict[str, str]) -> list[str]:
    lines: list[str] = []
    reviewer_id = review.get("reviewer_id") or "?"
    label = review.get("reviewer_label") or reviewer_id
    persona = review.get("persona") or "?"
    lines.append(f"### {reviewer_id} · {label} ({persona})")
    lines.append("")
    lines.append(f"- **{t['recommendation']}**: `{review.get('recommendation')}`")
    score = review.get("score") or {}
    if score:
        dims = ", ".join(
            f"{key}={score.get(key)}"
            for key in ("overall", "novelty", "technical_soundness", "evidence_strength", "reproducibility", "clarity", "significance")
            if score.get(key) is not None
        )
        lines.append(f"- **{t['score']}**: {dims}")
    lines.append(f"- **{t['confidence']}**: {review.get('confidence')}")
    summary = str(review.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append(f"**{t['summary']}**: {summary}")
    strengths = review.get("strengths") or []
    if strengths:
        lines.append("")
        lines.append(f"#### {t['strengths']}")
        for item in strengths:
            lines.append(f"- {item.get('claim')} ({item.get('importance', '-')})")
            for evidence in (item.get("evidence") or [])[:2]:
                lines.append("  " + _markdown_evidence(evidence))
    highlights = review.get("highlights") or []
    if highlights:
        lines.append("")
        lines.append(f"#### {t['highlights']}")
        for item in highlights:
            lines.append(f"- [{item.get('kind')}] {item.get('claim')}")
            for evidence in (item.get("evidence") or [])[:2]:
                lines.append("  " + _markdown_evidence(evidence))
    issues = list(review.get("issues") or [])
    if issues:
        lines.append("")
        lines.append(f"#### {t['issues']}")
        for issue in issues:
            lines.append(f"- **{issue.get('id')} · {issue.get('title')}** — `{issue.get('severity')}` / `{issue.get('status', 'open')}`")
            if issue.get("claim"):
                lines.append(f"  - {t['claim']}: {issue.get('claim')}")
            if issue.get("why_it_matters"):
                lines.append(f"  - {t['why_it_matters']}: {issue.get('why_it_matters')}")
            if issue.get("requested_action"):
                lines.append(f"  - {t['requested_action']}: {issue.get('requested_action')}")
            for evidence in (issue.get("evidence") or [])[:4]:
                lines.append("  " + _markdown_evidence(evidence))
    updates = review.get("issue_updates") or []
    if updates:
        lines.append("")
        lines.append(f"#### {t['issue_updates']}")
        for update in updates:
            lines.append(f"- `{update.get('issue_id')}` → `{update.get('status')}`: {update.get('rationale', '')}")
            for evidence in (update.get("evidence") or [])[:2]:
                lines.append("  " + _markdown_evidence(evidence))
    new_issues = review.get("new_issues") or []
    if new_issues:
        lines.append("")
        lines.append(f"#### {t['new_issues']}")
        for issue in new_issues:
            lines.append(f"- **{issue.get('id')} · {issue.get('title')}** — `{issue.get('severity')}`")
            if issue.get("why_new"):
                lines.append(f"  - why_new: {issue.get('why_new')}")
            for evidence in (issue.get("evidence") or [])[:3]:
                lines.append("  " + _markdown_evidence(evidence))
    changes = review.get("position_changes") or []
    if changes:
        lines.append("")
        lines.append(f"#### {t['position_changes']}")
        for change in changes:
            lines.append(f"- `{change.get('dimension')}`: {change.get('from')} → {change.get('to')} — {change.get('reason', '')}")
    remaining = review.get("remaining_concerns") or []
    if remaining:
        lines.append("")
        lines.append(f"#### {t['remaining_concerns']}")
        for item in remaining:
            lines.append(f"- {item}")
    for key in ("questions", "missing_evidence"):
        values = review.get(key) or []
        if values:
            lines.append("")
            lines.append(f"#### {t[key]}")
            for item in values:
                lines.append(f"- {item}")
    coverage = review.get("coverage") or {}
    if coverage:
        lines.append("")
        lines.append(f"#### {t['coverage']}")
        lines.append(
            f"- sections_read={coverage.get('sections_read', [])}; truncated={coverage.get('truncated')}; "
            f"omitted={coverage.get('omitted_sections', [])}"
        )
    lines.append("")
    return lines


def _render_rebuttal_block(rebuttal: dict[str, Any], t: dict[str, str], round_number: int) -> list[str]:
    lines = [f"## {t['round']} {round_number} · {t['author_rebuttal']}", ""]
    lines.append(f"- {t['author']}: `{rebuttal.get('author_id')}` · round `{rebuttal.get('round')}`")
    responses = rebuttal.get("responses") or []
    if responses:
        lines.append(f"### {t['response']}")
    for response in responses:
        lines.append(f"#### `{response.get('issue_id')}` · `{response.get('status')}`")
        if response.get("response"):
            lines.append(f"- {t['response']}: {response.get('response')}")
        for evidence in (response.get("paper_evidence") or [])[:3]:
            lines.append("  " + _markdown_evidence(evidence))
        if response.get("revision_plan"):
            lines.append(f"- {t['revision_plan']}: {response.get('revision_plan')}")
        lines.append(f"- {t['confidence']}: {response.get('confidence')}")
        lines.append("")
    clarifications = rebuttal.get("global_clarifications") or []
    if clarifications:
        lines.append(f"### {t['global_clarifications']}")
        for item in clarifications:
            lines.append(f"- {item.get('clarification') or item}")
            for evidence in (item.get("paper_evidence") or [])[:2] if isinstance(item, dict) else []:
                lines.append("  " + _markdown_evidence(evidence))
        lines.append("")
    plan = rebuttal.get("revision_plan") or []
    if plan:
        lines.append(f"### {t['revision_plan']}")
        for item in plan:
            lines.append(f"- `{item.get('issue_id')}`: {item.get('action')} ({item.get('location', '-')}, {item.get('effort', '-')})")
        lines.append("")
    unresolved = rebuttal.get("unresolved") or []
    if unresolved:
        lines.append(f"### {t['unresolved']}")
        for item in unresolved:
            lines.append(f"- {item}")
        lines.append("")
    return lines


def render_rounds_markdown(report: dict[str, Any]) -> str:
    t = _t(report)
    paper = report.get("paper") or {}
    lines = [f"# {t['reviews']} — {paper.get('title', 'Untitled')}", ""]
    lines.append(
        f"> HPE v3 · {t['run']} `{report.get('run_id')}` · {t['round']} 0–{report.get('final_opinion', {}).get('rounds_completed', 0)} · "
        f"{report.get('output_language', 'en')}"
    )
    lines.append("")
    rounds = report.get("review_rounds") or []
    if not rounds:
        lines.append(t["no_reviews"])
    for round_block in rounds:
        round_number = round_block.get("round")
        lines.append(f"## {t['round']} {round_number}")
        lines.append("")
        reviews = round_block.get("reviews") or []
        if not reviews:
            lines.append(t["no_reviews"])
        for review in reviews:
            lines.extend(_render_review_block(review, t))
    lines.append(f"# {t['author_rebuttal']}")
    lines.append("")
    rebuttals = report.get("rebuttal_rounds") or []
    if not rebuttals:
        lines.append(t["no_rebuttal"])
        lines.append("")
    for block in rebuttals:
        lines.extend(_render_rebuttal_block(block.get("rebuttal") or {}, t, int(block.get("round", 0))))
    return "\n".join(lines).rstrip() + "\n"


def render_final_opinion_markdown(report: dict[str, Any]) -> str:
    t = _t(report)
    paper = report.get("paper") or {}
    opinion = report.get("final_opinion") or {}
    decision = report.get("decision") or {}
    lines = [f"# {t['final_opinion']} — {paper.get('title', 'Untitled')}", ""]
    lines.append(f"> HPE v3 · {t['run']} `{report.get('run_id')}` · {report.get('created_at')}")
    lines.append(f"> {t['disclaimer']}")
    lines.append("")
    lines.append(f"## 0. {t['summary']}")
    lines.append("")
    lines.append(f"- **{t['recommendation']}**: `{decision.get('recommendation')}`")
    lines.append(f"- **{t['policy']}**: `{decision.get('policy_decision')}`")
    lines.append(f"- **{t['override']}**: {t['yes'] if decision.get('override') else t['no']}")
    lines.append(f"- **{t['provisional']}**: {t['yes'] if decision.get('provisional') else t['no']}")
    lines.append(f"- **{t['score']}**: {decision.get('overall_score')} / 10")
    lines.append(f"- **{t['confidence']}**: {decision.get('confidence')}")
    coverage = report.get("coverage") or {}
    lines.append(f"- **{t['coverage']}**: {coverage.get('reviewers_done', 0)}/{coverage.get('reviewers_expected', '?')}")
    summary = report.get("issue_summary") or {}
    lines.append(
        f"- **{t['issue_summary']}**: blocking {summary.get('blocking_open', 0)} · major {summary.get('major_open', 0)} · "
        f"minor {summary.get('minor_open', 0)} · resolved {summary.get('resolved', 0)} · disputed {summary.get('disputed', 0)}"
    )
    lines.append("")
    section = 1
    consensus = opinion.get("consensus") or []
    if consensus:
        lines.append(f"## {section}. {t['consensus']}")
        section += 1
        lines.append("")
        lines.extend(f"- {item}" for item in consensus)
        lines.append("")
    revisions = opinion.get("required_revisions") or report.get("required_revisions") or []
    lines.append(f"## {section}. {t['revisions']}")
    section += 1
    lines.append("")
    if revisions:
        lines.extend(f"- [ ] {item}" for item in revisions)
    else:
        lines.append(t["none"])
    lines.append("")
    suggestions = opinion.get("optional_suggestions") or []
    if suggestions:
        lines.append(f"## {section}. {t['optional_suggestions']}")
        section += 1
        lines.append("")
        lines.extend(f"- {item}" for item in suggestions)
        lines.append("")
    contested = opinion.get("contested") or []
    dissent = opinion.get("dissent") or report.get("dissent") or []
    if contested or dissent:
        lines.append(f"## {section}. {t['contested']}")
        section += 1
        lines.append("")
        for item in contested:
            lines.append(f"- `{item.get('issue_id')}`: {item.get('editor_rationale', '')}")
            for position in item.get("positions") or []:
                lines.append(f"  - {position.get('reviewer_id')}: {position.get('view')} — {position.get('reason', '')}")
        for item in dissent:
            lines.append(f"- dissent `{item.get('reviewer_id')}`: {item.get('summary')} ({item.get('severity')})")
        lines.append("")
    limitations = opinion.get("limitations") or []
    if limitations:
        lines.append(f"## {section}. {t['limitations']}")
        lines.append("")
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_report_files(
    *,
    run_dir: Path,
    run: dict[str, Any],
    state: Any,
    manifest: dict[str, Any],
    ledger: dict[str, Any],
    meta: dict[str, Any] | None,
    final_reviews: Sequence[dict[str, Any]],
    rebuttals: Sequence[dict[str, Any]],
    usage: dict[str, Any],
    formats: Iterable[str] = ("md", "json"),
) -> dict[str, str]:
    report = build_report(
        run_dir=run_dir,
        run=run,
        state=state,
        manifest=manifest,
        ledger=ledger,
        meta=meta,
        final_reviews=final_reviews,
        rebuttals=rebuttals,
        usage=usage,
    )
    markdown = render_markdown(
        report,
        run=run,
        ledger=ledger,
        meta=meta,
        rebuttals=rebuttals,
        final_reviews=final_reviews,
    )
    rounds_markdown = render_rounds_markdown(report)
    final_opinion_markdown = render_final_opinion_markdown(report)
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    formats = set(formats)
    if "md" in formats:
        atomic_write_text(output_dir / "report.md", markdown)
        atomic_write_text(output_dir / "review-rounds.md", rounds_markdown)
        atomic_write_text(output_dir / "final-opinion.md", final_opinion_markdown)
        written["md"] = "output/report.md"
        written["review_rounds_md"] = "output/review-rounds.md"
        written["final_opinion_md"] = "output/final-opinion.md"
    if "json" in formats:
        report["artifacts"]["report_md"] = written.get("md")
        report["artifacts"]["report_json"] = "output/report.json"
        report["artifacts"]["review_rounds_md"] = written.get("review_rounds_md")
        report["artifacts"]["final_opinion_md"] = written.get("final_opinion_md")
        atomic_write_json(output_dir / "report.json", report)
        written["json"] = "output/report.json"
    if "html" in formats:
        import html as html_lib

        body = "".join(f"<p>{html_lib.escape(line)}</p>" for line in markdown.splitlines())
        atomic_write_text(output_dir / "report.html", f"<!doctype html><meta charset='utf-8'><title>HPE report</title>{body}")
        written["html"] = "output/report.html"
    return written


def _summary_highlights(report: dict[str, Any]) -> str:
    zh = output_language(report) == "zh"
    rows = []
    for item in report.get("highlights", []) or []:
        evidence = item.get("evidence") or []
        pages = ", ".join(str(e.get("page")) for e in evidence if e.get("page")) or "-"
        rows.append(
            f"| {_escape_table(item.get('id'))} | {_escape_table(item.get('kind'))} | "
            f"{_escape_table(item.get('claim'))} | {pages} |"
        )
    if not rows:
        return ("未报告可辩护的创新/技术亮点。\n" if zh else "No defensible innovation/technical highlight was reported.\n")
    header = "| ID | 类型 | 主张 | 页码 |\n|---|---|---|---|\n" if zh else "| ID | Kind | Claim | Page(s) |\n|---|---|---|---|\n"
    return header + "\n".join(rows) + "\n"


def build_summary_report(
    *,
    run_dir: Path,
    run: dict[str, Any],
    state: Any,
    manifest: dict[str, Any],
    usage: dict[str, Any],
) -> dict[str, Any]:
    summary = _load(run_dir / "summary" / "summary.json") or {}
    warnings = list(state.warnings)
    if not summary:
        warnings.append({"code": "summary.missing", "message": "summary/summary.json is missing", "severity": "error"})
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.get("run_id"),
        "created_at": utc_now_iso(),
        "output_language": run.get("output_language")
        or ("zh" if str((manifest.get("language") or "en")).startswith("zh") else "en"),
        "purpose": "summary",
        "material": run.get("material", "paper"),
        "domain": run.get("domain", "ai-ict"),
        "domain_label": run.get("domain_label", run.get("domain", "ai-ict")),
        "paper": {
            "paper_id": manifest.get("paper_id"),
            "title": manifest.get("title", "Untitled"),
            "authors": manifest.get("authors", []),
            "year": manifest.get("year"),
            "pages": int(manifest.get("source", {}).get("pages", 1)),
            "sha256": manifest.get("source", {}).get("sha256", ""),
            "extraction_quality": manifest.get("extraction", {}).get("quality"),
        },
        "summary_markdown": summary.get("summary_markdown", ""),
        "highlights": summary.get("highlights", []),
        "open_questions": summary.get("open_questions", []),
        "missing_information": summary.get("missing_information", []),
        "limitations": summary.get("limitations", []),
        "images_read": summary.get("images_read", []),
        "confidence": summary.get("confidence", 0.0),
        "agent": run.get("agent"),
        "vision": run.get("vision"),
        "usage": usage,
        "warnings": warnings,
        "artifacts": {"manifest": "paper/manifest.json", "digest": "digest/digest.json" if (run_dir / "digest" / "digest.json").is_file() else None},
    }


def render_summary_markdown(report: dict[str, Any]) -> str:
    paper = report.get("paper") or {}
    zh = output_language(report) == "zh"
    labels = (
        {
            "title": "标题",
            "material": "材料类型",
            "domain": "领域",
            "pages": "页数",
            "confidence": "置信度",
            "images_read": "已查看页面图",
            "usage": "用量",
            "summary": "总结",
            "highlights": "创新 / 技术亮点",
            "questions": "待回答问题",
            "missing": "缺失信息",
            "limitations": "本总结的局限",
            "warnings": "警告",
            "none": "无。",
            "calls": "次调用",
            "known": "用量已知",
            "no_summary": "_未生成总结。_",
        }
        if zh
        else {
            "title": "Title",
            "material": "Material",
            "domain": "Domain",
            "pages": "Pages",
            "confidence": "Confidence",
            "images_read": "Page images inspected",
            "usage": "Usage",
            "summary": "Summary",
            "highlights": "Innovation / technical highlights",
            "questions": "Open questions",
            "missing": "Missing information",
            "limitations": "Limitations of this summary",
            "warnings": "Warnings",
            "none": "None.",
            "calls": "calls",
            "known": "known",
            "no_summary": "_No summary produced._",
        }
    )
    lines = [
        f"# {'HPE 详细总结' if zh else 'HPE Detailed Summary'}",
        "",
        f"- **{labels['title']}:** {paper.get('title')}",
        f"- **{labels['material']}:** {report.get('material')}",
        f"- **{labels['domain']}:** {report.get('domain_label')} (`{report.get('domain')}`)",
        f"- **{labels['pages']}:** {paper.get('pages')}",
        f"- **{labels['confidence']}:** {report.get('confidence')}",
        f"- **{labels['images_read']}:** {report.get('images_read', [])}",
        f"- **{labels['usage']}:** {report.get('usage', {}).get('calls', 0)} {labels['calls']} · "
        f"{labels['known']}={report.get('usage', {}).get('known')}",
        "",
        f"## {labels['summary']}",
        "",
        report.get("summary_markdown", labels["no_summary"]),
        "",
        f"## {labels['highlights']}",
        "",
        _summary_highlights(report),
        f"## {labels['questions']}",
        "",
        "\n".join(f"- {item}" for item in report.get("open_questions", [])) or f"- {labels['none']}",
        "",
        f"## {labels['missing']}",
        "",
        "\n".join(f"- {item}" for item in report.get("missing_information", [])) or f"- {labels['none']}",
        "",
        f"## {labels['limitations']}",
        "",
        "\n".join(f"- {item}" for item in report.get("limitations", [])) or f"- {labels['none']}",
    ]
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", f"## {labels['warnings']}", ""])
        lines.extend(f"- `{item.get('code')}`: {item.get('message')}" for item in warnings)
    return "\n".join(lines).rstrip() + "\n"


def render_summary_files(
    *,
    run_dir: Path,
    run: dict[str, Any],
    state: Any,
    manifest: dict[str, Any],
    usage: dict[str, Any],
    formats: Iterable[str] = ("md", "json"),
) -> dict[str, str]:
    report = build_summary_report(run_dir=run_dir, run=run, state=state, manifest=manifest, usage=usage)
    markdown = render_summary_markdown(report)
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    formats = set(formats)
    if "md" in formats:
        atomic_write_text(output_dir / "summary.md", markdown)
        atomic_write_text(output_dir / "report.md", markdown)
        written["md"] = "output/summary.md"
        written["report_md"] = "output/report.md"
    if "json" in formats:
        atomic_write_json(output_dir / "summary.json", report)
        atomic_write_json(output_dir / "report.json", report)
        written["json"] = "output/summary.json"
        written["report_json"] = "output/report.json"
    return written
