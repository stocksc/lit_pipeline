"""Turn one deep-read paper into one self-contained PDF: the write-up as
page 1, the paper itself appended after it.

Used by `lit-deep-dive --attach-pdfs` (manual_deep_dive.py) to attach a
readable artifact per paper to the report email, so the deep read travels with
the paper instead of living in an inbox next to a link.

The cover page is rendered with PyMuPDF's `Story` engine rather than
`Page.insert_htmlbox`. Both take HTML, but a htmlbox is a fixed rectangle: it
either shrinks text to fit or overflows, and a long summary plus two bullet
lists genuinely can exceed one page. A Story paginates -- it places what fits,
says whether more is pending, and gets drawn onto as many pages as it needs.

Nothing here raises on a bad paper PDF. A download failure, an encrypted file,
or a PDF PyMuPDF can't parse degrades that one paper to a cover-only
attachment (the write-up is the part that can't be re-fetched from a link),
and the caller reports which ones. Same per-paper isolation the pipeline
stages use.
"""

from __future__ import annotations

import io
import logging
import re

import pymupdf

from lit_pipeline.arxiv_client import download_pdf_bytes
from lit_pipeline.reporting import ReportPaper, jinja_env

logger = logging.getLogger(__name__)

COVER_TEMPLATE = "deep_dive_cover.html.jinja"

# US Letter with 0.75in margins -- the write-up is prose, and a full-width
# line at this point size is uncomfortably long to read.
PAGE_SIZE = "letter"
PAGE_MARGIN_POINTS = 54

# Story supports a small CSS subset (fonts, sizes, colors, margins, lists);
# anything fancier is silently ignored, so keep this plain.
COVER_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; color: #1a1a1a; }
h1 { font-size: 16pt; margin-top: 0; margin-bottom: 2pt; }
h2 { font-size: 11pt; color: #374151; margin-top: 14pt; margin-bottom: 2pt; }
p { margin-top: 0; margin-bottom: 8pt; }
p.meta { font-size: 9pt; color: #6b7280; }
p.score { font-size: 12pt; font-weight: bold; margin-top: 10pt; margin-bottom: 2pt; }
span.was { font-size: 9.5pt; font-weight: normal; color: #6b7280; }
p.rationale { font-size: 9.5pt; color: #4b5563; font-style: italic; }
p.footer { font-size: 8.5pt; color: #9ca3af; margin-top: 18pt; }
li { margin-bottom: 3pt; }
"""

# Windows-illegal filename characters, plus the ones mail clients tend to
# mangle. Kept deliberately broad -- an attachment name gets saved to disk on
# whatever OS the reader happens to use.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_FILENAME_TITLE_LENGTH = 60


def attachment_filename(paper: ReportPaper) -> str:
    """'2501.12345 - Debiasing XGBoost for Credit Scoring.pdf'.

    Leads with the arXiv id so a folder of these sorts and de-duplicates
    sensibly, and so the id survives even when the title is truncated.
    """
    # Substituted with a space, not deleted: a title carrying a newline or a
    # slash would otherwise come back with the words either side glued together.
    title = _UNSAFE_FILENAME_CHARS.sub(" ", paper.title)
    title = " ".join(title.split())  # collapse the runs that leaves
    if len(title) > _FILENAME_TITLE_LENGTH:
        title = title[:_FILENAME_TITLE_LENGTH].rstrip() + "..."
    # A legacy id ("hep-th/9711200") carries a slash that's illegal in a
    # filename; it's the id's own separator, so keep it readable as a dash.
    arxiv_id = paper.arxiv_id.replace("/", "-")
    return f"{arxiv_id} - {title}.pdf" if title else f"{arxiv_id}.pdf"


def build_cover_pdf(paper: ReportPaper, report_title: str, deep_read_model: str) -> bytes:
    """Render the deep-read write-up as a standalone PDF, paginating over as
    many pages as the content needs."""
    html = jinja_env().get_template(COVER_TEMPLATE).render(
        paper=paper,
        report_title=report_title,
        deep_read_model=deep_read_model,
    )

    buffer = io.BytesIO()
    story = pymupdf.Story(html=html, user_css=COVER_CSS)
    writer = pymupdf.DocumentWriter(buffer)
    mediabox = pymupdf.paper_rect(PAGE_SIZE)
    content_box = mediabox + (
        PAGE_MARGIN_POINTS,
        PAGE_MARGIN_POINTS,
        -PAGE_MARGIN_POINTS,
        -PAGE_MARGIN_POINTS,
    )

    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(content_box)
        story.draw(device)
        writer.end_page()
    writer.close()
    return buffer.getvalue()


def fetch_paper_pdf(paper: ReportPaper) -> bytes | None:
    """Download the paper's own PDF, or None if it can't be had.

    The bytes aren't stored anywhere -- the deep-read stage downloads, extracts
    text, and drops them -- so this re-fetches. That also makes the reuse path
    work: a paper whose deep read came from an earlier run has no download to
    piggyback on either way. The PDF url is derived from the stored `link` the
    same way pipeline_stages._candidate_from_row does it.
    """
    pdf_url = paper.link.replace("/abs/", "/pdf/")
    try:
        return download_pdf_bytes(pdf_url)
    except Exception as exc:  # isolate to this one paper -- the cover still sends
        logger.warning("Couldn't download the PDF for %s (%s): %s", paper.arxiv_id, pdf_url, exc)
        return None


def build_bundle_pdf(cover_pdf: bytes, paper_pdf: bytes | None) -> bytes:
    """Cover page(s) followed by the paper. With `paper_pdf=None` -- or a PDF
    PyMuPDF can't open -- this returns the cover alone rather than failing:
    the write-up is the part the reader can't get from the arXiv link."""
    with pymupdf.open("pdf", cover_pdf) as bundle:
        if paper_pdf is not None:
            try:
                with pymupdf.open("pdf", paper_pdf) as paper_doc:
                    bundle.insert_pdf(paper_doc)
            except Exception as exc:  # encrypted, truncated, or not really a PDF
                logger.warning("Couldn't append the paper's PDF, sending the summary alone: %s", exc)
        return bundle.tobytes()
