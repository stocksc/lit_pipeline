"""Search arXiv for candidate papers and fetch their PDF bytes.

The `arxiv` package wraps arXiv's free, keyless public API. `arxiv.Client`
handles the courtesy rate limiting (a delay between API requests) and retries
for us -- we never need a manual `time.sleep()` around search calls.

Note: as of arxiv==4.0.1, `Result` no longer has a `download_pdf()` helper
(older versions did), so PDF bytes are fetched directly with httpx below.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import arxiv
import httpx

from lit_pipeline.config import ArxivSettings

logger = logging.getLogger(__name__)

# arXiv's abs: search stems terms before matching (e.g. abs:"fairness" also
# matches "fair", "fairly", "unfair" -- anything sharing that root), which is
# far broader than the literal word/phrase in settings.yaml. To keep that
# stemmed search for recall but restore the precision of an exact match, we
# re-check each result's abstract for the literal term afterwards.
#
# A single broad word (e.g. "fairness", "credit") is still too noisy even
# with exact matching -- these are common words with unrelated meanings in
# other fields (physics' "state discrimination", "credit" in finance
# generally, etc.). Requiring BROAD_KEYWORD_MIN_HITS of settings.arxiv's
# `broad_keywords` to co-occur in the same abstract restores precision
# without needing a human to hand-curate an ever-growing exact-phrase list.
BROAD_KEYWORD_MIN_HITS = 2


def matches_literally(term: str, abstract: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", abstract, re.IGNORECASE) is not None


def build_abs_query(specific_keywords: list[str], broad_keywords: list[str]) -> str:
    """Combines both keyword lists into one arXiv abs: OR query. Casts a
    wider net than the eventual match (arXiv doesn't understand "2 of
    these 4" co-occurrence), so every result still gets client-side
    filtered by `passes_keyword_filter` before being kept."""
    terms = specific_keywords + broad_keywords
    return " OR ".join(f'abs:"{t}"' for t in terms)


def passes_keyword_filter(abstract: str, specific_keywords: list[str], broad_keywords: list[str]) -> bool:
    if any(matches_literally(t, abstract) for t in specific_keywords):
        return True
    broad_hits = sum(1 for t in broad_keywords if matches_literally(t, abstract))
    return broad_hits >= BROAD_KEYWORD_MIN_HITS


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
    """Search arXiv for `specific_keywords`/`broad_keywords` and return
    distinct candidates that pass `passes_keyword_filter`.

    The arXiv-side query ORs every keyword from both lists together --
    deliberately wider than the eventual match, since arXiv has no way to
    express "2 of these 4 broad keywords co-occur." That co-occurrence
    check happens client-side per result below.

    With no date bounds (the daily job's call site), behaves as a trailing
    window of `max_age_days` from now, filtered client-side.

    With either bound given (backfill's call site), pushes an explicit
    `submittedDate:[...]` range into the query so arXiv filters
    server-side, with `max_results=None`. This is required, not just an
    optimization: `arxiv.Search` sorts newest-first and caps results
    server-side *before* any client-side filtering, so a small
    `max_results` would never even surface months-old papers once more
    than `max_results` newer papers exist for the query.
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

    # Parenthesize the keyword query before ANDing in the date clause -- a
    # bare `q1 OR q2 AND submittedDate:[...]` would bind incorrectly,
    # scoping the date range to only the last OR'd term.
    keyword_query = build_abs_query(settings.specific_keywords, settings.broad_keywords)
    full_query = f"({keyword_query}) AND {date_clause}" if date_clause else keyword_query
    search = arxiv.Search(
        query=full_query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    seen: dict[str, PaperCandidate] = {}
    matched_raw = 0
    for result in client.results(search):
        if before_dt is not None and result.published > before_dt:
            # A stray too-new result doesn't mean everything scanned
            # after it is also out of range -- keep going.
            continue
        if result.published < after_dt:
            # Descending sort: nothing further in this query can match either.
            break
        matched_raw += 1
        arxiv_id = _strip_version(result.entry_id)
        if arxiv_id in seen or result.pdf_url is None:
            continue
        abstract = result.summary.strip().replace("\n", " ")
        if not passes_keyword_filter(abstract, settings.specific_keywords, settings.broad_keywords):
            continue
        seen[arxiv_id] = PaperCandidate(
            arxiv_id=arxiv_id,
            title=result.title.strip().replace("\n", " "),
            authors=", ".join(a.name for a in result.authors),
            published_date=result.published.date().isoformat(),
            abstract=abstract,
            link=result.entry_id,
            pdf_url=result.pdf_url,
        )

    logger.info(
        "arXiv matched %d raw result(s); %d passed the keyword filter",
        matched_raw,
        len(seen),
    )
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
