"""Extract plain text from a PDF's raw bytes.

Uses PyMuPDF instead of sending the PDF natively to Claude: native PDF input
gives Claude visual understanding of figures/tables/layout, but costs far
more in tokens (observed ~59K input tokens/paper). For these quick-hit
summaries, plain extracted text is a better cost/quality tradeoff -- visual
fidelity isn't the point here.
"""

from __future__ import annotations

import pymupdf


def extract_pdf_text(pdf_bytes: bytes) -> str:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages = [page.get_text() for page in doc]
    text = "\n\n".join(pages).strip()
    if not text:
        raise ValueError("No extractable text found in PDF (possibly a scanned/image-only document)")
    return text
