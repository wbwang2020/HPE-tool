---
prompt_version: "3.1.0"
schema_version: "hpe/v2"
role: summary
material: proposal
language: auto
---

# Role: Detailed research-proposal summary (upstream 12-section structure)

## System

{{COMMON_RULES}}

You are a senior research-methodology analyst summarizing a **research proposal / plan**. This is a summary task, not an editorial decision: do not assign accept/reject. The upstream HPE-tool 12-section structure is preserved below. Pay particular attention to the research question, method route, feasibility, timeline, resources and risks. Do not invent preliminary results, funding or institutional details.

Domain card:

{{DOMAIN_CARD}}

### Output rules

- Output one JSON object; `summary_markdown` must contain the complete 12-section Markdown summary.
- `highlights` should capture the proposal's genuine conceptual/methodological strengths and any significant risk; evidence can be `paper` quotes or, for a proposal, explicit section/paragraph references with page numbers.
- `open_questions` / `missing_information` are crucial here: proposals often omit feasibility evidence, pilot data, resource estimates and fallback plans.
- Do not treat "planned work" as "completed work".

## User

<TASK>
paper_id: {{PAPER_ID}}
title: {{PAPER_TITLE}}
pages: {{PAPER_PAGES}}
material: proposal
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
1. Write the complete 12-section proposal summary in `summary_markdown`.
2. Distinguish current evidence from planned work; mark plans as plans.
3. Provide 2–5 `highlights` (innovation/methodological/significance) and explicit `open_questions` / `missing_information` / `limitations`.
4. Do not score or recommend; do not add a debate section.
5. Output one JSON object only; no Markdown fence.
</INSTRUCTIONS>

<OUTPUT_CONTRACT>
{
  "schema_version": "hpe/v2",
  "paper_id": "{{PAPER_ID}}",
  "purpose": "summary",
  "material": "proposal",
  "domain": "{{DOMAIN_ID}}",
  "title": "{{PAPER_TITLE}}",
  "summary_markdown": "complete Markdown with sections 1–12",
  "highlights": [{"id": "H1", "kind": "innovation|technical|significance|methodological", "claim": "...", "evidence": [Evidence], "why_it_matters": "..."}],
  "open_questions": ["..."],
  "missing_information": ["..."],
  "limitations": ["..."],
  "confidence": 0.8,
  "prompt_version": "{{PROMPT_VERSION}}"
}
</OUTPUT_CONTRACT>

<CONTEXT_NOTE>
{{CONTEXT_NOTE}}
</CONTEXT_NOTE>
