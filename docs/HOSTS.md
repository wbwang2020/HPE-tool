# Host adapters: OpenCode, Codex CLI, and DeepSeek Harness

The v3 protocol is host-neutral. The same shell commands and the same JSON
contracts work in OpenCode, Codex CLI, and DeepSeek Harness; only the way a
host starts a fresh subagent differs.

## Installation

The skill is a directory containing `SKILL.md` plus scripts, prompts, schemas
and config. No package installation is required; Python 3.10+ and the standard
library are enough. For best extraction install `pymupdf` and `jsonschema`.

### OpenCode V2

OpenCode discovers a directory skill at `.opencode/skills/<id>/SKILL.md`
(project) or `~/.config/opencode/skills/<id>/SKILL.md` (global). The directory
name is the callable skill ID.

```bash
# Global install (recommended for one user)
ln -s "/path/to/hpe-paper-evaluator-v2" ~/.config/opencode/skills/hpe-paper-evaluator-v2

# Project install
mkdir -p .opencode/skills
ln -s "/path/to/hpe-paper-evaluator-v2" .opencode/skills/hpe-paper-evaluator-v2

# Verify the link and the CLI
readlink -f ~/.config/opencode/skills/hpe-paper-evaluator-v2
python scripts/hpe.py doctor --json
```

OpenCode advertises the skill on the next model step. If it is not listed yet,
start a new session or mention `@hpe-paper-evaluator-v2` explicitly. The
`skill` tool loads the directory form and reports its base directory.

On Windows, use a directory junction (`mklink /J`) or copy the directory if a
symlink is not available. Keep the skill directory as the shell working
directory so relative `scripts/`, `prompts/`, `schemas/` paths resolve.

### DeepSeek Harness

Point the manifest at `scripts/hpe.py`, keep the skill directory as the working
directory, and use the `agents/deepseek.yaml.example` workflow. The stable
contract is the CLI, not the YAML field names. If the Harness exposes its model
through `DEEPSEEK_HARNESS_MODEL` (or you set `HPE_AGENT_BACKEND`), the skill
records that as the agent base and refuses a same-vendor/different-model API
default.

## Agent-base detection

The skill tries these sources in order:

1. `--agent-backend provider:model`
2. `HPE_AGENT_BACKEND`
3. `HPE_AGENT_PROVIDER` + `HPE_AGENT_MODEL`
4. `DEEPSEEK_HARNESS_MODEL`
5. OpenCode `OPENCODE_SESSION_ID` → `opencode api get /api/session/<id>`
6. `~/.hpe-paper-evaluator/agent.json` (`{"provider": "...", "model": "..."}`)

If nothing is detected, host mode still runs on the current agent. API mode
requires an explicit backend or a configured agent identity; it never silently
uses a vendor default. Use `python scripts/hpe.py doctor --json` to inspect the
detected identity.

## Default output location

`--out` is optional. When omitted, the run directory is
`<PDF directory>/<PDF filename>.hpe/` (a timestamped sibling is used if it
already exists). All intermediate artifacts live there. Review mode also
writes `output/report.md|json`, `output/report-zh.md` / `report-en.md`,
`output/review-rounds-zh.md` / `review-rounds-en.md`, and
`output/final-opinion-zh.md` / `final-opinion-en.md`.
Pass `--out` only when the user explicitly asks for a different location.

## Load-time usage hint

When the skill is loaded, show the usage card before starting work. The
deterministic version is:

```bash
python scripts/hpe.py usage --language zh
python scripts/hpe.py usage --language en --json
```

It states the required PDF path, the default output location next to the PDF,
and the optional purpose/language/domain/reviewers/rounds choices. Do not ask
for an output directory unless the user wants to override the default.

## Shared host contract

1. `init` once, then loop:
   - `next --run <run> --executor host --json`
   - if `action=execute`: for each packet, start a **fresh** subagent/session,
     give it the entire `prompt_path` content (verbatim), and tell it to write one
     JSON object to `output_path` matching `schema_path`;
   - `submit --run <run> --task <task_id> --file <output_path>`;
   - if `action=advance`: `advance --run <run>`;
   - if `action=done`: read `output/report.md` (review/problem finding) or
     `output/summary.md` (summary mode).
2. Never edit `state.json`, `ledger.json`, run artifacts, or the report by hand.
3. One fresh context per reviewer task. Do not pass other reviewers' round-0
   opinions to a round-0 reviewer.
4. In review mode, `render` also returns `bilingual.tasks`. Execute each
   translation prompt with a fresh subagent (or the API provider), write the
   target Markdown, then run `hpe.py bilingual --run <run> --check`. Do not
   report completion until `ok=true`. Expected bilingual files:
   `review-rounds-zh.md`, `review-rounds-en.md`, `final-opinion-zh.md`,
   `final-opinion-en.md`.
5. If a subagent fails, call `fail`; do not write an empty template.
6. In host mode the current host LLM is the default for all roles. The same
   model can play three reviewer personas and the author via separate fresh
   subagents; do not merge them into one context.
7. If the user asks for Chinese output, pass `--language zh` at `init`; do not
   translate the prompt or the JSON by hand. The script records the resolved
   language and renders Chinese report headings/status labels.
8. The review base defaults to the host-agent model. In API mode, pass
   `--agent-backend` or rely on detection; a same-vendor/different-model default
   is refused unless the user explicitly passes `--allow-backend-mismatch`.
9. `--vision-pages auto` (default) renders page images for every page containing
   a table or figure (prioritizing caption-only/unknown over text_maybe). Give
   the subagent the image paths from the task packet `context.vision.page_images`;
   the image is supplementary, not a replacement for text quotes. Ask the model
   to fill `coverage.images_read` (review) or `images_read` (summary). If it
   cannot read images, it must use `[]`, mark cells not extractable, and lower
   confidence.

## Codex CLI

Codex 0.163+ uses the Agent Skills layout: `$CODEX_HOME/skills/<skill-id>/SKILL.md`
(default `~/.codex/skills/`). The skill can be installed as a symlink or copy.

```bash
ln -sfn "/path/to/hpe-paper-evaluator-v2" ~/.codex/skills/hpe-paper-evaluator-v2

# Verify discovery without starting an agent session
codex debug prompt-input | grep hpe-paper-evaluator-v2
```

`agents/openai.yaml` is included for Codex UI metadata. Invoke the skill with
`$hpe-paper-evaluator-v2` or mention it in the prompt.

The init command is the same as OpenCode; **do not pass `--out`** unless the
user asks for a different location:

```bash
RUN="<PDF_DIR>/<PDF_STEM>.hpe"
python scripts/hpe.py init --pdf "<ABS_PDF>" --purpose review --language zh
python scripts/hpe.py next --run "$RUN" --executor host --json --limit 4
```

For Codex, write permission to the PDF directory is required. A per-task
`codex exec` worker can be launched from the host:

```bash
codex exec --sandbox workspace-write --add-dir "<PDF_DIR>" \
  -C "<SKILL_DIR>" \
  "Read the entire <RUN>/prompts/T-0007.prompt.md, write exactly one JSON object to <RUN>/<output_path>, and do not call hpe.py submit/advance."
```

### Codex host contract

- **Fresh context**: Codex has no OpenCode-style `subagent` tool. For blind
  review, run each task packet in a separate `codex exec` process/session (or
  a separate Codex session). If only one session is available, run serially and
  pass `--isolation session`; the report will warn that independence is
  compromised.
- **Sandbox / writes**: the run directory is next to the PDF and may be outside
  the Codex workspace. Use `--sandbox workspace-write --add-dir "<PDF_DIR>"`
  (or `danger-full-access` for a fully trusted local run). The skill must write
  `paper/`, `prompts/`, `artifacts/`, and `output/`.
- **Images**: Codex exposes an image-viewing tool (`view_image`) and accepts
  `-i/--image` on `codex exec`. Page images are generated after `init`, so the
  in-session image viewer is the natural path; if unavailable, set
  `images_read=[]`, mark table/figure cells not extractable, and lower
  confidence (text-only fallback).
- **Auth**: Codex needs `codex login` (or a supported auth env variable) before
  an agent session. The skill itself needs no key in host mode.
- **Model identity**: the skill reads `CODEX_MODEL` or `~/.codex/config.toml`
  (`model = "..."`) when available, and records `openai:<model>` as the agent
  base. If no model is configured, host mode still runs but the identity is
  reported as unknown.

**Compatibility status**: Codex 0.163.0 discovers the skill statically
(`codex debug prompt-input` lists `r0/hpe-paper-evaluator-v2/SKILL.md`). A live
Codex agent loop was not executed in this environment because no Codex
credentials were configured.

## DeepSeek Harness

- Use the same host-neutral init command as OpenCode/Codex; **do not pass
  `--out`** unless the user explicitly asks for a different location:

  ```bash
  RUN="<PDF_DIR>/<PDF_STEM>.hpe"
  python scripts/hpe.py init --pdf "<ABS_PDF>" --purpose review --language zh
  python scripts/hpe.py next --run "$RUN" --executor host --json --limit 4
  ```

- Use the `subagent` tool (or `subagent_fork` only when the task explicitly
  needs the parent conversation; reviewer tasks do not).
- Give each child the prompt path, output path, schema path and the instruction
  "do not run `submit`/`advance`; only write the JSON file".
- Run `next` with `--limit 1` or a small limit; collect each task before
  submitting. Start independent review tasks in parallel if the host supports it.
- Use `--isolation subagent` and `--mode host`. No API key is required.
- The `agents/deepseek.yaml.example` file is a starting point for a manifest;
  field names may differ across Harness versions.

## OpenCode

- Use OpenCode's `task` tool to create a fresh worker per packet; use a primary
  agent for the deterministic shell loop. If your version only supports one
  session, run tasks serially and start a new session for each packet.
- Keep the skill directory as the working directory; pass absolute run paths.
- Use `agents/opencode.yaml.example` as a starting point. The important fields
  are `fresh_context_per_task`, `read_prompt_verbatim` and the bash allowlist.
- Do not enable plan mode for the worker; it must write the JSON artifact.
- The worker must not call `submit`/`advance`; the primary agent owns state.

## API / hybrid modes

- `--mode api` executes the whole loop through external OpenAI-compatible
  providers. Configure Qwen, GLM, Kimi and DeepSeek keys in
  `~/.hpe-paper-evaluator/llm_apis.json` (or `--llm-config`), never in the
  command line or a run artifact.
- `--mode hybrid` can mix host and API reviewers. `hpe.py run --mode hybrid`
  executes API tasks and returns `action=host_tasks` when a host task is next;
  the host then runs `next --executor host`, finishes those tasks, and can rerun
  `run` to advance/render. This is intentionally a manual loop.
- Role-specific backends: `--reviewer-backends host,host,qwen:qwen3-max`,
  `--author-backend glm:glm-4-plus`, `--digest-backend kimi:kimi-k2-0905-preview`.

## Minimal changes for a new host

Only three host capabilities are required:

1. run a shell command (the CLI);
2. spawn a fresh LLM context and pass it a text prompt;
3. write the model's JSON output to a file.

If a host has those, it can run the skill without code changes. The adapter YAML
is documentation; the CLI is the stable interface.
