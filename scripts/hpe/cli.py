"""Command-line interface for HPE v2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bilingual import check_bilingual_files, prepare_bilingual_files
from .config import (
    MAX_ROUNDS,
    PROMPT_VERSION,
    RunOptions,
    ContextLimits,
    Budget,
    get_profile,
)
from .errors import (
    EXIT_CANCELLED,
    EXIT_OK,
    HpeError,
)
from .domains import load_domains
from .ingest import doctor as ingest_doctor
from .llm_config import detect_agent_identity, init_llm_config, load_llm_config
from .util import default_run_dir
from .orchestrator import (
    advance,
    fail_task,
    init_run,
    next_tasks,
    render,
    run_api,
    status,
    submit_task,
)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _reviewers_from_arg(value: str | None) -> list[dict[str, Any]]:
    names = _split_csv(value)
    if not names or names == ["auto"]:
        return []
    return [{"persona": name} for name in names]


def _force_unlock(run_dir: Path) -> None:
    lock = run_dir / "run.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)


def _print(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                print(f"{key}: {value}")
    else:
        print(data)


def _build_init_options(args: argparse.Namespace) -> RunOptions:
    profile = get_profile(args.profile)
    rounds = args.rounds if args.rounds is not None else int(profile.get("default_rounds", 2))
    if rounds < 0 or rounds > MAX_ROUNDS:
        raise HpeError(f"--rounds must be between 0 and {MAX_ROUNDS}")
    if args.mode == "legacy":
        raise HpeError("Use `hpe.py legacy -- ...` for the v1 HPE-tool path; a v2 run cannot use mode=legacy.")
    purpose = getattr(args, "purpose", "review")
    if args.rounds is None and purpose in {"review", "problem_finding"} and rounds < 1:
        # A default review must include at least one cross-review round with
        # the simulated author's response; explicit --rounds 0 is still honored.
        rounds = 1
    material = getattr(args, "material", "paper")
    domain = getattr(args, "domain", "ai-ict")
    reviewer_backends = _split_csv(getattr(args, "reviewer_backends", None))
    llm_config_value = getattr(args, "llm_config", None)
    role_cfg: dict[str, Any] = {}
    try:
        role_cfg = load_llm_config(llm_config_value).roles
    except HpeError:
        role_cfg = {}
    if args.mode == "api":
        def non_host(value: Any) -> str | None:
            text = str(value or "").strip()
            return text if text and text.lower() != "host" else None

        cfg_reviewers = [str(item) for item in (role_cfg.get("reviewers") or []) if non_host(item)]
        if not reviewer_backends:
            reviewer_backends = cfg_reviewers
        author_backend = getattr(args, "author_backend", None) or non_host(role_cfg.get("author"))
        digest_backend = getattr(args, "digest_backend", None) or non_host(role_cfg.get("digest"))
        meta_backend = getattr(args, "meta_backend", None) or non_host(role_cfg.get("meta"))
    else:
        author_backend = getattr(args, "author_backend", None) or role_cfg.get("author")
        digest_backend = getattr(args, "digest_backend", None) or role_cfg.get("digest")
        meta_backend = getattr(args, "meta_backend", None) or role_cfg.get("meta")
    reviewers = _reviewers_from_arg(args.reviewers)
    context = ContextLimits(
        max_paper_tokens=args.max_paper_tokens,
        max_output_tokens_per_call=args.max_output_tokens,
        allow_truncation=args.allow_truncation,
        include_digest=not args.no_digest,
        include_references=args.include_references,
    )
    budget = Budget(
        max_cost_usd=args.budget_usd,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens_total,
    )
    return RunOptions(
        profile=args.profile,
        mode=args.mode,
        purpose=purpose,
        material=material,
        domain=domain,
        summary_style=getattr(args, "summary_style", "upstream"),
        llm_config=llm_config_value,
        agent_backend=getattr(args, "agent_backend", None),
        allow_backend_mismatch=bool(getattr(args, "allow_backend_mismatch", False)),
        reviewer_backends=reviewer_backends,
        author_backend=author_backend,
        digest_backend=digest_backend,
        meta_backend=meta_backend,
        provider=args.provider,
        model=args.model,
        rounds=rounds,
        language=args.language,
        reviewers=reviewers,
        author_mode=args.author,
        provided_rebuttal=args.rebuttal_file,
        isolation=args.isolation,
        blind_paper=args.blind_paper,
        strict_evidence=not args.lenient_evidence,
        early_stop=not args.no_early_stop,
        allow_poor_extraction=args.allow_poor_extraction,
        ocr=args.ocr,
        vision_pages=getattr(args, "vision_pages", "auto"),
        vision_max_pages=int(getattr(args, "vision_max_pages", 20)),
        vision_dpi=int(getattr(args, "vision_dpi", 150)),
        digest=not args.no_digest,
        prompt_set=PROMPT_VERSION,
        context=context,
        budget=budget,
    )


def _add_init_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pdf", required=True, help="Local PDF path")
    parser.add_argument("--out", default=None,
                        help="Run directory; default: <PDF directory>/<PDF filename>.hpe (all artifacts and reports live there)")
    parser.add_argument("--profile", choices=["paper", "conference", "journal", "proposal", "experiment"], default="conference")
    parser.add_argument("--purpose", choices=["review", "summary", "problem_finding"], default="review",
                        help="review (default), detailed summary, or problem finding")
    parser.add_argument("--material", choices=["paper", "proposal", "design"], default="paper",
                        help="paper, research proposal, or project/experiment design")
    parser.add_argument("--domain", default="ai-ict",
                        help="domain id from config/domains.json (ai-ict, materials, mechanical, or auto)")
    parser.add_argument("--summary-style", choices=["upstream"], default="upstream",
                        help="summary template style; upstream preserves the detailed 20/12/14-section structure")
    parser.add_argument("--mode", choices=["host", "api", "hybrid", "legacy"], default="host")
    parser.add_argument("--provider", default=None, help="Default provider for digest/author/meta")
    parser.add_argument("--model", default=None, help="Default model for digest/author/meta")
    parser.add_argument("--reviewers", default="auto", help="Comma-separated personas, or auto")
    parser.add_argument("--rounds", "--debate-rounds", dest="rounds", type=int, default=None,
                        help="Debate/cross-review rounds after round 0")
    parser.add_argument("--language", choices=["auto", "zh", "en"], default="auto",
                        help="Report and review prose language: auto follows the paper, zh forces Chinese, en forces English")
    parser.add_argument("--author", choices=["simulated", "provided", "none"], default="simulated")
    parser.add_argument("--rebuttal-file", default=None, help="Provided rebuttal JSON (author mode provided)")
    parser.add_argument("--isolation", choices=["subagent", "session", "unknown"], default="subagent")
    parser.add_argument("--budget-usd", type=float, default=None)
    parser.add_argument("--max-input-tokens", type=int, default=None)
    parser.add_argument("--max-output-tokens-total", type=int, default=None)
    parser.add_argument("--max-paper-tokens", type=int, default=60000)
    parser.add_argument("--max-output-tokens", type=int, default=8000)
    parser.add_argument("--allow-truncation", action="store_true")
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--no-digest", action="store_true")
    parser.add_argument("--allow-poor-extraction", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--vision-pages", choices=["off", "auto", "all"], default="auto",
                        help="Render page images for the host's multimodal model: off, auto (caption-only tables/figures), or all")
    parser.add_argument("--vision-max-pages", type=int, default=20)
    parser.add_argument("--vision-dpi", type=int, default=150)
    parser.add_argument("--blind-paper", action="store_true")
    parser.add_argument("--lenient-evidence", action="store_true", help="Warn instead of rejecting non-substring quotes")
    parser.add_argument("--no-early-stop", action="store_true")
    parser.add_argument("--force-unlock", action="store_true")
    parser.add_argument("--llm-config", default=None, help="Independent LLM API/key config file (never commit it)")
    parser.add_argument("--agent-backend", default=None,
                        help="Model the host agent itself is using (provider:model or host); defaults to auto-detection")
    parser.add_argument("--allow-backend-mismatch", action="store_true",
                        help="Allow a role to use a different model of the same provider as the agent (not recommended)")
    parser.add_argument("--reviewer-backends", default=None,
                        help="Comma-separated reviewer backends: host or qwen:model,glm:model,kimi:model,deepseek:model")
    parser.add_argument("--author-backend", default=None, help="Responder backend (host by default)")
    parser.add_argument("--digest-backend", default=None)
    parser.add_argument("--meta-backend", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hpe.py", description="HPE v2 evidence-anchored paper review")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Ingest a PDF and create a run")
    _add_init_arguments(init)

    nxt = sub.add_parser("next", help="Get ready task packets")
    nxt.add_argument("--run", required=True)
    nxt.add_argument("--limit", type=int, default=1)
    nxt.add_argument("--executor", choices=["host", "api", "all"], default="host")
    nxt.add_argument("--json", action="store_true")
    nxt.add_argument("--force-unlock", action="store_true")

    submit = sub.add_parser("submit", help="Validate and submit one task result")
    submit.add_argument("--run", required=True)
    submit.add_argument("--task", required=True)
    submit.add_argument("--file", required=True)
    submit.add_argument("--usage-json", default=None, help="Host-reported usage JSON")
    submit.add_argument("--check-only", action="store_true")
    submit.add_argument("--force", action="store_true")
    submit.add_argument("--json", action="store_true")
    submit.add_argument("--force-unlock", action="store_true")

    fail = sub.add_parser("fail", help="Mark a task failed")
    fail.add_argument("--run", required=True)
    fail.add_argument("--task", required=True)
    fail.add_argument("--reason", required=True)
    fail.add_argument("--json", action="store_true")
    fail.add_argument("--force-unlock", action="store_true")

    adv = sub.add_parser("advance", help="Advance to the next stage when the current one is complete")
    adv.add_argument("--run", required=True)
    adv.add_argument("--json", action="store_true")
    adv.add_argument("--force-unlock", action="store_true")

    stat = sub.add_parser("status", help="Show run status")
    stat.add_argument("--run", required=True)
    stat.add_argument("--json", action="store_true")

    ren = sub.add_parser("render", help="Render report.md / report.json")
    ren.add_argument("--run", required=True)
    ren.add_argument("--format", default="md,json")
    ren.add_argument("--json", action="store_true")
    ren.add_argument("--force-unlock", action="store_true")

    run = sub.add_parser("run", help="Run the whole protocol in API mode")
    _add_init_arguments(run)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-tasks", type=int, default=1000)

    bi = sub.add_parser("bilingual", help="Prepare or check Chinese/English review materials")
    bi.add_argument("--run", required=True)
    bi.add_argument("--check", action="store_true", help="Only verify that both language versions exist")
    bi.add_argument("--json", action="store_true")

    use = sub.add_parser("usage", help="Show the load-time usage hint")
    use.add_argument("--language", choices=["zh", "en"], default="zh")
    use.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor", help="Show runtime/dependency status")
    doc.add_argument("--json", action="store_true")

    llmcfg = sub.add_parser("init-llm-config", help="Create a private LLM API/key config template")
    llmcfg.add_argument("--path", default=None)
    llmcfg.add_argument("--json", action="store_true")

    legacy = sub.add_parser("legacy", help="Delegate to the v1 HPE-tool launcher")
    legacy.add_argument("args", nargs=argparse.REMAINDER)
    return p


def _run_init(args: argparse.Namespace) -> int:
    options = _build_init_options(args)
    out_dir = Path(args.out).expanduser().resolve() if args.out else default_run_dir(args.pdf)
    if args.force_unlock:
        _force_unlock(out_dir)
    result = init_run(
        pdf_path=Path(args.pdf),
        out_dir=out_dir,
        options=options,
        run_id=args.run_id,
        force=False,
    )
    _print(result, as_json=args.json)
    return EXIT_OK


def _run_next(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.force_unlock:
        _force_unlock(run_dir)
    result = next_tasks(run_dir, limit=max(1, args.limit), executor=args.executor)
    _print(result, as_json=args.json)
    return EXIT_OK


def _run_submit(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.force_unlock:
        _force_unlock(run_dir)
    usage = None
    if args.usage_json:
        try:
            usage = json.loads(args.usage_json)
        except json.JSONDecodeError as exc:
            raise HpeError(f"--usage-json is not valid JSON: {exc}") from exc
    result = submit_task(
        run_dir,
        task_id=args.task,
        payload_file=Path(args.file),
        usage=usage,
        check_only=args.check_only,
        force=args.force,
    )
    _print(result, as_json=args.json)
    return EXIT_OK


def _run_fail(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.force_unlock:
        _force_unlock(run_dir)
    _print(fail_task(run_dir, task_id=args.task, reason=args.reason), as_json=args.json)
    return EXIT_OK


def _run_advance(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.force_unlock:
        _force_unlock(run_dir)
    _print(advance(run_dir), as_json=args.json)
    return EXIT_OK


def _run_status(args: argparse.Namespace) -> int:
    _print(status(Path(args.run)), as_json=args.json)
    return EXIT_OK


def _run_render(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.force_unlock:
        _force_unlock(run_dir)
    formats = [item.strip() for item in args.format.split(",") if item.strip()]
    unknown = sorted(set(formats) - {"md", "json", "html"})
    if unknown or not formats:
        raise HpeError(f"--format must contain md, json or html; got {unknown or 'nothing'}")
    _print(render(run_dir, formats=formats), as_json=args.json)
    return EXIT_OK


def _run_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.out).expanduser().resolve() if args.out else default_run_dir(args.pdf, resume=True)
    if args.mode not in {"api", "hybrid"}:
        raise HpeError("`run` executes the built-in API loop; use init/next/submit for host mode.")
    if args.force_unlock:
        _force_unlock(run_dir)
    if not (run_dir / "run.json").is_file():
        options = _build_init_options(args)
        init_run(pdf_path=Path(args.pdf), out_dir=run_dir, options=options, run_id=args.run_id)
    if args.dry_run:
        _print({"run_dir": str(run_dir), "message": "dry run: initialized run only; no model calls were made"}, as_json=args.json)
        return EXIT_OK
    result = run_api(run_dir=run_dir, provider_name=args.provider, model_name=args.model, max_tasks=args.max_tasks)
    if result.get("action") == "host_tasks":
        result["next"] = "Host tasks remain. Run `hpe.py next --executor host`, execute them, then rerun `hpe.py run`."
    else:
        rendered = render(run_dir, formats=("md", "json"))
        result.update({"rendered": rendered})
    _print(result, as_json=args.json)
    return EXIT_OK


def _run_bilingual(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).expanduser().resolve()
    if args.check:
        result = check_bilingual_files(run_dir)
    else:
        result = prepare_bilingual_files(run_dir)
    _print(result, as_json=args.json)
    return EXIT_OK


def _usage_payload(language: str) -> dict[str, Any]:
    if language == "en":
        return {
            "skill": "hpe-paper-evaluator-v2",
            "title": "HPE Paper Evaluator v3.1 — usage hint",
            "summary": "Evidence-anchored multi-reviewer review, detailed summary, or problem finding for a local PDF.",
            "required": ["A local PDF path"],
            "default_output": "<PDF directory>/<PDF filename>.hpe/ — all intermediate artifacts and final materials live there: report.md|json, report-zh/en.md, review-rounds-zh/en.md, final-opinion-zh/en.md.",
            "options": {
                "purpose": "review (default) | summary | problem_finding",
                "material": "paper (default) | proposal | design",
                "domain": "ai-ict (default) | materials | mechanical | auto | custom",
                "language": "auto (default; follows the paper) | zh | en",
                "reviewers": "methodology,experiments,novelty (default three)",
                "rounds": "review/problem_finding default to at least 1 cross-review round; explicit 0 is review+meta only",
                "vision": "off | auto (default) | all — page images for multimodal table/figure reading",
            },
            "example": "python scripts/hpe.py init --pdf /path/paper.pdf --language en --purpose review",
            "next": "Send me the PDF path. Tell me the purpose/language/domain if you want non-default behavior; tell me an output directory only if you want to override the default next to the PDF.",
        }
    return {
        "skill": "hpe-paper-evaluator-v2",
        "title": "HPE Paper Evaluator v3.1 — 使用提示",
        "summary": "面向本地 PDF 的证据锚定、多审稿人评审 / 详细总结 / 问题查找 skill。",
        "required": ["本地 PDF 路径"],
        "default_output": "PDF 所在目录 / <PDF文件名>.hpe/ —— 所有中间文件和最终物料都在这里：report.md|json、report-zh/en.md、review-rounds-zh/en.md、final-opinion-zh/en.md。",
        "options": {
            "purpose": "review（默认）| summary | problem_finding",
            "material": "paper（默认）| proposal | design",
            "domain": "ai-ict（默认）| materials | mechanical | auto | 自定义",
            "language": "auto（默认，跟随论文）| zh | en",
            "reviewers": "methodology,experiments,novelty（默认三位）",
            "rounds": "review/problem_finding 默认至少 1 轮交叉复审；显式 0 为独立审稿 + 元审稿",
            "vision": "off | auto（默认）| all —— 为表格/图渲染页面图，供多模态读取",
        },
        "example": "python scripts/hpe.py init --pdf /path/paper.pdf --language zh --purpose review",
        "next": "请直接给我 PDF 路径。如需非默认行为，再说明 purpose/language/domain；只有想覆盖默认位置时才需要指定输出目录。",
    }


def _print_usage(payload: dict[str, Any]) -> None:
    print(payload["title"])
    print("=" * len(payload["title"]))
    print(payload["summary"])
    print()
    print("必需 / Required:")
    for item in payload["required"]:
        print(f"  - {item}")
    print()
    print("默认输出 / Default output:")
    print(f"  {payload['default_output']}")
    print()
    print("常用选项 / Options:")
    for key, value in payload["options"].items():
        print(f"  - {key}: {value}")
    print()
    print("示例 / Example:")
    print(f"  {payload['example']}")
    print()
    print(payload["next"])


def _run_usage(args: argparse.Namespace) -> int:
    payload = _usage_payload(args.language)
    if args.json:
        _print(payload, as_json=True)
    else:
        _print_usage(payload)
    return EXIT_OK


def _run_doctor(args: argparse.Namespace) -> int:
    info = ingest_doctor()
    info["prompt_version"] = PROMPT_VERSION
    try:
        info["domains"] = sorted(load_domains())
    except Exception as exc:  # doctor must remain usable even with a broken config
        info["domains_error"] = str(exc)
    try:
        config = load_llm_config()
        info["llm_config"] = config.status()
    except Exception as exc:
        info["llm_config_error"] = str(exc)
    try:
        info["agent_backend"] = detect_agent_identity().to_dict()
    except Exception as exc:  # doctor must remain usable
        info["agent_backend_error"] = str(exc)
    _print(info, as_json=args.json)
    return EXIT_OK


def _run_init_llm_config(args: argparse.Namespace) -> int:
    path = init_llm_config(args.path)
    _print({"path": str(path), "mode": "0600", "note": "Fill api_key fields locally; never commit this file."}, as_json=args.json)
    return EXIT_OK


def _run_legacy(args: argparse.Namespace) -> int:
    from .legacy import run_legacy

    forwarded = list(args.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    return run_legacy(forwarded)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            return _run_init(args)
        if args.command == "next":
            return _run_next(args)
        if args.command == "submit":
            return _run_submit(args)
        if args.command == "fail":
            return _run_fail(args)
        if args.command == "advance":
            return _run_advance(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "render":
            return _run_render(args)
        if args.command == "run":
            return _run_run(args)
        if args.command == "bilingual":
            return _run_bilingual(args)
        if args.command == "usage":
            return _run_usage(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "init-llm-config":
            return _run_init_llm_config(args)
        if args.command == "legacy":
            return _run_legacy(args)
        raise HpeError(f"Unknown command {args.command!r}")
    except HpeError as exc:
        payload = exc.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
            if exc.detail:
                print(json.dumps(exc.detail, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return EXIT_CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())
