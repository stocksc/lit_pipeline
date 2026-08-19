"""Extract plain text from a PDF's raw bytes.

Uses PyMuPDF instead of sending the PDF natively to Claude: native PDF input
gives Claude visual understanding of figures/tables/layout, but costs far
more in tokens (observed ~59K input tokens/paper). For these quick-hit
summaries, plain extracted text is a better cost/quality tradeoff -- visual
fidelity isn't the point here.

Two trims are applied before the text goes anywhere near an LLM call:

1. A references/bibliography cut -- a bibliography is pure token cost with
   zero summary value, and cutting at that heading also drops any appendix
   after it (standard paper structure). This is the main saver, typically
   10-20%+ per paper.
2. A generous page-count cap -- a backstop for the rare pathological
   outlier (e.g. a 200+ page thesis swept up by the same arXiv query as
   everything else) where the references heading can't be trusted to mark
   "end of real content" -- a multi-chapter document can have several such
   headings, and blindly trusting the first one risks truncating real
   later chapters. The cap bounds worst-case cost regardless.

Both are strictly safe: on a normal paper under the cap with no heading
match, the result is byte-identical to sending the full extracted text.
"""

from __future__ import annotations

import logging
import re

import pymupdf

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 50

# Matches a standalone "References" or "Bibliography" heading line --
# deliberately strict (the whole line, nothing else) so it can't match a
# section titled e.g. "6 References and Related Work", or a
# table-of-contents entry (which always carries a page number/dot leader,
# so it never reduces to a bare line).
_REFERENCES_HEADING_RE = re.compile(r"^[ \t]*(references|bibliography)[ \t]*$", re.IGNORECASE | re.MULTILINE)

# A heading match on the first couple of pages is almost certainly a false
# positive (e.g. a stray table-of-contents artifact) rather than a paper's
# real references section, which never starts that early.
_MIN_HEADING_PAGE = 3


def extract_pdf_text(pdf_bytes: bytes, max_pages: int = DEFAULT_MAX_PAGES) -> str:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        n_pages = min(len(doc), max_pages)
        if len(doc) > max_pages:
            logger.info("PDF has %d pages; capping extraction to first %d", len(doc), max_pages)
        pages = [doc[i].get_text() for i in range(n_pages)]

    if not pages:
        raise ValueError("No extractable text found in PDF (possibly a scanned/image-only document)")

    cutoff = None
    offset = 0
    for page_num, page_text in enumerate(pages, start=1):
        if page_num >= _MIN_HEADING_PAGE:
            match = _REFERENCES_HEADING_RE.search(page_text)
            if match:
                cutoff = offset + match.start()
                logger.info("Found references/bibliography heading on page %d; trimming text there", page_num)
                break
        offset += len(page_text) + 2  # +2 for the "\n\n" join separator below

    text = "\n\n".join(pages)
    if cutoff is not None:
        text = text[:cutoff]
    text = text.strip()

    if not text:
        raise ValueError("No extractable text found in PDF (possibly a scanned/image-only document)")
    return text
