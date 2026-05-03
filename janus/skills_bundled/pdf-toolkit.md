---
name: pdf-toolkit
description: Extract text, OCR, summarize, redact, and split PDFs.
state: quarantined
capabilities:
  fs.read:
    - "**/*.pdf"
    - "**"
  fs.write:
    - "**/*.pdf"
    - "**/*.txt"
    - "**/*.md"
  code.exec:
    - "python"
  shell.exec:
    - "pdftotext *"
    - "pdfinfo *"
    - "qpdf *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running pdf-toolkit.

You operate on PDFs: text extraction, OCR for scanned docs, summarize,
redact, split, merge. Choose the lightest tool that does the job.

Steps:
1. Inspect first: `pdfinfo <file>` for size/pages/encryption. Don't load
   a 500-page PDF wholesale into the model context — extract per page.
2. **EXTRACT**: `pdftotext -layout <file> -` for text-mode PDFs.
   For scanned PDFs (no extractable text), use OCR via pytesseract or
   the `vision` tool on rasterized pages.
3. **SUMMARIZE**: extract → summarize per logical section (chapters,
   headers). Don't summarize the whole PDF in one prompt for >20 pages.
4. **REDACT**: identify the spans (PII patterns, named entities,
   user-supplied terms). Use `qpdf` or PyPDF to overlay redaction.
   VERIFY by re-extracting after — visual redaction without text
   removal still leaks via copy-paste.
5. **SPLIT/MERGE**: `qpdf --pages` for split, `qpdf --empty --pages` for
   merge. Always confirm output filename before writing.

Never claim a PDF is redacted without verifying the underlying text
stream is clean. Never email or share a PDF you redacted without an
external review pass.
