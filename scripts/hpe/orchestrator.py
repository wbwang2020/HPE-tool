"""Deterministic orchestrator: init, task packets, submit, advance, render, API loop."""

from __future__ import annotations

import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import (
    MAX_REVIEWERS,
    MAX_ROUNDS,
    PROMPT_VERSION,
    SCHEMAS_DIR,
    SCHEMA_VERSION,
    RunOptions,
    get_profile,
    get_persona,
)
from .errors import (
    BudgetExceeded,
    ConfigError,
    CoverageError,
    IngestError,
    StateError,
    ValidationError,
)
from .bilingual import check_bilingual_files, prepare_bilingual_files
from .domains import detect_domain, get_domain
from .events import EventLog, UsageTracker
from .ingest import QuoteIndex, ingest_pdf
from .llm_config import backend_vendor, detect_agent_identity, load_llm_config, resolve_backends
from .ledger import apply_meta_resolutions, build_ledger, ledger_open_issue_ids
from .policy import aggregate_scores, policy_bundle, policy_decision
from .prompts import (
    build_prompt_for_task,
    ledger_path,
    load_final_reviews,
    load_json,
    load_reviews,
    rebuttal_path,
    resolve_output_language,
    review_path,
    summary_prompt_role,
    write_prompt_files,
)
from .render import render_report_files, render_summary_files
from .state import (
    RunLock,
    RunState,
    Task,
    TERMINAL_STATUSES,
    load_run,
    load_state,
    new_state,
    save_run,
    save_state,
)
from .util import (
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    read_json,
    scan_injection,
    sha256_file,
    utc_now_iso,
)
from .validate import (
    parse_json_output,
    policy_from_summary,
    validate_artifact,
    validate_schema,
)


LLM_KINDS = ("digest", "reviewer_report", "author_rebuttal", "meta_review", "summary_report", "legacy_summary")
STAGE_FOR_TASK = {
    "digesting": "digest",
    "reviewing": "review",
    "rebutting": "rebuttal",
    "updating": "update",
    "meta": "meta",
    "summary": "summary",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_run_id(paper_sha: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{paper_sha[:6]}-{secrets.token_hex(2)}"


def _rel(run_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def _copy_schemas(run_dir: Path) -> None:
    target = ensure_dir(run_dir / "schemas")
    for path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        shutil.copy2(path, target / path.name)


def _reviewer_specs(options: RunOptions, profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw = options.reviewers or [
        {"persona": persona} for persona in profile.get("default_reviewers", ["methodology", "experiments", "novelty"])
    ]
    reviewers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"persona": item}
        if not isinstance(item, dict):
            raise ConfigError("reviewers must be persona names or objects")
        persona = str(item.get("persona", ""))
        get_persona(persona)  # validates
        reviewer_id = str(item.get("id") or f"R{index}")
        if not re.fullmatch(r"R[0-9]{1,2}", reviewer_id):
            raise ConfigError(f"invalid reviewer id {reviewer_id!r}; use R1..R{MAX_REVIEWERS}")
        if reviewer_id in seen_ids:
            raise ConfigError(f"duplicate reviewer id {reviewer_id!r}")
        seen_ids.add(reviewer_id)
        reviewers.append(
            {
                "id": reviewer_id,
                "persona": persona,
                "label": str(item.get("label") or f"Reviewer {chr(ord('A') + index - 1)}"),
                "provider": item.get("provider") or options.provider,
                "model": item.get("model") or options.model,
                "temperature": float(item.get("temperature", 0.3)),
                "weight": float(item.get("weight", 1.0)),
            }
        )
    if not reviewers:
        raise ConfigError("at least one reviewer is required")
    if len(reviewers) > MAX_REVIEWERS:
        raise ConfigError(f"at most {MAX_REVIEWERS} reviewers are supported")
    return reviewers


def _executor_for_backend(label: str | None) -> str:
    return "api" if label and label != "host" else "host"


def _task_stage_name(state_stage: str) -> str | None:
    return STAGE_FOR_TASK.get(state_stage)


def _find_task(state: RunState, *, kind: str, actor: str | None = None, round_number: int | None = None) -> Task | None:
    for task in state.tasks.values():
        if task.kind != kind:
            continue
        if actor is not None and task.actor_id != actor:
            continue
        if round_number is not None and task.round != round_number:
            continue
        return task
    return None


def _add_task(
    state: RunState,
    *,
    kind: str,
    stage: str,
    round_number: int,
    actor_id: str,
    persona: str,
    label: str,
    output: str,
    schema_name: str,
    prompt_role: str,
    depends_on: Sequence[str] = (),
    optional_depends_on: Sequence[str] = (),
    executor: str = "host",
    purpose: str = "review",
) -> Task:
    task = Task(
        task_id=state.next_task_id(),
        kind=kind,
        stage=stage,
        round=round_number,
        actor_id=actor_id,
        persona=persona,
        label=label,
        output=output,
        schema_name=schema_name,
        prompt_role=prompt_role,
        executor=executor,
        purpose=purpose,
        depends_on=list(depends_on),
        optional_depends_on=list(optional_depends_on),
    )
    return state.add_task(task)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def init_run(
    *,
    pdf_path: Path,
    out_dir: Path,
    options: RunOptions,
    run_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    pdf_path = pdf_path.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    if not pdf_path.is_file():
        raise IngestError(f"PDF not found: {pdf_path}")
    if options.rounds < 0 or options.rounds > MAX_ROUNDS:
        raise ConfigError(f"rounds must be between 0 and {MAX_ROUNDS}")
    if options.mode not in {"host", "api", "hybrid", "legacy"}:
        raise ConfigError(f"unknown mode {options.mode!r}")
    if options.purpose not in {"review", "summary", "problem_finding"}:
        raise ConfigError(f"unknown purpose {options.purpose!r}")
    if options.material not in {"paper", "proposal", "design"}:
        raise ConfigError(f"unknown material {options.material!r}")

    if out_dir.exists() and not out_dir.is_dir():
        raise ConfigError(f"--out is a file, not a directory: {out_dir}")
    if out_dir.exists() and any((out_dir / name).exists() for name in ("run.json", "state.json", "paper")):
        raise ConfigError(
            f"Run directory already contains an HPE run: {out_dir}. Choose a new --out; overwriting an existing run is refused."
        )
    ensure_dir(out_dir)
    for name in ("paper", "digest", "summary", "prompts", "artifacts", "artifacts/reviews", "artifacts/rebuttals", "artifacts/evidence", "logs", "cache", "output", "schemas"):
        ensure_dir(out_dir / name)
    _copy_schemas(out_dir)

    run_id = run_id or _now_run_id(sha256_file(pdf_path))
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", run_id):
        raise ConfigError("run_id must match [A-Za-z0-9._-]{3,80}")
    paper = ingest_pdf(
        pdf_path,
        out_dir,
        paper_id=None,
        allow_poor=options.allow_poor_extraction,
        ocr=options.ocr,
        blind=options.blind_paper,
        vision_pages=options.vision_pages,
        vision_max_pages=options.vision_max_pages,
        vision_dpi=options.vision_dpi,
    )
    domain_id = options.domain
    domain_auto = domain_id == "auto"
    if domain_auto:
        domain_id = detect_domain(f"{paper.title}\n{paper.full_text[:60000]}")
        options.domain = domain_id
    domain_spec = get_domain(domain_id)
    if not options.reviewers:
        options.reviewers = [{"persona": persona} for persona in (domain_spec.default_reviewers or get_profile(options.profile).get("default_reviewers", []))]
    profile = get_profile(options.profile)
    reviewers = _reviewer_specs(options, profile)
    llm_config = load_llm_config(options.llm_config, required=bool(options.llm_config))
    agent_identity = detect_agent_identity(options.agent_backend)
    if options.mode == "host" and agent_identity.provider and agent_identity.model:
        # In host mode the detected/explicit model is the host agent itself,
        # not an independently callable API backend.
        agent_identity.kind = "host"
    backends = resolve_backends(
        config=llm_config,
        mode=options.mode,
        default_provider=options.provider,
        default_model=options.model,
        reviewer_backends=options.reviewer_backends,
        author_backend=options.author_backend,
        digest_backend=options.digest_backend,
        meta_backend=options.meta_backend,
        reviewer_count=len(reviewers),
        agent_identity=agent_identity,
        allow_backend_mismatch=options.allow_backend_mismatch,
    )
    for reviewer, backend in zip(reviewers, backends["reviewers"]):
        reviewer["backend"] = backend.label
        reviewer["provider"] = backend.provider or (agent_identity.provider if backend.is_host else None) or reviewer.get("provider")
        reviewer["model"] = backend.model or (agent_identity.model if backend.is_host else None) or reviewer.get("model")
    author_backend = backends["author"]
    digest_backend = backends["digest"]
    meta_backend = backends["meta"]
    output_language = resolve_output_language({"language": options.language}, paper.manifest)
    run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "paper": {
            "path": str(pdf_path),
            "sha256": paper.manifest["source"]["sha256"],
            "title": paper.title,
            "language": paper.manifest.get("language", "en"),
            "pages": paper.page_count,
        },
        "profile": options.profile,
        "mode": options.mode,
        "purpose": options.purpose,
        "material": options.material,
        "domain": domain_id,
        "domain_label": domain_spec.label,
        "domain_auto": domain_auto,
        "summary_style": options.summary_style,
        "provider": options.provider,
        "model": options.model,
        "llm_config": str(llm_config.path) if llm_config.path else None,
        "agent": agent_identity.to_dict(),
        "backend_consistency": backends.get("consistency", {}),
        "backends": {
            "digest": digest_backend.to_dict(),
            "reviewers": [backend.to_dict() for backend in backends["reviewers"]],
            "author": author_backend.to_dict(),
            "meta": meta_backend.to_dict(),
        },
        "reviewers": reviewers,
        "author": {
            "id": options.author_id,
            "mode": options.author_mode,
            "provider": options.author_provider
            or options.provider
            or (agent_identity.provider if author_backend.is_host else None),
            "model": options.author_model
            or options.model
            or (agent_identity.model if author_backend.is_host else None),
            "rebuttal_file": options.provided_rebuttal,
            "temperature": 0.3,
            "backend": author_backend.label,
        },
        "rounds": options.rounds,
        "language": options.language,
        "output_language": output_language,
        "vision": {
            "mode": options.vision_pages,
            "max_pages": options.vision_max_pages,
            "dpi": options.vision_dpi,
            "page_images": paper.manifest.get("vision", {}).get("page_images", []),
        },
        "host": {"isolation": options.isolation, "parallel_tasks": 4},
        "output": {"dir": str(out_dir), "formats": ["md", "json"], "compat_path": False},
        "budget": {
            "max_input_tokens": options.budget.max_input_tokens,
            "max_output_tokens": options.budget.max_output_tokens,
            "max_cost_usd": options.budget.max_cost_usd,
            "max_wall_seconds": options.budget.max_wall_seconds,
        },
        "context": {
            "max_paper_tokens": options.context.max_paper_tokens,
            "max_output_tokens_per_call": options.context.max_output_tokens_per_call,
            "max_prior_tokens": options.context.max_prior_tokens,
            "max_digest_tokens": options.context.max_digest_tokens,
            "include_digest": options.context.include_digest,
            "include_references": options.context.include_references,
            "allow_truncation": options.context.allow_truncation,
        },
        "options": {
            "strict_evidence": options.strict_evidence,
            "anonymize": True,
            "early_stop": options.early_stop,
            "blind_paper": options.blind_paper,
            "prompt_set": options.prompt_set,
        },
        "prompt_set": options.prompt_set,
    }
    state = new_state(run_id, max_rounds=options.rounds, isolation=options.isolation)
    state.stage = "digesting" if options.digest else ("summary" if options.purpose == "summary" else "reviewing")
    consistency = backends.get("consistency", {})
    if agent_identity.kind == "unknown":
        state.add_warning(
            "backend.agent_unknown",
            "Host-agent model could not be detected. Host mode still uses the current agent; "
            "for API mode pass --agent-backend provider:model so the review base is explicit.",
            severity="info",
        )
    if consistency.get("mismatches"):
        state.add_warning(
            "backend.mismatch",
            "A role uses a different model of the same provider as the agent: "
            + ", ".join(f"{item['role']}={item['role_backend']}" for item in consistency["mismatches"]),
        )
    for item in consistency.get("cross_provider", []):
        state.add_warning(
            "backend.cross_provider",
            f"{item['role']} uses {item['role_backend']} while the agent is {item['agent_backend']}; "
            "the role no longer runs on the agent base.",
            severity="info",
        )
    for warning in paper.manifest.get("extraction", {}).get("warnings", []) or []:
        state.add_warning("extraction", str(warning))
    if paper.quality.get("quality") != "good":
        state.add_warning(
            "extraction.quality",
            f"Extraction quality is {paper.quality.get('quality')}; reviewers must lower confidence for unreadable content.",
        )
    injection = scan_injection(paper.full_text)
    if injection:
        state.add_warning(
            "paper.injection",
            f"Possible prompt-injection text in the paper: {injection[0][:200]}",
        )
    if options.isolation == "session":
        state.add_warning(
            "isolation.shared",
            "Host isolation is 'session': reviewers may share context, which weakens blind-review independence. "
            "Use one fresh subagent per task when possible.",
        )
    if options.author_mode not in {"simulated", "provided", "none"}:
        raise ConfigError(f"unknown author mode {options.author_mode!r}")
    if options.author_mode == "provided" and options.rounds > 1:
        state.add_warning(
            "author.provided_rounds",
            "A single provided rebuttal file only covers round 1; later rounds will be treated as author mode 'none'.",
        )

    # Ensure the run_id appears in state and run.
    save_run(out_dir, run)
    if options.digest:
        _add_task(
            state,
            kind="digest",
            stage="digest",
            round_number=0,
            actor_id="D1",
            persona="digest",
            label="Digest",
            output="digest/digest.json",
            schema_name="digest.schema.json",
            prompt_role="digest",
            executor="api" if not digest_backend.is_host else "host",
            purpose=options.purpose,
        )
    elif options.purpose == "summary":
        create_summary_task(out_dir, state, run)
    else:
        create_review_tasks(out_dir, state, run, round_number=0)
    save_state(out_dir, state)
    event = EventLog(out_dir)
    event.emit("run.created", run_id=run_id, stage=state.stage, paper_sha256=run["paper"]["sha256"], profile=options.profile)
    event.emit("paper.ingested", engine=paper.manifest["extraction"].get("engine"), quality=paper.manifest["extraction"].get("quality"), pages=paper.page_count)
    for warning in state.warnings:
        event.warning(code=str(warning.get("code")), message=str(warning.get("message")))
    return {
        "run_id": run_id,
        "run_dir": str(out_dir),
        "stage": state.stage,
        "round": state.round,
        "output_language": output_language,
        "agent": agent_identity.to_dict(),
        "reviewers": reviewers,
        "warnings": state.warnings,
        "next": "run `hpe.py next --run <run_dir> --json` to get the first task",
    }


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------


def create_summary_task(run_dir: Path, state: RunState, run: dict[str, Any]) -> Task:
    existing = _find_task(state, kind="summary_report")
    if existing:
        return existing
    material = str(run.get("material", "paper"))
    purpose = str(run.get("purpose", "summary"))
    digest_backend = (run.get("backends") or {}).get("digest") or {}
    executor = "api" if digest_backend.get("kind") == "api" else "host"
    digest_task = _find_task(state, kind="digest")
    task = _add_task(
        state,
        kind="summary_report",
        stage="summary",
        round_number=0,
        actor_id="S1",
        persona="summary",
        label="Summary",
        output="summary/summary.json",
        schema_name="summary_report.schema.json",
        prompt_role=summary_prompt_role(material),
        optional_depends_on=[digest_task.task_id] if digest_task else [],
        executor=executor,
        purpose=purpose,
    )
    return task


def create_review_tasks(run_dir: Path, state: RunState, run: dict[str, Any], *, round_number: int) -> list[Task]:
    existing = state.tasks_for(kind="reviewer_report", round=round_number)
    if existing:
        return existing
    digest_task = _find_task(state, kind="digest")
    rebuttal_task = _find_task(state, kind="author_rebuttal", round_number=round_number)
    created: list[Task] = []
    for reviewer in run.get("reviewers", []):
        reviewer_id = str(reviewer.get("id"))
        persona = str(reviewer.get("persona"))
        label = str(reviewer.get("label", reviewer_id))
        hard: list[str] = []
        optional: list[str] = []
        if round_number == 0:
            if digest_task:
                optional.append(digest_task.task_id)
        else:
            own_prior = _find_task(state, kind="reviewer_report", actor=reviewer_id, round_number=round_number - 1)
            if not own_prior or own_prior.status not in {"done"}:
                state.add_warning(
                    "reviewer.update_skipped",
                    f"{reviewer_id} has no completed prior review; round {round_number} update is skipped.",
                )
                continue
            hard.append(own_prior.task_id)
            if rebuttal_task:
                optional.append(rebuttal_task.task_id)
            for other in state.tasks_for(kind="reviewer_report", round=round_number - 1):
                if other.actor_id != reviewer_id:
                    optional.append(other.task_id)
        task = _add_task(
            state,
            kind="reviewer_report",
            stage="review" if round_number == 0 else "update",
            round_number=round_number,
            actor_id=reviewer_id,
            persona=persona,
            label=label,
            output=f"artifacts/reviews/{reviewer_id}.round{round_number}.review.json",
            schema_name="review.schema.json",
            prompt_role="reviewer" if round_number == 0 else "reviewer_update",
            depends_on=hard,
            optional_depends_on=optional,
            executor=_executor_for_backend(str((next((item for item in run.get("reviewers", []) if item.get("id") == reviewer_id), {}) or {}).get("backend"))),
            purpose=str(run.get("purpose", "review")),
        )
        created.append(task)
    return created


def create_rebuttal_task(run_dir: Path, state: RunState, run: dict[str, Any], *, round_number: int) -> Task | None:
    existing = _find_task(state, kind="author_rebuttal", round_number=round_number)
    if existing:
        return existing
    ledger = load_json(ledger_path(run_dir))
    if ledger and not ledger_open_issue_ids(ledger):
        state.add_warning("rebuttal.skipped", "No open issues remain; rebuttal is skipped.")
        return None
    if not ledger:
        raise StateError("ledger.json is required before creating a rebuttal task")
    reviews = state.tasks_for(kind="reviewer_report", round=round_number - 1)
    optional = [task.task_id for task in reviews]
    author = run.get("author") or {}
    task = _add_task(
        state,
        kind="author_rebuttal",
        stage="rebuttal",
        round_number=round_number,
        actor_id=str(author.get("id", "A1")),
        persona="author",
        label="Author",
        output=f"artifacts/rebuttals/{author.get('id', 'A1')}.round{round_number}.rebuttal.json",
        schema_name="rebuttal.schema.json",
        prompt_role="author",
        optional_depends_on=optional,
        executor=_executor_for_backend(str(author.get("backend", "host"))),
        purpose=str(run.get("purpose", "review")),
    )
    return task


def create_meta_task(run_dir: Path, state: RunState, run: dict[str, Any]) -> Task:
    existing = _find_task(state, kind="meta_review")
    if existing:
        return existing
    final_reviews = load_final_reviews(run_dir, run, state.round)
    if not final_reviews:
        raise CoverageError("No final reviews are available; cannot create a meta-review task.")
    ledger = load_json(ledger_path(run_dir))
    if not ledger:
        raise StateError("ledger.json is missing; advance the review rounds first")
    coverage = state.coverage or {}
    if not coverage:
        coverage = _compute_coverage(state, run, state.round)
        state.coverage = coverage
    bundle = policy_bundle(
        ledger=ledger,
        final_reviews=final_reviews,
        profile_name=str(run.get("profile", "conference")),
        coverage=coverage,
    )
    state.policy = bundle
    atomic_write_json(run_dir / "artifacts" / "policy.json", bundle)
    review_tasks = state.tasks_for(kind="reviewer_report")
    task = _add_task(
        state,
        kind="meta_review",
        stage="meta",
        round_number=state.round,
        actor_id="E1",
        persona="meta",
        label="Editor",
        output="artifacts/meta_review.json",
        schema_name="meta_review.schema.json",
        prompt_role="meta",
        optional_depends_on=[item.task_id for item in review_tasks],
        executor=_executor_for_backend(str(((run.get("backends") or {}).get("meta") or {}).get("kind") == "api" and "api" or "host")),
        purpose=str(run.get("purpose", "review")),
    )
    return task


# ---------------------------------------------------------------------------
# Coverage / ledger / convergence
# ---------------------------------------------------------------------------


def _compute_coverage(state: RunState, run: dict[str, Any], round_number: int) -> dict[str, Any]:
    tasks = state.tasks_for(kind="reviewer_report", round=round_number)
    expected = len(run.get("reviewers", []))
    done = [task for task in tasks if task.status == "done"]
    failed = [task for task in tasks if task.status == "failed"]
    skipped = [task for task in tasks if task.status == "skipped"]
    return {
        "round": round_number,
        "reviewers_expected": max(1, expected),
        "reviewers_done": len(done),
        "reviewers_failed": len(failed),
        "reviewers_skipped": len(skipped),
        "complete": len(done) == expected,
        "ratio": round(len(done) / max(1, expected), 3),
        "failed_reviewers": [task.actor_id for task in failed],
        "skipped_reviewers": [task.actor_id for task in skipped],
    }


def _update_coverage(state: RunState, run: dict[str, Any], round_number: int) -> dict[str, Any]:
    coverage = _compute_coverage(state, run, round_number)
    state.coverage = coverage
    if coverage["ratio"] < 0.5:
        state.stage = "failed"
        state.stop_reason = "coverage_below_50"
        state.add_warning(
            "coverage.below_50",
            f"Only {coverage['reviewers_done']}/{coverage['reviewers_expected']} reviewers completed round {round_number}; the run is stopped.",
        )
        return coverage
    if not coverage["complete"]:
        state.add_warning(
            "coverage.incomplete",
            f"Round {round_number} coverage is {coverage['reviewers_done']}/{coverage['reviewers_expected']}; the decision will be provisional.",
        )
    return coverage


def _reviews_by_round(run_dir: Path, run: dict[str, Any], max_round: int) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for round_number in range(0, max_round + 1):
        reviews = load_reviews(run_dir, run, round_number)
        if reviews:
            result[round_number] = reviews
    return result


def _rebuttals(run_dir: Path, run: dict[str, Any], max_round: int) -> list[dict[str, Any]]:
    author_id = str((run.get("author") or {}).get("id", "A1"))
    out: list[dict[str, Any]] = []
    for round_number in range(1, max_round + 1):
        data = load_json(rebuttal_path(run_dir, author_id, round_number))
        if data:
            out.append(data)
    return out


def write_ledger(run_dir: Path, state: RunState, run: dict[str, Any]) -> dict[str, Any]:
    max_round = max(state.round, 0)
    reviews_by_round = _reviews_by_round(run_dir, run, max_round)
    if not reviews_by_round:
        raise StateError("No reviews are available; cannot build the ledger")
    previous = load_json(ledger_path(run_dir))
    ledger = build_ledger(
        paper_id=str(run.get("paper", {}).get("sha256", "")[:16]),
        reviews_by_round=reviews_by_round,
        rebuttals=_rebuttals(run_dir, run, max_round),
        coverage=state.coverage,
        previous_ledger=previous,
        rounds_completed=max_round,
    )
    atomic_write_json(ledger_path(run_dir), ledger)
    errors = validate_schema(ledger, "ledger.schema.json")
    for where, message in errors:
        state.add_warning("ledger.schema", f"{where}: {message}")
    return ledger


def _convergence(run_dir: Path, run: dict[str, Any], ledger: dict[str, Any], round_number: int) -> dict[str, Any]:
    current = load_reviews(run_dir, run, round_number)
    previous = load_reviews(run_dir, run, max(0, round_number - 1))
    previous_by_id = {str(item.get("reviewer_id")): item for item in previous}
    current_by_id = {str(item.get("reviewer_id")): item for item in current}
    common = set(previous_by_id) & set(current_by_id)
    score_delta = 0.0
    stable = 0
    for reviewer_id in common:
        before = (previous_by_id[reviewer_id].get("score") or {}).get("overall")
        after = (current_by_id[reviewer_id].get("score") or {}).get("overall")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            score_delta = max(score_delta, abs(float(before) - float(after)))
        if previous_by_id[reviewer_id].get("recommendation") == current_by_id[reviewer_id].get("recommendation"):
            stable += 1
    rec_stability = (stable / len(common)) if common else 0.0
    new_major = sum(
        1
        for issue in ledger.get("issues", [])
        if int(issue.get("round_opened", 0)) == round_number
        and str(issue.get("severity")) in {"blocking", "major"}
    )
    resolved_major = sum(
        1
        for issue in ledger.get("issues", [])
        if int(issue.get("round_closed") or -1) == round_number
        and str(issue.get("severity")) in {"blocking", "major"}
        and str(issue.get("status")) == "resolved"
    )
    converged = bool(
        new_major == 0
        and score_delta < 0.5
        and rec_stability == 1.0
        and round_number >= 1
        and common
        and bool((run.get("context") or {}).get("early_stop", run.get("options", {}).get("early_stop", True)))
    )
    return {
        "converged": converged,
        "new_major": new_major,
        "resolved_major": resolved_major,
        "score_delta": round(score_delta, 3),
        "rec_stability": round(rec_stability, 3),
        "reviewers_compared": len(common),
    }


# ---------------------------------------------------------------------------
# next / submit / fail / advance
# ---------------------------------------------------------------------------


def next_tasks(run_dir: Path, *, limit: int = 1, executor: str = "host", command: str = "next") -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    with RunLock(run_dir, command=command):
        state = load_state(run_dir)
        run = load_run(run_dir)
        reclaimed = state.reclaim_stale()
        if state.stage == "failed":
            save_state(run_dir, state)
            return {"run_id": state.run_id, "stage": state.stage, "round": state.round, "tasks": [], "action": "failed", "warnings": state.warnings}
        if state.stage in {"done", "rendered"}:
            save_state(run_dir, state)
            return {"run_id": state.run_id, "stage": state.stage, "round": state.round, "tasks": [], "action": "advance" if state.stage == "rendered" else "done", "warnings": state.warnings}
        stage_name = _task_stage_name(state.stage)
        if stage_name is None:
            save_state(run_dir, state)
            return {"run_id": state.run_id, "stage": state.stage, "round": state.round, "tasks": [], "action": "advance", "warnings": state.warnings}
        all_stage_tasks = state.tasks_for(stage=stage_name, round=state.round if stage_name in {"review", "update", "rebuttal"} else None)
        if executor in {"host", "api"}:
            stage_tasks = [task for task in all_stage_tasks if task.executor == executor]
        else:
            stage_tasks = all_stage_tasks
        if not stage_tasks and all_stage_tasks and not all(task.status in TERMINAL_STATUSES for task in all_stage_tasks):
            save_state(run_dir, state)
            return {
                "run_id": state.run_id,
                "stage": state.stage,
                "round": state.round,
                "tasks": [],
                "action": "other_executor",
                "pending": [task.to_state() for task in all_stage_tasks if task.status in {"pending", "running"}],
                "warnings": state.warnings,
            }
        if not stage_tasks:
            save_state(run_dir, state)
            return {"run_id": state.run_id, "stage": state.stage, "round": state.round, "tasks": [], "action": "advance", "warnings": state.warnings}
        if all(task.status in TERMINAL_STATUSES for task in stage_tasks):
            save_state(run_dir, state)
            return {"run_id": state.run_id, "stage": state.stage, "round": state.round, "tasks": [], "action": "advance", "warnings": state.warnings}

        ready = [task for task in stage_tasks if task.status == "pending" and state.is_ready(task)]
        if not ready:
            save_state(run_dir, state)
            return {
                "run_id": state.run_id,
                "stage": state.stage,
                "round": state.round,
                "tasks": [],
                "action": "wait",
                "pending": [task.to_state() for task in stage_tasks if task.status in {"pending", "running"}],
                "warnings": state.warnings,
            }

        packets: list[dict[str, Any]] = []
        for task in ready[: max(1, limit)]:
            try:
                rendered = build_prompt_for_task(run_dir, state, task, run)
                if not run.get("context", {}).get("allow_truncation", False) and rendered.context.get("truncated"):
                    raise ConfigError(
                        "The paper does not fit the context budget and truncation is disabled. "
                        "Increase --max-paper-tokens or pass --allow-truncation."
                    )
                prompt_md, prompt_json = write_prompt_files(run_dir, task.task_id, rendered)
            except Exception as exc:  # prompt assembly failure is task-local
                state.mark_failed(task.task_id, f"prompt assembly failed: {type(exc).__name__}: {exc}")
                state.add_warning("prompt.failed", f"{task.task_id}: {exc}")
                continue
            task.prompt_path = prompt_md
            task.prompt_sha256 = rendered.sha256
            state.claim(task.task_id)
            reviewer = next((item for item in run.get("reviewers", []) if item.get("id") == task.actor_id), None)
            agent_label = str((run.get("agent") or {}).get("label") or "")
            model_hint = None
            if task.executor == "host" and agent_label:
                model_hint = agent_label
            elif reviewer:
                model_hint = reviewer.get("model") or run.get("model")
            elif task.kind in {"digest", "author_rebuttal", "meta_review", "summary_report"}:
                model_hint = run.get("model")
            packet = {
                "schema_version": SCHEMA_VERSION,
                "task_id": task.task_id,
                "run_id": state.run_id,
                "kind": task.kind,
                "stage": task.stage,
                "round": task.round,
                "purpose": str(run.get("purpose", "review")),
                "material": str(run.get("material", "paper")),
                "domain": str(run.get("domain", "ai-ict")),
                "actor": {
                    "id": task.actor_id,
                    "persona": task.persona,
                    "label": task.label,
                    "model_hint": model_hint,
                },
                "prompt_path": prompt_md,
                "prompt_json_path": prompt_json,
                "prompt_sha256": rendered.sha256,
                "prompt_set": run.get("prompt_set", PROMPT_VERSION),
                "schema_path": f"schemas/{task.schema_name}",
                "output_path": task.output,
                "depends_on": list(task.depends_on),
                "optional_depends_on": list(task.optional_depends_on),
                "context_isolation": "shared" if state.isolation in {"session", "unknown"} else "fresh",
                "constraints": {
                    "max_input_tokens": int((run.get("context") or {}).get("max_paper_tokens", 60000)),
                    "max_output_tokens": int((run.get("context") or {}).get("max_output_tokens_per_call", 8000)),
                    "temperature": float((reviewer or {}).get("temperature", 0.3)),
                    "language": str(run.get("output_language") or run.get("language", "auto")),
                    "json_only": task.kind != "legacy_summary",
                    "executor": task.executor,
                    "backend": str(
                        (
                            (reviewer or {}).get("backend")
                            if (reviewer or {}).get("backend") not in {None, "", "host"}
                            else (run.get("agent") or {}).get("label")
                        )
                        or "host"
                    ),
                },
                "context": rendered.context,
            }
            packets.append(packet)
        if stage_name in {"review", "update"}:
            state.update_coverage(len(run.get("reviewers", [])), round_number=state.round)
        save_state(run_dir, state)
        event = EventLog(run_dir)
        if reclaimed:
            for task_id in reclaimed:
                event.emit("task.reclaimed", task_id=task_id)
        for packet in packets:
            event.emit("task.claimed", task_id=packet["task_id"], stage=packet["stage"], actor=packet["actor"]["id"], prompt_sha256=packet["prompt_sha256"])
        return {
            "run_id": state.run_id,
            "stage": state.stage,
            "round": state.round,
            "tasks": packets,
            "action": "execute" if packets else "advance",
            "warnings": state.warnings,
            "reclaimed": reclaimed,
        }


def _load_prompt_context(run_dir: Path, task_id: str) -> dict[str, Any]:
    path = run_dir / "prompts" / f"{task_id}.prompt.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return dict(data.get("context") or {})


def _enrich_manifest_from_digest(run_dir: Path, digest: dict[str, Any], run: dict[str, Any]) -> None:
    """Use the digest's bibliographic block to repair an obviously truncated title.

    Font-based title detection often stops at the first line of a split title
    ("Millimeter", "A Review of Online Diffusion Policy RL"). The digest sees
    the same page but writes the complete bibliographic title. Only accept the
    correction when it extends the extracted title and preserves its first two
    normalized words; otherwise keep the extractor's value.
    """

    manifest_path = run_dir / "paper" / "manifest.json"
    manifest = load_json(manifest_path)
    if not manifest:
        return
    bibliographic = digest.get("bibliographic") or {}
    title = str(bibliographic.get("title") or "").strip()
    current = str(manifest.get("title") or "").strip()
    if title and len(title) > len(current) + 5:
        current_words = re.findall(r"[a-z0-9]+", current.casefold())
        title_words = re.findall(r"[a-z0-9]+", title.casefold())
        if current_words and title_words[: len(current_words)] == current_words:
            manifest["title"] = title
            run.setdefault("paper", {})["title"] = title
    if not manifest.get("authors") and bibliographic.get("authors"):
        manifest["authors"] = list(bibliographic["authors"])
        run.setdefault("paper", {})["authors"] = list(bibliographic["authors"])
    if manifest.get("year") is None and isinstance(bibliographic.get("year"), int):
        manifest["year"] = bibliographic["year"]
    atomic_write_json(manifest_path, manifest)
    save_run(run_dir, run)


def _invalid_path(run_dir: Path, task_id: str) -> Path:
    return ensure_dir(run_dir / "artifacts" / "_invalid") / f"{task_id}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.txt"


def _relpath(run_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def submit_task(
    run_dir: Path,
    *,
    task_id: str,
    payload_file: Path,
    usage: dict[str, Any] | None = None,
    check_only: bool = False,
    force: bool = False,
    command: str = "submit",
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    with RunLock(run_dir, command=command):
        state = load_state(run_dir)
        run = load_run(run_dir)
        manifest = load_json(run_dir / "paper" / "manifest.json")
        if not manifest:
            raise StateError("paper/manifest.json is missing")
        task = state.task(task_id)
        output_path = run_dir / task.output
        payload_path = Path(payload_file).expanduser()
        if not payload_path.is_absolute():
            cwd_candidate = Path.cwd() / payload_path
            payload_path = cwd_candidate if cwd_candidate.is_file() else run_dir / payload_path
        if not payload_path.is_file():
            raise ValidationError(f"Submitted file not found: {payload_path}")
        raw = payload_path.read_text(encoding="utf-8", errors="replace")
        if task.status == "done" and not force:
            return {"ok": True, "task_id": task_id, "status": "already_done", "output": task.output, "warnings": []}
        try:
            payload, parse_warnings = parse_json_output(raw)
        except ValueError as exc:
            invalid = _invalid_path(run_dir, task_id)
            atomic_write_text(invalid, raw)
            EventLog(run_dir).validation_failed(task_id=task_id, errors=[{"where": "json", "message": str(exc)}])
            raise ValidationError(f"Invalid JSON for {task_id}: {exc}. Raw output saved to {_relpath(run_dir, invalid)}") from exc
        quote_index = QuoteIndex.from_run_dir(run_dir)
        prompt_context = _load_prompt_context(run_dir, task_id)
        provided_sections = prompt_context.get("sections", []) or []
        strict = bool((run.get("options") or {}).get("strict_evidence", True))
        ledger = load_json(ledger_path(run_dir))
        result: Any = None

        if task.kind == "legacy_summary":
            result = None
            sanitized = {"text": raw}
        elif task.kind == "digest":
            result = validate_artifact(
                "digest",
                payload,
                manifest=manifest,
                quote_index=quote_index,
                strict=strict,
            )
            sanitized = result.sanitized
        elif task.kind == "summary_report":
            result = validate_artifact(
                "summary_report",
                payload,
                run=run,
                manifest=manifest,
                quote_index=quote_index,
                strict=strict,
            )
            sanitized = result.sanitized
        elif task.kind == "reviewer_report":
            prior = load_json(review_path(run_dir, task.actor_id, task.round - 1)) if task.round >= 1 else None
            rebuttal = load_json(rebuttal_path(run_dir, str((run.get("author") or {}).get("id", "A1")), task.round)) if task.round >= 1 else None
            result = validate_artifact(
                "reviewer_report",
                payload,
                run=run,
                task=task.to_state(),
                manifest=manifest,
                quote_index=quote_index,
                provided_section_ids=provided_sections,
                prior_review=prior,
                ledger=ledger,
                rebuttal=rebuttal,
                strict=strict,
            )
            sanitized = result.sanitized
        elif task.kind == "author_rebuttal":
            if not ledger:
                raise StateError("ledger.json is missing; cannot validate a rebuttal")
            result = validate_artifact(
                "author_rebuttal",
                payload,
                run=run,
                task=task.to_state(),
                manifest=manifest,
                quote_index=quote_index,
                ledger=ledger,
                strict=strict,
                allow_incomplete=False,
            )
            sanitized = result.sanitized
        elif task.kind == "meta_review":
            if not ledger:
                raise StateError("ledger.json is missing; cannot validate a meta review")
            final_reviews = load_final_reviews(run_dir, run, state.round)
            policy_pre = str((state.policy or {}).get("policy_decision") or policy_from_summary(dict(ledger.get("summary", {}))))
            result = validate_artifact(
                "meta_review",
                payload,
                run=run,
                task=task.to_state(),
                manifest=manifest,
                ledger=ledger,
                final_reviews=final_reviews,
                quote_index=quote_index,
                policy_pre=policy_pre,
                coverage=state.coverage,
                strict=strict,
            )
            sanitized = result.sanitized
        else:
            raise StateError(f"Unknown task kind {task.kind!r}")

        if result is not None and not result.ok:
            invalid = _invalid_path(run_dir, task_id)
            atomic_write_text(invalid, raw)
            EventLog(run_dir).validation_failed(task_id=task_id, errors=[problem.to_dict() for problem in result.errors])
            raise ValidationError(
                f"Validation failed for {task_id}",
                detail={
                    "task_id": task_id,
                    "errors": [problem.to_dict() for problem in result.errors],
                    "warnings": [problem.to_dict() for problem in result.warnings],
                    "raw_saved_to": _relpath(run_dir, invalid),
                },
            )

        warnings: list[dict[str, Any]] = []
        if result is not None:
            for problem in result.warnings:
                state.add_warning(f"validation.{problem.code}", f"{problem.where}: {problem.message}")
                warnings.append(problem.to_dict())
            if result.extra.get("evidence_verification"):
                evidence_dir = ensure_dir(run_dir / "artifacts" / "evidence")
                atomic_write_json(
                    evidence_dir / f"{task_id}.json",
                    {
                        "task_id": task_id,
                        "kind": task.kind,
                        "actor": task.actor_id,
                        "created_at": utc_now_iso(),
                        "results": result.extra.get("evidence_verification", []),
                        "summary": result.extra.get("evidence_summary", {}),
                    },
                )
        if parse_warnings:
            for warning in parse_warnings:
                state.add_warning("parse.repair", warning)

        if check_only:
            save_state(run_dir, state)
            return {
                "ok": True,
                "task_id": task_id,
                "check_only": True,
                "warnings": warnings,
                "evidence_summary": (result.extra.get("evidence_summary") if result else {}),
            }

        if task.kind == "legacy_summary":
            ensure_dir(output_path.parent)
            atomic_write_text(output_path, raw)
        else:
            sanitized.setdefault("created_at", utc_now_iso())
            sanitized.setdefault("prompt_version", run.get("prompt_set", PROMPT_VERSION))
            ensure_dir(output_path.parent)
            atomic_write_json(output_path, sanitized)
            if task.kind == "digest":
                _enrich_manifest_from_digest(run_dir, sanitized, run)

        # Meta review closes the ledger.
        if task.kind == "meta_review":
            final_ledger = apply_meta_resolutions(ledger, sanitized)
            atomic_write_json(run_dir / "artifacts" / "ledger.final.json", final_ledger)
            errors = validate_schema(final_ledger, "ledger.schema.json")
            for where, message in errors:
                state.add_warning("ledger.final_schema", f"{where}: {message}")
            if result is not None:
                final_policy = result.extra.get("policy_final")
                if final_policy:
                    state.policy = dict(state.policy or {})
                    state.policy["policy_final"] = final_policy
                    state.policy["counts_final"] = result.extra.get("counts_final", {})
            if not state.coverage.get("complete", False):
                state.add_warning(
                    "decision.provisional",
                    "Meta review completed with incomplete reviewer coverage; the decision is provisional.",
                )

        state.mark_done(task_id)
        if task.kind == "reviewer_report":
            state.update_coverage(len(run.get("reviewers", [])), round_number=task.round)
        if usage:
            try:
                tracker = UsageTracker(run_dir, max_cost_usd=(run.get("budget") or {}).get("max_cost_usd"))
                tracker.record(
                    model=str(usage.get("model") or "unknown"),
                    stage=task.stage,
                    tokens_in=int(usage.get("tokens_in", 0)),
                    tokens_out=int(usage.get("tokens_out", 0)),
                    cost_usd=float(usage.get("cost_usd", 0.0)),
                )
                task.usage = usage
                usage_model = str(usage.get("model") or "").strip()
                agent = run.get("agent") or {}
                agent_model = str(agent.get("model") or "").strip()
                if (
                    task.executor == "host"
                    and usage_model
                    and agent_model
                    and backend_vendor(agent.get("provider"), agent_model) == backend_vendor(None, usage_model)
                    and usage_model.lower() != agent_model.lower()
                ):
                    state.add_warning(
                        "backend.host_model_mismatch",
                        f"{task.task_id} reported model {usage_model!r} while the host-agent base is "
                        f"{agent.get('label')!r}; the review no longer ran on the agent base.",
                    )
            except (OSError, ValueError) as exc:
                state.add_warning("usage.record_failed", f"could not record usage: {exc}")
        save_state(run_dir, state)
        event = EventLog(run_dir)
        event.task_completed(
            task_id=task_id,
            stage=task.stage,
            actor=task.actor_id,
            output=task.output,
            evidence_summary=(result.extra.get("evidence_summary") if result else {}),
        )
        return {
            "ok": True,
            "task_id": task_id,
            "status": "done",
            "output": task.output,
            "warnings": warnings,
            "evidence_summary": (result.extra.get("evidence_summary") if result else {}),
        }


def fail_task(run_dir: Path, *, task_id: str, reason: str, command: str = "fail") -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    with RunLock(run_dir, command=command):
        state = load_state(run_dir)
        run = load_run(run_dir)
        task = state.task(task_id)
        if task.status == "done":
            raise StateError(f"Task {task_id} is already done; refusing to mark it failed")
        state.mark_failed(task_id, reason)
        if task.kind == "reviewer_report":
            state.update_coverage(len(run.get("reviewers", [])), round_number=task.round)
        save_state(run_dir, state)
        EventLog(run_dir).task_failed(task_id=task_id, stage=task.stage, actor=task.actor_id, reason=reason)
        return {"ok": True, "task_id": task_id, "status": "failed", "reason": reason, "warnings": state.warnings}


def advance(run_dir: Path, *, command: str = "advance") -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    with RunLock(run_dir, command=command):
        state = load_state(run_dir)
        run = load_run(run_dir)
        reclaimed = state.reclaim_stale()
        if state.stage in {"failed", "done"}:
            save_state(run_dir, state)
            return {"stage": state.stage, "round": state.round, "changed": False, "warnings": state.warnings}
        stage_name = _task_stage_name(state.stage)
        stage_tasks = state.tasks_for(stage=stage_name, round=state.round if stage_name in {"review", "update", "rebuttal"} else None) if stage_name else []
        pending = [task for task in stage_tasks if task.status not in TERMINAL_STATUSES]
        if pending:
            raise StateError(
                f"Cannot advance: {len(pending)} task(s) in stage {state.stage!r} are not terminal: "
                + ", ".join(f"{task.task_id}({task.status})" for task in pending)
            )
        old_stage = state.stage
        event = EventLog(run_dir)
        if reclaimed:
            for task_id in reclaimed:
                event.emit("task.reclaimed", task_id=task_id)

        if state.stage == "digesting":
            if str(run.get("purpose", "review")) == "summary":
                create_summary_task(run_dir, state, run)
                state.stage = "summary"
                state.round = 0
            else:
                create_review_tasks(run_dir, state, run, round_number=0)
                state.stage = "reviewing"
                state.round = 0
                state.update_coverage(len(run.get("reviewers", [])))
        elif state.stage == "summary":
            state.stage = "rendered"
            state.stop_reason = "summary_complete"
        elif state.stage == "reviewing":
            coverage = _update_coverage(state, run, 0)
            if coverage["ratio"] < 0.5:
                save_state(run_dir, state)
                raise CoverageError(
                    f"Only {coverage['reviewers_done']}/{coverage['reviewers_expected']} reviewers completed; refusing to produce a complete review.",
                    detail={"coverage": coverage, "run_dir": state.run_id},
                )
            ledger = write_ledger(run_dir, state, run)
            state.round = 0
            author_mode = str((run.get("author") or {}).get("mode", "simulated"))
            if state.max_rounds == 0 or not ledger_open_issue_ids(ledger):
                create_meta_task(run_dir, state, run)
                state.stage = "meta"
            elif author_mode == "none":
                state.round = 1
                create_review_tasks(run_dir, state, run, round_number=1)
                state.stage = "updating"
            elif author_mode == "provided":
                imported = _import_provided_rebuttal(run_dir, state, run, round_number=1)
                if not imported:
                    state.add_warning("author.provided_missing", "Provided rebuttal file is missing; continuing with author mode none.")
                    run["author"]["mode"] = "none"
                    save_run(run_dir, run)
                state.round = 1
                create_review_tasks(run_dir, state, run, round_number=1)
                state.stage = "updating"
            else:
                task = create_rebuttal_task(run_dir, state, run, round_number=1)
                if task is None:
                    state.round = 1
                    create_review_tasks(run_dir, state, run, round_number=1)
                    state.stage = "updating"
                else:
                    state.round = 1
                    state.stage = "rebutting"
        elif state.stage == "rebutting":
            write_ledger(run_dir, state, run)
            create_review_tasks(run_dir, state, run, round_number=state.round)
            state.stage = "updating"
        elif state.stage == "updating":
            coverage = _update_coverage(state, run, state.round)
            if coverage["ratio"] < 0.5:
                save_state(run_dir, state)
                raise CoverageError(
                    f"Only {coverage['reviewers_done']}/{coverage['reviewers_expected']} reviewers completed round {state.round}.",
                    detail={"coverage": coverage, "run_id": state.run_id},
                )
            ledger = write_ledger(run_dir, state, run)
            convergence = _convergence(run_dir, run, ledger, state.round)
            if convergence["converged"] or state.round >= state.max_rounds:
                state.stop_reason = "converged" if convergence["converged"] else "max_rounds"
                create_meta_task(run_dir, state, run)
                state.stage = "meta"
            else:
                state.round += 1
                author_mode = str((run.get("author") or {}).get("mode", "simulated"))
                if author_mode == "simulated":
                    task = create_rebuttal_task(run_dir, state, run, round_number=state.round)
                    if task is not None:
                        state.stage = "rebutting"
                    else:
                        create_review_tasks(run_dir, state, run, round_number=state.round)
                        state.stage = "updating"
                else:
                    if author_mode == "provided":
                        state.add_warning("author.provided_rounds", "No provided rebuttal for this round; switching to author mode none.")
                        run["author"]["mode"] = "none"
                        save_run(run_dir, run)
                    create_review_tasks(run_dir, state, run, round_number=state.round)
                    state.stage = "updating"
        elif state.stage == "meta":
            meta_task = _find_task(state, kind="meta_review")
            if meta_task and meta_task.status == "done":
                meta = load_json(run_dir / "artifacts" / "meta_review.json")
                ledger = load_json(ledger_path(run_dir))
                if not meta or not ledger:
                    raise StateError("meta task is done but its artifact is missing")
                final_ledger = apply_meta_resolutions(ledger, meta)
                atomic_write_json(run_dir / "artifacts" / "ledger.final.json", final_ledger)
                state.stage = "rendered"
                state.stop_reason = state.stop_reason or "meta_complete"
            else:
                ledger = load_json(ledger_path(run_dir))
                if not ledger:
                    raise StateError("ledger.json is missing; cannot create a policy-only meta review")
                final_reviews = load_final_reviews(run_dir, run, state.round)
                scores = aggregate_scores(final_reviews, str(run.get("profile", "conference")))
                policy = policy_decision(ledger.get("issues", []))
                synthetic = _policy_only_meta(run, ledger, policy, scores, state.coverage)
                atomic_write_json(run_dir / "artifacts" / "meta_review.json", synthetic)
                final_ledger = apply_meta_resolutions(ledger, synthetic)
                atomic_write_json(run_dir / "artifacts" / "ledger.final.json", final_ledger)
                state.add_warning("meta.fallback", "Meta review failed or was unavailable; a deterministic policy-only decision was written.")
                state.stage = "rendered"
                state.stop_reason = "meta_fallback"
        elif state.stage == "rendered":
            output_dir = run_dir / "output"
            if not any((output_dir / name).is_file() for name in ("report.md", "report.json", "report.html")):
                raise StateError("The run is rendered but no report file exists; run `hpe.py render` before advancing to done.")
            state.stage = "done"
        else:
            raise StateError(f"Cannot advance from stage {state.stage!r}")

        save_state(run_dir, state)
        event.stage_changed(old=old_stage, new=state.stage, round=state.round, stop_reason=state.stop_reason)
        return {
            "stage": state.stage,
            "round": state.round,
            "stop_reason": state.stop_reason,
            "changed": old_stage != state.stage,
            "warnings": state.warnings,
            "coverage": state.coverage,
        }


def _policy_only_meta(run: dict[str, Any], ledger: dict[str, Any], policy: dict[str, Any], scores: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    issues = ledger.get("issues", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_id": str(run.get("paper", {}).get("sha256", "")[:16]),
        "editor_id": "E1",
        "decision": policy["policy_decision"],
        "policy_decision": policy["policy_decision"],
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
        "issue_resolution": [
            {
                "issue_id": issue.get("id"),
                "final_status": issue.get("status") if issue.get("status") in {"resolved", "partially_resolved", "disputed", "open", "out_of_scope"} else "open",
                "merged_from": issue.get("aliases", []),
                "rationale": "Policy-only fallback: meta review did not produce a usable artifact.",
            }
            for issue in issues
        ],
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
        "limitations": ["Meta review artifact was unavailable; this decision is the deterministic policy result."],
        "created_at": utc_now_iso(),
    }


def _import_provided_rebuttal(run_dir: Path, state: RunState, run: dict[str, Any], *, round_number: int) -> bool:
    author = run.get("author") or {}
    file_value = author.get("rebuttal_file")
    if not file_value:
        return False
    path = Path(str(file_value)).expanduser()
    if not path.is_absolute():
        path = run_dir / path
    if not path.is_file():
        return False
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Provided rebuttal file is not valid JSON: {exc}") from exc
    task = {
        "kind": "author_rebuttal",
        "actor_id": str(author.get("id", "A1")),
        "round": round_number,
        "persona": "author",
        "label": "Author",
    }
    manifest = load_json(run_dir / "paper" / "manifest.json") or {}
    quote_index = QuoteIndex.from_run_dir(run_dir)
    ledger = load_json(ledger_path(run_dir))
    if not ledger:
        raise StateError("ledger.json is required before importing a provided rebuttal")
    result = validate_artifact(
        "author_rebuttal",
        payload,
        run=run,
        task=task,
        manifest=manifest,
        quote_index=quote_index,
        ledger=ledger,
        strict=bool((run.get("options") or {}).get("strict_evidence", True)),
    )
    if not result.ok:
        raise ValidationError(
            "Provided rebuttal failed validation",
            detail={"errors": [problem.to_dict() for problem in result.errors], "warnings": [problem.to_dict() for problem in result.warnings]},
        )
    ensure_dir((run_dir / "artifacts" / "rebuttals"))
    atomic_write_json(rebuttal_path(run_dir, str(author.get("id", "A1")), round_number), result.sanitized)
    for problem in result.warnings:
        state.add_warning(f"rebuttal.{problem.code}", f"{problem.where}: {problem.message}")
    return True


# ---------------------------------------------------------------------------
# Status / render / API loop
# ---------------------------------------------------------------------------


def status(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    state = load_state(run_dir)
    run = load_run(run_dir)
    tasks = [task.to_state() for task in sorted(state.tasks.values(), key=lambda item: item.task_id)]
    counts: dict[str, int] = {}
    for task in state.tasks.values():
        counts[task.status] = counts.get(task.status, 0) + 1
    ledger = load_json(ledger_path(run_dir, final=True)) or load_json(ledger_path(run_dir))
    return {
        "run_id": state.run_id,
        "stage": state.stage,
        "round": state.round,
        "max_rounds": state.max_rounds,
        "task_counts": counts,
        "coverage": state.coverage,
        "warnings": state.warnings,
        "policy": state.policy,
        "issue_summary": (ledger or {}).get("summary"),
        "tasks": tasks,
        "outputs": {
            "report_md": str(run_dir / "output" / "report.md") if (run_dir / "output" / "report.md").is_file() else None,
            "report_json": str(run_dir / "output" / "report.json") if (run_dir / "output" / "report.json").is_file() else None,
        },
        "bilingual": check_bilingual_files(run_dir) if str(run.get("purpose", "review")) in {"review", "problem_finding"} else {},
    }


def render(run_dir: Path, *, formats: Sequence[str] = ("md", "json"), command: str = "render") -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    with RunLock(run_dir, command=command):
        state = load_state(run_dir)
        run = load_run(run_dir)
        summary_mode = str(run.get("purpose", "review")) == "summary"
        allowed = {"summary", "rendered", "done"} if summary_mode else {"meta", "rendered", "done"}
        if state.stage not in allowed:
            raise StateError(f"Cannot render from stage {state.stage!r}; finish the {'summary' if summary_mode else 'meta review'} first")
        manifest = load_json(run_dir / "paper" / "manifest.json")
        if not manifest:
            raise StateError("paper/manifest.json is missing")
        if summary_mode:
            summary = load_json(run_dir / "summary" / "summary.json")
            if not summary:
                raise StateError("summary/summary.json is missing; submit the summary task first")
            tracker = UsageTracker(run_dir, max_cost_usd=(run.get("budget") or {}).get("max_cost_usd"))
            totals = tracker.totals
            done_llm = sum(1 for task in state.tasks.values() if task.kind in LLM_KINDS and task.status == "done")
            usage = {
                "calls": int(totals.get("calls", 0)) if tracker.known else done_llm,
                "tokens_in": int(totals.get("tokens_in", 0)) if tracker.known else 0,
                "tokens_out": int(totals.get("tokens_out", 0)) if tracker.known else 0,
                "cost_usd": float(totals.get("cost_usd", 0.0)) if tracker.known else 0.0,
                "known": tracker.known,
            }
            written = render_summary_files(
                run_dir=run_dir, run=run, state=state, manifest=manifest, usage=usage, formats=formats
            )
            state.stage = "done"
            save_state(run_dir, state)
            EventLog(run_dir).emit("summary.rendered", formats=list(formats), files=written)
            return {
                "stage": state.stage,
                "written": written,
                "warnings": state.warnings,
                "purpose": "summary",
                "confidence": summary.get("confidence"),
            }
        ledger = load_json(ledger_path(run_dir, final=True)) or load_json(ledger_path(run_dir))
        if not ledger:
            raise StateError("No ledger found; cannot render")
        meta = load_json(run_dir / "artifacts" / "meta_review.json")
        final_reviews = load_final_reviews(run_dir, run, state.round)
        rebuttals = _rebuttals(run_dir, run, state.round)
        tracker = UsageTracker(run_dir, max_cost_usd=(run.get("budget") or {}).get("max_cost_usd"))
        totals = tracker.totals
        done_llm = sum(1 for task in state.tasks.values() if task.kind in LLM_KINDS and task.status == "done")
        usage = {
            "calls": int(totals.get("calls", 0)) if tracker.known else done_llm,
            "tokens_in": int(totals.get("tokens_in", 0)) if tracker.known else 0,
            "tokens_out": int(totals.get("tokens_out", 0)) if tracker.known else 0,
            "cost_usd": float(totals.get("cost_usd", 0.0)) if tracker.known else 0.0,
            "known": tracker.known,
        }
        if meta is None:
            final_ledger = ledger
            policy = policy_decision(final_ledger.get("issues", []))
            scores = aggregate_scores(final_reviews, str(run.get("profile", "conference")))
            meta = _policy_only_meta(run, final_ledger, policy, scores, state.coverage)
            atomic_write_json(run_dir / "artifacts" / "meta_review.json", meta)
            state.add_warning("meta.fallback", "No meta review artifact was available; rendered a policy-only decision.")
        else:
            final_ledger = apply_meta_resolutions(ledger, meta)
            atomic_write_json(run_dir / "artifacts" / "ledger.final.json", final_ledger)
        written = render_report_files(
            run_dir=run_dir,
            run=run,
            state=state,
            manifest=manifest,
            ledger=ledger,
            meta=meta,
            final_reviews=final_reviews,
            rebuttals=rebuttals,
            usage=usage,
            formats=formats,
        )
        bilingual = prepare_bilingual_files(run_dir)
        report_json = load_json(run_dir / "output" / "report.json")
        if report_json is not None:
            report_json.setdefault("artifacts", {})["bilingual"] = bilingual.get("files", {})
            atomic_write_json(run_dir / "output" / "report.json", report_json)
            errors = validate_schema(report_json, "final_report.schema.json")
            for where, message in errors:
                state.add_warning("report.schema", f"{where}: {message}")
        if bilingual.get("tasks"):
            state.add_warning(
                "bilingual.pending",
                f"{len(bilingual['tasks'])} bilingual translation task(s) remain; execute the prompt files "
                "and then run `hpe.py bilingual --run <run_dir> --check`.",
            )
        else:
            state.warnings = [item for item in state.warnings if item.get("code") != "bilingual.pending"]
        state.stage = "done"
        save_state(run_dir, state)
        EventLog(run_dir).emit("report.rendered", formats=list(formats), files=written)
        return {
            "stage": state.stage,
            "written": written,
            "bilingual": bilingual,
            "warnings": state.warnings,
            "decision": (meta or {}).get("decision"),
            "policy_decision": (meta or {}).get("policy_decision"),
            "issue_summary": final_ledger.get("summary"),
        }


def _reset_task_pending(run_dir: Path, task_id: str, reason: str) -> None:
    with RunLock(run_dir, command="budget-reset"):
        state = load_state(run_dir)
        task = state.task(task_id)
        if task.status == "running":
            task.status = "pending"
            task.claimed_at = None
            state.add_warning("budget.paused", reason)
            save_state(run_dir, state)


def run_api(
    *,
    run_dir: Path,
    provider_name: str | None,
    model_name: str | None,
    max_tasks: int = 1000,
    command: str = "run",
) -> dict[str, Any]:
    from .llm_config import backend_from_spec, load_llm_config
    from .providers import provider_for, provider_for_backend

    run_dir = run_dir.expanduser().resolve()
    tracker = None
    providers: dict[str, Any] = {}
    executed = 0
    while executed < max_tasks:
        result = next_tasks(run_dir, limit=1, executor="api", command=command)
        action = result.get("action")
        if action == "done":
            return {"stage": "done", "executed": executed, "warnings": result.get("warnings", [])}
        if action == "failed":
            raise CoverageError("Run failed; inspect `hpe.py status --run <run_dir>`")
        if action == "advance":
            current = load_state(run_dir)
            if current.stage == "rendered":
                return {"stage": "rendered", "executed": executed, "warnings": current.warnings}
            advance(run_dir, command=command)
            continue
        if action == "other_executor":
            return {
                "stage": result.get("stage"),
                "executed": executed,
                "action": "host_tasks",
                "pending": result.get("pending", []),
                "warnings": result.get("warnings", []),
            }
        if action == "wait":
            raise StateError("API mode has no ready task but the stage is not complete; inspect the run state")
        if action != "execute":
            raise StateError(f"Unexpected next action {action!r}")
        for packet in result.get("tasks", []):
            task_id = packet["task_id"]
            prompt_json_path = run_dir / str(packet.get("prompt_json_path") or "")
            if not prompt_json_path.is_file():
                raise StateError(f"Prompt JSON missing for {task_id}")
            prompt = read_json(prompt_json_path)
            task = load_state(run_dir).task(task_id)
            run = load_run(run_dir)
            reviewer = next((item for item in run.get("reviewers", []) if item.get("id") == task.actor_id), None)
            backend_label = None
            if reviewer:
                backend_label = reviewer.get("backend")
            elif task.kind == "author_rebuttal":
                backend_label = (run.get("author") or {}).get("backend")
            elif task.kind == "summary_report":
                digest_backend = (run.get("backends") or {}).get("digest") or {}
                backend_label = digest_backend.get("label") or ("host" if digest_backend.get("kind") == "host" else f"{digest_backend.get('provider')}:{digest_backend.get('model')}")
            elif task.kind == "digest":
                digest_backend = (run.get("backends") or {}).get("digest") or {}
                backend_label = "host" if digest_backend.get("kind") == "host" else f"{digest_backend.get('provider')}:{digest_backend.get('model')}"
            elif task.kind == "meta_review":
                meta_backend = (run.get("backends") or {}).get("meta") or {}
                backend_label = "host" if meta_backend.get("kind") == "host" else f"{meta_backend.get('provider')}:{meta_backend.get('model')}"
            key = str(backend_label or "")
            if not key:
                agent = run.get("agent") or {}
                provider_id = provider_name or run.get("provider") or agent.get("provider")
                model_id = model_name or run.get("model") or agent.get("model")
                key = f"legacy:{provider_id}:{model_id}"
            if key not in providers:
                if backend_label:
                    config = load_llm_config(run.get("llm_config"))
                    backend_spec = backend_from_spec(backend_label, config, default_provider=provider_name, default_model=model_name)
                    if backend_spec.is_host:
                        raise StateError(f"Task {task_id} is marked API but its backend is host")
                    providers[key] = provider_for_backend(backend_spec)
                else:
                    providers[key] = provider_for(provider_name, model_name)
            provider = providers[key]
            constraints = packet.get("constraints", {})
            max_tokens = int(constraints.get("max_output_tokens", 8000))
            tokens_in, tokens_out, estimated_cost = provider.estimate_cost(prompt["system"], prompt["user"], max_tokens)
            budget = run.get("budget") or {}
            max_cost = budget.get("max_cost_usd")
            tracker = tracker or UsageTracker(run_dir, max_cost_usd=max_cost)
            if max_cost is not None and tracker.totals.get("cost_usd", 0.0) + estimated_cost > float(max_cost):
                _reset_task_pending(run_dir, task_id, "budget exceeded before the next call")
                raise BudgetExceeded(
                    f"Budget ${max_cost} would be exceeded by the next call (estimated ${estimated_cost:.4f}). "
                    "Increase --budget-usd and resume."
                )
            completion = provider.complete(
                system=prompt["system"],
                user=prompt["user"],
                temperature=float(constraints.get("temperature", 0.3)),
                max_tokens=max_tokens,
                json_mode=bool(constraints.get("json_only", True)),
            )
            raw_dir = ensure_dir(run_dir / "artifacts" / "_api")
            raw_path = raw_dir / f"{task_id}.json"
            atomic_write_text(raw_path, completion.text)
            usage = {
                "model": completion.model,
                "tokens_in": completion.usage.get("tokens_in", 0),
                "tokens_out": completion.usage.get("tokens_out", 0),
                "cost_usd": provider.spec.estimate_cost(
                    completion.usage.get("tokens_in", tokens_in),
                    completion.usage.get("tokens_out", tokens_out),
                ),
            }
            submit_task(run_dir, task_id=task_id, payload_file=raw_path, usage=usage, command=command)
            executed += 1
            tracker = None  # re-read usage next iteration
    raise BudgetExceeded(f"Stopped after {max_tasks} task executions; increase --max-tasks if needed.")
