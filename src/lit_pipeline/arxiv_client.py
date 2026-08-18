"""Search arXiv for candidate papers and fetch their PDF bytes.

The `arxiv` package wraps arXiv's free, keyless public API. `arxiv.Client`
handles the courtesy rate limiting (a delay between API requests) and retries
for us -- we never need a manual `time.sleep()` around search calls.

Note: as of arxiv==4.0.1, `Result` no longer has a `download_pdf()` helper
(older versions did), so PDF bytes are fetched directly with httpx below.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import arxiv
import httpx

from lit_pipeline.config import ArxivSettings

logger = logging.getLogger(__name__)

# Be a polite client when hitting the PDF servers directly, same spirit as
# the courtesy delay `arxiv.Client` applies to the search API.
PDF_DOWNLOAD_DELAY_SECONDS = 2.0
PDF_DOWNLOAD_TIMEOUT_SECONDS = 60.0
PDF_DOWNLOAD_MAX_RETRIES = 3


@dataclass
class PaperCandidate:
    arxiv_id: str  # version-stripped, e.g. "2501.12345"
    title: str
    authors: str  # comma-joined
    published_date: str  # ISO date, e.g. "2026-01-15"
    abstract: str
    link: str
    pdf_url: str


def _strip_version(entry_id: str) -> str:
    """'http://arxiv.org/abs/2501.12345v2' -> '2501.12345'"""
    base = entry_id.rstrip("/").rsplit("/", 1)[-1]
    if "v" in base:
        base = base.rsplit("v", 1)[0]
    return base


def fetch_candidates(settings: ArxivSettings) -> list[PaperCandidate]:
    """Run every configured query and return distinct, recent candidates."""
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)

    seen: dict[str, PaperCandidate] = {}
    for query in settings.queries:
        search = arxiv.Search(
            query=query,
            max_results=settings.max_results_per_query,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        count_for_query = 0
        for result in client.results(search):
            if result.published < cutoff:
                continue
            arxiv_id = _strip_version(result.entry_id)
            if arxiv_id in seen or result.pdf_url is None:
                continue
            seen[arxiv_id] = PaperCandidate(
                arxiv_id=arxiv_id,
                title=result.title.strip().replace("\n", " "),
                authors=", ".join(a.name for a in result.authors),
                published_date=result.published.date().isoformat(),
                abstract=result.summary.strip().replace("\n", " "),
                link=result.entry_id,
                pdf_url=result.pdf_url,
            )
            count_for_query += 1
        logger.info("Query %r matched %d new candidate(s)", query, count_for_query)

    logger.info("Fetched %d distinct candidates across %d queries", len(seen), len(settings.queries))
    return list(seen.values())


def download_pdf_bytes(pdf_url: str) -> bytes:
    """Download a PDF's raw bytes, with a small courtesy delay and retries."""
    last_error: Exception | None = None
    for attempt in range(1, PDF_DOWNLOAD_MAX_RETRIES + 1):
        try:
            time.sleep(PDF_DOWNLOAD_DELAY_SECONDS)
            response = httpx.get(
                pdf_url,
                timeout=PDF_DOWNLOAD_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": "lit-pipeline/0.1 (personal research tracker)"},
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning("PDF download attempt %d/%d failed for %s: %s", attempt, PDF_DOWNLOAD_MAX_RETRIES, pdf_url, exc)
    assert last_error is not None
    raise last_error
