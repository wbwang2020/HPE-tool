# Third-party dependencies and licenses

This file is a deployment note, not legal advice. Verify the exact license of
the version you ship.

| Dependency | Role | License (typical upstream) | Notes |
|---|---|---|---|
| PyMuPDF (`fitz`) | primary PDF extraction | AGPL-3.0 or commercial | AGPL obligations may apply if you distribute a service/binary that includes it; a commercial license is available from Artifex. The implementation treats it as a recommended, replaceable dependency. |
| pdfplumber | fallback extraction | MIT | Uses pdfminer.six (MIT). |
| pypdf | fallback extraction | BSD-3-Clause | Pure Python. |
| Poppler `pdftotext` | fallback extraction | GPL-2.0+ | Invoked as an external process when installed; distribution implications depend on how it is bundled. |
| Tesseract / pytesseract | optional OCR | Apache-2.0 / MIT | Only used with `--ocr`; the `tesseract` binary is separate. |
| jsonschema | schema validation | MIT | Recommended; the CLI can run semantic checks without it. |
| pytest | tests only | MIT | Not shipped at runtime. |

The skill itself contains no model weights and no API keys. Provider prices in
`config/models.example.json` are placeholders; update them from the provider
documentation before relying on cost estimates.


## Prompt structure attribution

The 20/12/14-section summary structures and the four-category critique checklist
in `prompts/upstream/` are adapted from
[`wbwang2020/HPE-tool`](https://github.com/wbwang2020/HPE-tool),
commit `ab0b5610e1727ce1b8b4a57025b69c10ffd99b6c`, file
`src/prompts/prompt_templates.py`. The upstream repository did not include an
explicit license file at the inspected commit; verify licensing with the
upstream author before redistributing the verbatim prompt text. The v3 skill
rewrites these structures into its own JSON/evidence protocol; see
`prompts/upstream/README.md` for the exact modifications.
