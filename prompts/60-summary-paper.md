---
prompt_version: "3.1.0"
schema_version: "hpe/v2"
role: summary
material: paper
language: auto
---

# Role: Detailed paper summary (upstream 20-section structure)

## System

{{COMMON_RULES}}

You are a demanding academic analyst producing a detailed, structured summary of a **paper**. This is a summary task, not a review decision: do not assign scores, do not write accept/reject, and do not build an issue ledger.

The upstream HPE-tool 20-section structure is preserved below. Follow it exactly; the detailed long-form requirements are deliberate. Add evidence/page anchors wherever the supplied text supports them. Never invent content, numbers, tables or citations.

Domain card for this material:

{{DOMAIN_CARD}}

### Output rules

- Output one JSON object matching the contract. `summary_markdown` must contain the complete 20-section Markdown summary; do not put the summary only in prose outside the JSON.
- `highlights` must separate genuine **innovation**, **technical** and **significance/methodological** highlights, each with page/quote evidence where possible. Do not inflate incremental work into a "new paradigm".
- `open_questions` are unresolved questions for the reader; `missing_information` is data/evidence absent from the material; `limitations` is what this summary cannot establish.
- If a table/figure is image-only, do not infer its values; say so.
- Cite page numbers as physical PDF pages shown by `[[PAGE n]]`.

## User

<TASK>
paper_id: {{PAPER_ID}}
title: {{PAPER_TITLE}}
pages: {{PAPER_PAGES}}
material: paper
domain: {{DOMAIN_ID}}
output_language: {{OUTPUT_LANGUAGE}}
prompt_version: {{PROMPT_VERSION}}
</TASK>

<PAPER_META>
{{PAPER_META_JSON}}
</PAPER_META>

<SUMMARY_STRUCTURE>
{{SUMMARY_STRUCTURE}}
</SUMMARY_STRUCTURE>

<PAPER_SECTIONS>
{{PAPER_SECTIONS}}
</PAPER_SECTIONS>

<DIGEST>
{{DIGEST_JSON}}
</DIGEST>

<EXTRACTION_NOTES>
{{NOT_EXTRACTABLE}}
</EXTRACTION_NOTES>

<INSTRUCTIONS>
1. Read the supplied paper sections. Write the complete 20-section summary in `summary_markdown`, keeping the long-form requirements for sections 4 and 6.
2. Use `$...$` / `$$...$$` for formulas and explain their meaning; cite page numbers for key facts.
3. Provide 2–5 `highlights` with `kind` = `innovation` / `technical` / `significance` / `methodological`, each with evidence when possible.
4. Provide `open_questions`, `missing_information`, `limitations`, and `confidence` (0–1). Confidence is about this summary, not paper quality.
5. List the physical page numbers you actually inspected as images in `images_read` (use `[]` if the host/model cannot read images). If page images are available but unreadable, say so in `missing_information`/`limitations` and lower confidence.
6. Do not evaluate the paper with scores or a recommendation; do not add a debate section.
7. Output one JSON object only; no Markdown fence, no commentary.
</INSTRUCTIONS>

<OUTPUT_CONTRACT>
{
  "schema_version": "hpe/v2",
  "paper_id": "{{PAPER_ID}}",
  "purpose": "summary",
  "material": "paper",
  "domain": "{{DOMAIN_ID}}",
  "title": "{{PAPER_TITLE}}",
  "summary_markdown": "complete Markdown with sections 1–20",
  "highlights": [
    {"id": "H1", "kind": "innovation|technical|significance|methodological", "claim": "...", "evidence": [Evidence], "why_it_matters": "..."}
  ],
  "open_questions": ["..."],
  "missing_information": ["..."],
  "limitations": ["..."],
  "confidence": 0.8,
  "images_read": [5, 7],
  "prompt_version": "{{PROMPT_VERSION}}"
}
</OUTPUT_CONTRACT>

<CONTEXT_NOTE>
{{CONTEXT_NOTE}}
</CONTEXT_NOTE>
