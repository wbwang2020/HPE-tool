# Upstream HPE-tool prompt provenance

Source: https://github.com/wbwang2020/HPE-tool
Commit: ab0b5610e1727ce1b8b4a57025b69c10ffd99b6c (2026-07-16)
Upstream file: `src/prompts/prompt_templates.py` (717 lines)

These files preserve the *structure and analysis requirements* of the upstream
prompts (20-section paper summary, 12-section research proposal, 14-section
experiment design, and the four-category debate checklist). The v3 skill wraps
them in its evidence-anchored JSON protocol; it does not copy upstream's
free-text-only output or its "summary-only" reviewer setup.

Modifications:
- placeholders/JSON wrapper added for the v3 summary task;
- evidence/page requirements added by `prompts/00-common.md`;
- wording updated for proposals/designs and non-AI domains;
- upstream's "reasoning preamble" is kept as a requirement, not as a request
  for hidden chain-of-thought in the output.

Also preserved: `debate-reviewer-4-categories.md` and `debate-response-4-categories.md` (upstream four-category debate/respond structure, adapted to the v3 JSON/evidence contract).
