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
from datetime import date, datetime, timedelta, timezone

import arxiv
import httpx

from lit_pipeline.config import ArxivSettings

logger = logging.getLogger(__name__)

# Be a polite client when hitting the PDF servers directly, same spirit as
# the courtesy delay `arxiv.Client` applies to the search API.
PDF_DOWNLOAD_DELAY_SECONDS = 2.0
PDF_DOWNLOAD_TIMEOUT_SECONDS = 60.0
PDF_DOWNLOAD_MAX_RETRIES = 3

# arXiv's practical earliest coverage -- used as the lower bound when a
# backfill only specifies `published_before`.
EARLIEST_ARXIV_DATE = date(2007, 1, 1)


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


def _format_arxiv_datetime(d: date, end_of_day: bool) -> str:
    """arXiv's submittedDate range format: YYYYMMDDHHMM, UTC."""
    return d.strftime("%Y%m%d") + ("2359" if end_of_day else "0000")


def fetch_candidates(
    settings: ArxivSettings,
    published_after: date | None = None,
    published_before: date | None = None,
) -> list[PaperCandidate]:
    """Run every configured query and return distinct candidates.

    With no date bounds (the daily job's call site), behaves exactly as
    before: a trailing window of `max_age_days` from now, filtered
    client-side.

    With either bound given (backfill's call site), pushes an explicit
    `submittedDate:[...]` range into each query so arXiv filters
    server-side, with `max_results=None`. This is required, not just an
    optimization: `arxiv.Search` sorts newest-first and caps results
    server-side *before* any client-side filtering, so a small
    `max_results` would never even surface months-old papers once more
    than `max_results` newer papers exist for that query.
    """
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)

    ranged = published_after is not None or published_before is not None
    if ranged:
        after = published_after or EARLIEST_ARXIV_DATE
        before = published_before or datetime.now(timezone.utc).date()
        date_clause = (
            f"submittedDate:[{_format_arxiv_datetime(after, end_of_day=False)}"
            f" TO {_format_arxiv_datetime(before, end_of_day=True)}]"
        )
        after_dt = datetime(after.year, after.month, after.day, tzinfo=timezone.utc)
        before_dt: datetime | None = datetime(before.year, before.month, before.day, 23, 59, 59, tzinfo=timezone.utc)
        max_results = None
    else:
        date_clause = None
        after_dt = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)
        before_dt = None
        max_results = settings.max_results_per_query

    seen: dict[str, PaperCandidate] = {}
    for query in settings.queries:
        # Parenthesize the configured query before ANDing in the date clause --
        # a bare `q1 OR q2 AND submittedDate:[...]` would bind incorrectly,
        # scoping the date range to only the last OR'd term.
        full_query = f"({query}) AND {date_clause}" if date_clause else query
        search = arxiv.Search(
            query=full_query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        count_for_query = 0
        for result in client.results(search):
            if before_dt is not None and result.published > before_dt:
                # A stray too-new result doesn't mean everything scanned
                # after it is also out of range -- keep going.
                continue
            if result.published < after_dt:
                # Descending sort: nothing further in this query can match either.
                break
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
        logger.info("Query %r matched %d new candidate(s)", full_query, count_for_query)

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
