---
name: hpe-paper-evaluator-v2
description: Evidence-anchored multi-reviewer review, detailed summary, or problem finding for local PDFs (papers, proposals, project/experiment designs). Deterministic host task loop, structured issue ledger, author rebuttal and cross-review, meta-review decision, domain cards (AI/CS/ICT, metals/high-entropy alloys, mechanical), host or external LLM backends (Qwen/GLM/Kimi/DeepSeek), Chinese/English report output, optional page images for multimodal table/figure reading, and Markdown/JSON reports. Use when the user asks for HPE evaluation, a structured paper review, proposal/design review, a detailed paper/proposal summary, logic/technical/innovation loophole finding, multi-reviewer debate, or a review with page-level evidence.
---

## Load-time usage hint (read this first)

When this skill is loaded — for example after OpenCode `/skills`, `@hpe-paper-evaluator-v2`,
Codex `$hpe-paper-evaluator-v2`, or an explicit `skill` tool call — do **not** start a run immediately. First present a
short usage card (or run `python scripts/hpe.py usage --language zh --json` and render it):

1. Required input: a **local PDF path**.
2. Default output: **`<PDF directory>/<PDF filename>.hpe/`** — all intermediate
   artifacts live next to the reviewed paper. Review mode produces:
   - `output/report.md` / `report.json` — final report;
   - `output/report-zh.md` and `report-en.md` — final report in both languages;
   - `output/review-rounds-zh.md` and `review-rounds-en.md` — every reviewer's
     evaluation and the author agent's responses for every round;
   - `output/final-opinion-zh.md` and `final-opinion-en.md` — final opinion in
     both languages.
3. Optional choices: `purpose` (review/summary/problem_finding), `language`
   (auto/zh/en), `domain`, `material`, `reviewers`, `rounds`.
4. If the user did not specify an output directory, **do not ask for one**; use
   the default next to the PDF. Ask only if they want an override.
5. End the hint by asking for the PDF path and any non-default options.

If the user has already supplied a PDF path in the same message, skip the card
and start the workflow.

# HPE Paper Evaluator v3.1

Review a local PDF with a deterministic protocol: the script ingests and sections the paper, emits one task packet at a time, validates every JSON artifact, merges duplicate issues, enforces a decision policy, and renders the report. LLM work happens in the host agent or one fresh subagent per task; the script itself never calls a model in host mode.

## When to use

- The user explicitly asks for HPE, HPE-tool, structured paper review, multi-reviewer review, cross-review, or model debate.
- The user supplies a local PDF (or a path to one) and wants a review, a detailed summary, a problem/loophole analysis, scores/decision, or a revision list.
- The review must be auditable: page-anchored evidence, an issue ledger, preserved dissent.
- The material is a paper, a research proposal, or a project/experiment design, in AI/CS/ICT, metals/high-entropy alloys, mechanical engineering, or a registered custom domain.

Do not use for:

- plagiarism, citation-database lookup, or misconduct detection;
- pure translation or polishing (summary mode is a structured analytical summary, not a translation);
- papers without an accessible local PDF (ask for the file rather than inventing content).

## Core principles

1. **Ingest first, review second.** Never review from a summary alone.
2. **Round 0 is independent; cross-review comes later.** Do not show a reviewer the other reviewers' round-0 opinions.
3. **Evidence first.** Every issue needs a physical PDF page and a contiguous quote, or an explicit `absence` record.
4. **The script owns state.** The host never edits `state.json`, `ledger.json`, or the report by hand.
5. **Fresh context per reviewer.** Use one fresh subagent per reviewer task. Running all reviewers in one conversation breaks blind review.
6. **Explicit failure.** A failed task is marked failed; never write an empty template and call it success.
7. **The decision policy is deterministic.** The meta reviewer explains and may override only with a written reason.

## Inputs

- Required: a local PDF path.
- Optional:
  - output directory: `--out`; default `<PDF directory>/<PDF filename>.hpe/`;
  - purpose: `review` (default), `summary`, or `problem_finding`;
  - material: `paper` (default), `proposal`, or `design`;
  - domain: `ai-ict` (default), `materials`, `mechanical`, `auto`, or a custom id from `config/domains.json`;
  - profile: `conference` (default), `journal`, `proposal`, `experiment`, or `paper`;
  - reviewers: default from the profile/domain; choose personas, not model brands;
  - rounds: `--rounds` / `--debate-rounds`; default from the profile, and review/problem_finding default to at least 1 cross-review round (author response included); explicit `--rounds 0` is independent review + meta only;
  - author/responder: `simulated` (default), `provided`, or `none`;
  - backends: `host` by default; external `qwen:model`, `glm:model`, `kimi:model`, `deepseek:model`;
  - agent backend: `--agent-backend provider:model` records the LLM the host agent is using; auto-detected in OpenCode and DeepSeek Harness where possible;
  - language: `auto`, `zh`, or `en`; `auto` follows the paper, `zh`/`en` force the report and review prose;
  - vision: `--vision-pages off|auto|all` renders page images for the host's multimodal reader (default `auto`: pages with caption-only tables/figures);
  - isolation: `subagent` (default), `session`, or `unknown`;
  - budget and context limits.

## Purposes and materials

| purpose | Pipeline | Output |
|---|---|---|
| `review` | digest → 3 independent reviewers → rebuttal → cross-review → ledger → meta | review report + scores/decision |
| `problem_finding` | same pipeline; reviewer prompt prioritizes logic/technical/innovation loopholes; highlights are context, not an excuse | problem-finding report + prioritized fixes |
| `summary` | digest → one detailed summary task | `output/summary.md` and `output/summary.json`; no ledger/decision |

`material=paper|proposal|design` selects the preserved upstream 20/12/14-section summary structure and the reviewer role card. A proposal/design is assessed for feasibility, resources, milestones, risk and evidence, not for "experiments" as if it were a completed paper.

## Domains

- `ai-ict` (default): baselines, matched compute, statistics, leakage, reproducibility, LLM-specific pitfalls.
- `materials`: metals and high-entropy alloys; composition → processing → microstructure → property → service chain; XRD/SEM/TEM/EBSD, mechanical-test standards, phase stability, characterization and batch variation.
- `mechanical`: load cases, boundary conditions, material model, FEA validation, fatigue/fracture, manufacturing tolerance, safety basis.
- `auto`: keyword-based detection over the first 60k characters; falls back to `ai-ict`. The chosen domain is recorded in `run.json`.
- To add a domain: add `prompts/domains/<id>.md` and a `config/domains.json` entry; no Python change required.

Reviewers must find **innovation highlights** (what is genuinely new), **technical highlights** (new mechanism/characterization/design), and **significance/methodological highlights**, then focus the issue list on logic, technical and innovation loopholes. Empty highlights are allowed only with a reason and produce a warning.

## LLM backends and keys

Host mode uses the current host LLM for every role; the same model imitates different reviewer/responder personas through fresh subagents. API mode uses role-specific external providers:

```bash
python scripts/hpe.py init-llm-config          # writes ~/.hpe-paper-evaluator/llm_apis.json (0600)
python scripts/hpe.py init ... --mode api \
  --reviewer-backends qwen:qwen3-max,glm:glm-4-plus,kimi:kimi-k2-0905-preview \
  --author-backend glm:glm-4-plus --digest-backend qwen:qwen3-max --meta-backend kimi:kimi-k2-0905-preview
```

- Qwen, GLM, Kimi and DeepSeek are OpenAI-compatible and live in the independent `llm_apis.json`; `config/models.json` remains a legacy fallback.
- Keys are read only from the LLM config (literal key or the named env fallback). Never pass a key on the command line, log it, put it in a prompt, or commit it.
- `--mode host`: all roles host; `--mode api`: all roles external; `--mode hybrid`: mix (host tasks are returned as `action=host_tasks` for the host loop).
- API mode without a configured key exits 3; the error names the provider/env variable only.

### Agent-base consistency (v3.1)

The review base must be the same model as the host agent by default.

- **Host mode** is the safest path: every role runs on the current agent model, without a second API key.
- **API mode** now inherits the agent model. The script detects the current model from `--agent-backend`, `HPE_AGENT_BACKEND`, OpenCode's session API, Codex (`CODEX_MODEL` or `~/.codex/config.toml`), DeepSeek Harness env (`DEEPSEEK_HARNESS_MODEL`), or `~/.hpe-paper-evaluator/agent.json`. It never silently falls back to a vendor default such as `deepseek-chat`.
- If a role resolves to a **different model of the same vendor** (for example agent `deepseek-v4.1-flash`, reviewer `deepseek-chat`), `init` refuses to start unless the user passes `--allow-backend-mismatch`. A different vendor is allowed but recorded as an informational warning.
- `run.json` records `agent`, `backends.*`, and `backend_consistency`; the report shows the agent backend and any mismatch.
- If the agent model is detected but its provider is not configured in `llm_apis.json`, API mode stops with instructions: use host mode or add that provider/model to the independent key file. It does not substitute another model.

### Output language (v3.1)

- `--language auto` (default) follows the paper language: a Chinese paper produces Chinese reviews and a Chinese report; an English paper produces English.
- `--language zh` forces Chinese prose, report headings, status labels and summary headings even when the paper is English.
- `--language en` forces English.
- JSON field names and enum values stay English for contract stability. Quotations keep the paper's original language. The resolved language is stored as `run.json:output_language` and `report.json:output_language`.

### PDF extraction and page images

- The deterministic Python extractor is always the canonical text/evidence layer: page markers, section ranges and `QuoteIndex` validation depend on it.
- `--vision-pages auto` renders PNGs for **every page containing a table or figure**, prioritizing `caption_only`/`unknown` over `text_maybe`; `all` renders every page up to `--vision-max-pages`; `off` disables. The host's multimodal reader can inspect these images to recover table cells, axes, equations or scanned text.
- The evidence contract does not relax: a `paper` quote must still exist in extracted text. Image-only observations go in the evidence `note`; reviewers must not invent numbers or quotes.
- Record the physical pages actually inspected as images in `coverage.images_read` (review) or `images_read` (summary). If the host/model cannot read images, use `[]`, mark table/figure cells as not extractable, put the gap in `missing_evidence`/`missing_information`, and lower confidence. This is the **text-only fallback**. `--ocr` remains the optional fallback for scanned pages when tesseract is installed.
- Python extraction remains the canonical evidence layer. Do not replace it with an image-only pipeline: it would destroy page-level quote verification and reproducibility.

## Workflow A: host mode (default)

The host already runs a model; no API key is required.

### A1. Initialize

```bash
python scripts/hpe.py init \
  --pdf "<PDF_PATH>" \
  --purpose review \
  --material paper \
  --domain ai-ict \
  --profile conference \
  --reviewers methodology,experiments,novelty \
  --rounds 2 \
  --language auto \
  --isolation subagent
```

`--out` is **optional**. When it is omitted, the run directory is
**`<PDF directory>/<PDF filename>.hpe/`** (or a timestamped sibling if that
directory already exists). Pass `--out "<directory>"` only when the user
explicitly asks for a different location. This contract is **host-neutral**:
OpenCode, Codex CLI, and DeepSeek Harness must all use the same default; host
adapters must not inject `--out` on their own. Always pass an absolute PDF path
so the default run directory is created next to the paper.

The command writes `run.json`, `state.json`, `paper/manifest.json`, section files and page texts. It refuses to overwrite an existing run.

If extraction quality is `poor`, the run stops unless the user explicitly passes `--allow-poor-extraction` or `--ocr`. Do not override this silently.

### A2. Run the task loop

```bash
# Default run directory next to the PDF; use your --out value if you overrode it.
RUN="<PDF_DIR>/<PDF_STEM>.hpe"
python scripts/hpe.py next --run "$RUN" --executor host --json --limit 4
```

The response has `action`:

- `execute`: `tasks[]` contains task packets. For each task:
  1. Resolve `prompt_path` relative to the run directory and pass its **entire content** to a fresh subagent (or to the host if only one task and the host supports it).
  2. Tell the subagent to write exactly one JSON object matching `schema_path` to `output_path` (relative to the run directory).
  3. Submit:
     ```bash
     python scripts/hpe.py submit --run "$RUN" --task "T-0007" --file "<output_path>"
     ```
  4. If the subagent cannot finish, mark it:
     ```bash
     python scripts/hpe.py fail --run "$RUN" --task "T-0007" --reason "timeout"
     ```
- `advance`: the current stage is complete; run:
  ```bash
  python scripts/hpe.py advance --run "$RUN"
  ```
- `wait`: a dependency is still pending; do not spin. Check `status` and finish the pending task.
- `done`: render and report.
- `failed`: report the coverage/failure reason; do not present a partial run as complete.

Repeat `next` until `advance` reports `stage=rendered` or the loop is done.

**Dispatch rules**

- `--limit N` returns independent tasks only; dispatch them in parallel only when the host supports parallel subagents and each gets a fresh context.
- Tasks with `depends_on` must wait; `optional_depends_on` are recorded for context, not as a hard gate.
- The prompt is self-contained. Do not paraphrase, summarize, shrink or "improve" it; the prompt hash is recorded.
- The reviewer must not see another reviewer's live output. If the host cannot create fresh contexts, pass `--isolation session` and treat blind review as compromised.
- For DeepSeek Harness, use one fresh child agent per task when available. OpenCode can use a `general` subagent per task. Codex can use a separate `codex exec` process/session per task. If none is available, run tasks serially in one session but state the limitation.
- A subagent must not call `submit` or `advance`; only the host does.

### A3. Render

```bash
python scripts/hpe.py render --run "$RUN" --format md,json
# If render returns bilingual.tasks, execute each prompt with a fresh subagent, then:
python scripts/hpe.py bilingual --run "$RUN" --check --json
```

Report to the user:

- `output/report.md` and `output/report.json` paths;
- `output/report-zh.md` / `report-en.md`,
  `output/review-rounds-zh.md` / `review-rounds-en.md`, and
  `output/final-opinion-zh.md` / `final-opinion-en.md`;
- recommendation and policy decision, override/provisional status;
- overall score and the major open issues;
- reviewer coverage and any failed tasks;
- profile, rounds, author mode, isolation warning;
- every warning (extraction, injection, budget, dissent).

### Review-mode deliverables

For `purpose=review` or `problem_finding`, `render` writes:

- `report.md` / `report.json` — main decision report (scores, issue ledger,
  required revisions);
- `report-zh.md` and `report-en.md` — the final report in both languages;
- `review-rounds-zh.md` and `review-rounds-en.md` — **every reviewer's opinion
  in every round** plus the **author agent's rebuttal for each round**
  (including the simulated author), in both languages;
- `final-opinion-zh.md` and `final-opinion-en.md` — the final consolidated
  review opinion in both languages.

Default review runs use **at least one cross-review round** (`rounds >= 1`)
unless the user explicitly passes `--rounds 0`. The bilingual files are
produced by the mandatory post-render translation step:

1. `render` mirrors the primary language and writes
   `output/bilingual-tasks.json` plus `prompts/bilingual-*.prompt.md`.
2. For each task, start a fresh subagent with the prompt; it translates the
   source Markdown and writes the target file.
3. Verify with `python scripts/hpe.py bilingual --run "<RUN>" --check --json`.
   Do not report the run as complete while `ok=false`.

`report.json` also carries `review_rounds`, `rebuttal_rounds`,
`final_opinion`, and `artifacts.bilingual` for downstream tooling. Round 0 is
always the independent blind round; later rounds include `issue_updates`,
`position_changes` and `remaining_concerns`.

## Workflow B: API mode

For CLI/CI/batch runs when keys are configured in the independent LLM config:

```bash
python scripts/hpe.py run \
  --pdf "<PDF_PATH>" \
  --mode api \
  --purpose review --material paper --domain ai-ict \
  --reviewer-backends qwen:qwen3-max,glm:glm-4-plus,kimi:kimi-k2-0905-preview \
  --author-backend glm:glm-4-plus \
  --reviewers methodology,experiments,novelty \
  --rounds 2 --budget-usd 3.0
```

`run` performs `init`, the task loop, and rendering. It records real usage when the provider returns it. API mode requires external backends for every role; use `--mode hybrid` if the host should remain the responder, and then finish host tasks with `next --executor host`. Without a configured key, do not claim API mode works.

## Workflow C: legacy mode

```bash
python scripts/hpe.py legacy -- --file "<PDF_PATH>" --analysis-mode paper_summary --summary-api deepseek
```

This forwards to the v1 `hpe_runner.py` when it is available (sibling checkout or `HPE_LEGACY_RUNNER`). It produces the old 20-section Markdown, not the v2 ledger. Never present legacy output as a v2 structured review.

## Reviewer selection

| Need | Reviewers |
|---|---|
| General paper | `methodology,experiments,novelty` |
| Theory/algorithm | `methodology,novelty,devils_advocate` |
| Experimental/system | `experiments,reproducibility,practitioner` |
| Research proposal | `methodology,novelty,practitioner` |
| Experiment design | `experiments,reproducibility,devils_advocate` |
| High-risk/controversial | add `devils_advocate` |

Default to three reviewers. More reviewers multiply cost and do not automatically improve the decision. `--rounds 0` is the fastest useful mode.

## Failure handling

| Situation | Action |
|---|---|
| PDF missing / not a PDF | Stop; report the path |
| Poor extraction | Stop unless the user explicitly overrides; report the quality score |
| `submit` validation error | Fix the JSON once; if it still fails, report exit 4 and the raw response path |
| One reviewer fails | Continue; coverage becomes N-1/N and the decision is provisional |
| Coverage < 50% | The run is failed; no "complete" report |
| Budget exceeded | Stop at a safe point; resume with a larger budget |
| Host crash | `hpe.py status` then `next`; stale running tasks are reclaimed |
| Meta output invalid | Retry; if it still fails, `advance` writes a deterministic policy-only decision and marks it provisional |

## Credentials

- Preferred: the independent `~/.hpe-paper-evaluator/llm_apis.json` (or `--llm-config`), created with `init-llm-config`; file mode 0600.
- Providers: Qwen, GLM, Kimi, DeepSeek (plus legacy `config/models.json` providers). Environment-variable fallbacks are optional and named in the config.
- Never put a key on the command line, in a run directory, in a report, or in a prompt.
- Missing key: report only the provider/env variable name; never ask the user to paste the secret.

## Report requirements

The final answer must include the report paths, output language, recommendation/policy status, overall score, open blocking/major issues, highlights, reviewer coverage, rounds/purpose/material/domain/backend, agent backend and any backend-consistency warning, isolation warning when applicable, and the disclaimer that model output requires human verification. For review/problem_finding, also report `output/report-zh.md` / `report-en.md`, `output/review-rounds-zh.md` / `review-rounds-en.md` (all reviewer rounds + author replies), and `output/final-opinion-zh.md` / `final-opinion-en.md`; run `hpe.py bilingual --run <run> --check` before claiming completion. For `summary`, report `output/summary.md`/`summary.json`, not a fake decision.

## Runtime boundaries

- Do not modify the upstream HPE-tool source.
- Do not write the paper full text into public logs; `--redact`-style export is not implemented yet, so treat run directories as private.
- Do not overwrite an existing run. With the default output path, a repeated
  run automatically gets a timestamped sibling directory; use `--out` only when
  the user asks for a specific location.
- The report is model-assisted and is not a peer-review decision.
