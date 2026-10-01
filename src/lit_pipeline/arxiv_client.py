"""Find candidate papers on arXiv and fetch their PDF bytes.

Two ways in, for two different jobs:

- `harvest_candidates` (the daily job) pulls every record arXiv touched in
  a date window from its OAI-PMH interface, arXiv's designated channel for
  routinely copying metadata, and keyword-filters them client-side.
- `fetch_candidates` (backfill) and `fetch_by_ids` (manual deep dive) use
  the search API via the `arxiv` package, whose `arxiv.Client` handles the
  courtesy delay between requests and retries for us.

The daily job moved off the search API because that API is
capacity-constrained and throttles per IP at arXiv's CDN: GitHub's shared
runner IPs regularly got a 429/406 on a run's very *first* request, no
matter how politely the run itself behaved. OAI-PMH is served separately,
with its own flow control (503 + Retry-After). The search API remains fine
for the occasional hand-run backfill or deep dive, where a refusal just
means trying again later.

Note: as of arxiv==4.0.1, `Result` no longer has a `download_pdf()` helper
(older versions did), so PDF bytes are fetched directly with httpx below.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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


# Sent on every request we make directly with httpx (OAI-PMH and PDFs).
USER_AGENT = "lit-pipeline/0.1 (personal research tracker)"

# Be a polite client when hitting the PDF servers directly, same spirit as
# the courtesy delay `arxiv.Client` applies to the search API.
PDF_DOWNLOAD_DELAY_SECONDS = 2.0
PDF_DOWNLOAD_TIMEOUT_SECONDS = 60.0
PDF_DOWNLOAD_MAX_RETRIES = 3

OAI_PMH_BASE_URL = "https://oaipmh.arxiv.org/oai"
OAI_NAMESPACES = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "raw": "http://arxiv.org/OAI/arXivRaw/",
}
# arXiv's terms of use: no more than one request every three seconds, across
# all of its APIs.
OAI_REQUEST_DELAY_SECONDS = 3.0
# A page is a few MB of XML, and arXiv can be slow to generate one.
OAI_REQUEST_TIMEOUT_SECONDS = 120.0
# OAI-PMH flow control: a 503 with Retry-After means "come back in N
# seconds", which we honor -- up to this much total waiting per harvest,
# after which the run fails and the next day's run picks the window back up
# from the checkpoint.
OAI_MAX_TOTAL_RETRY_WAIT_SECONDS = 300.0
OAI_DEFAULT_RETRY_AFTER_SECONDS = 30.0


@dataclass
class PaperCandidate:
    arxiv_id: str  # version-stripped, e.g. "2501.12345"
    title: str
    authors: str  # comma-joined
    published_date: str  # ISO date, e.g. "2026-01-15"
    abstract: str
    link: str
    pdf_url: str


# The abs/pdf URL wrapper around an id, in either the form arXiv's API returns
# (`result.entry_id`) or the form someone pastes out of their browser.
ARXIV_URL_PREFIX_PATTERN = re.compile(r"^https?://(www\.)?arxiv\.org/(abs|pdf)/", re.IGNORECASE)


def _strip_version(entry_id: str) -> str:
    """'http://arxiv.org/abs/2501.12345v2' -> '2501.12345'.

    A legacy id keeps its archive prefix ('.../abs/math.GT/0309136v1' ->
    'math.GT/0309136'): that prefix is part of the id itself rather than a URL
    path segment, so the id can't just be read off the last path component
    without corrupting it -- and a corrupted key breaks dedup and every
    lookup keyed by arxiv_id thereafter.
    """
    base = ARXIV_URL_PREFIX_PATTERN.sub("", entry_id.strip()).rstrip("/")
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return re.sub(r"v\d+$", "", base)


# Modern ("2501.12345") and legacy ("math.GT/0309136") arXiv id forms, with an
# optional version suffix. Checked up front by `normalize_arxiv_id` so a typo
# in a hand-typed id fails immediately with a clear message, rather than as an
# opaque arXiv API error part-way through a run (or, worse, as an "error"
# entry quietly returned inside an otherwise-successful feed).
ARXIV_ID_PATTERN = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Za-z]{2})?/\d{7})(v\d+)?$", re.IGNORECASE)


def normalize_arxiv_id(raw: str) -> str:
    """Reduce a hand-supplied paper reference to the bare, version-stripped id
    the sheet is keyed by. Accepts what someone would realistically paste:
    '2501.12345', '2501.12345v2', 'arXiv:2501.12345', a full abs/pdf URL, or a
    legacy 'hep-th/9711200'. Raises ValueError on anything that isn't a
    recognizable arXiv id."""
    value = re.sub(r"^arxiv:", "", raw.strip(), flags=re.IGNORECASE)
    value = _strip_version(value)  # also unwraps an abs/pdf URL, if that's what this is
    if not ARXIV_ID_PATTERN.match(value):
        raise ValueError(f"{raw!r} is not a recognizable arXiv id")
    return value


def _candidate_from_result(result: arxiv.Result) -> PaperCandidate:
    """Map an arXiv API result onto our own PaperCandidate shape -- shared by
    the keyword-search path (`fetch_candidates`) and the by-id path
    (`fetch_by_ids`), so both store identical fields for a given paper."""
    return PaperCandidate(
        arxiv_id=_strip_version(result.entry_id),
        title=result.title.strip().replace("\n", " "),
        authors=", ".join(a.name for a in result.authors),
        published_date=result.published.date().isoformat(),
        abstract=result.summary.strip().replace("\n", " "),
        link=result.entry_id,
        pdf_url=result.pdf_url,
    )


def _format_arxiv_datetime(d: date, end_of_day: bool) -> str:
    """arXiv's submittedDate range format: YYYYMMDDHHMM, UTC."""
    return d.strftime("%Y%m%d") + ("2359" if end_of_day else "0000")


def fetch_candidates(
    settings: ArxivSettings,
    published_after: date,
    published_before: date,
) -> list[PaperCandidate]:
    """Search arXiv for `specific_keywords`/`broad_keywords` submitted in
    [published_after, published_before] and return distinct candidates that
    pass `passes_keyword_filter`. Used by backfill; the daily job uses
    `harvest_candidates` instead (see the module docstring).

    The arXiv-side query ORs every keyword from both lists together --
    deliberately wider than the eventual match, since arXiv has no way to
    express "2 of these 4 broad keywords co-occur." That co-occurrence
    check happens client-side per result below.

    The date range goes into the query as an explicit `submittedDate:[...]`
    clause so arXiv filters server-side, with `max_results=None`. This is
    required, not just an optimization: `arxiv.Search` sorts newest-first
    and caps results server-side *before* any client-side filtering, so a
    `max_results` cap would never even surface the older papers in range
    once more than that many newer papers exist for the query.
    """
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)

    date_clause = (
        f"submittedDate:[{_format_arxiv_datetime(published_after, end_of_day=False)}"
        f" TO {_format_arxiv_datetime(published_before, end_of_day=True)}]"
    )
    after_dt = datetime(published_after.year, published_after.month, published_after.day, tzinfo=timezone.utc)
    before_dt = datetime(
        published_before.year, published_before.month, published_before.day, 23, 59, 59, tzinfo=timezone.utc
    )

    # Parenthesize the keyword query before ANDing in the date clause -- a
    # bare `q1 OR q2 AND submittedDate:[...]` would bind incorrectly,
    # scoping the date range to only the last OR'd term.
    keyword_query = build_abs_query(settings.specific_keywords, settings.broad_keywords)
    search = arxiv.Search(
        query=f"({keyword_query}) AND {date_clause}",
        max_results=None,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    seen: dict[str, PaperCandidate] = {}
    matched_raw = 0
    for result in client.results(search):
        if result.published > before_dt:
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
        seen[arxiv_id] = _candidate_from_result(result)

    logger.info(
        "arXiv matched %d raw result(s); %d passed the keyword filter",
        matched_raw,
        len(seen),
    )
    return list(seen.values())


def _parse_retry_after(value: str | None) -> float:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return OAI_DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        return max((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return OAI_DEFAULT_RETRY_AFTER_SECONDS


def _oai_list_records_pages(from_date: date, until_date: date) -> Iterator[ET.Element]:
    """Yield each parsed ListRecords page for [from_date, until_date],
    following resumptionTokens until the list is exhausted."""
    params: dict[str, str] = {
        "verb": "ListRecords",
        "metadataPrefix": "arXivRaw",
        "from": from_date.isoformat(),
        "until": until_date.isoformat(),
    }
    total_retry_wait = 0.0
    last_request_at: float | None = None
    with httpx.Client(
        timeout=OAI_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        while True:
            if last_request_at is not None:
                remaining = OAI_REQUEST_DELAY_SECONDS - (time.monotonic() - last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            response = client.get(OAI_PMH_BASE_URL, params=params)
            last_request_at = time.monotonic()

            if response.status_code == 503:
                wait = _parse_retry_after(response.headers.get("Retry-After"))
                if total_retry_wait + wait > OAI_MAX_TOTAL_RETRY_WAIT_SECONDS:
                    response.raise_for_status()
                logger.info("OAI-PMH asked us to wait %.0fs (503 Retry-After)", wait)
                time.sleep(wait)
                total_retry_wait += wait
                continue
            response.raise_for_status()

            root = ET.fromstring(response.content)
            error = root.find("oai:error", OAI_NAMESPACES)
            if error is not None:
                # A window with nothing in it (e.g. no announcement that day)
                # is an "error" in OAI-PMH terms, but just an empty result to us.
                if error.get("code") == "noRecordsMatch":
                    return
                raise RuntimeError(f"OAI-PMH error {error.get('code')}: {(error.text or '').strip()}")
            yield root

            token = root.find("oai:ListRecords/oai:resumptionToken", OAI_NAMESPACES)
            if token is None or not (token.text or "").strip():
                return
            # Per the protocol, a follow-up request carries only the verb and the token.
            params = {"verb": "ListRecords", "resumptionToken": token.text.strip()}


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _join_authors(raw_authors: str) -> str:
    """arXivRaw gives authors as one free-text line, e.g. "A, B and C" or
    "A and B". Normalize the final "and" to a comma so the sheet gets the
    same comma-joined form `_candidate_from_result` produces."""
    return re.sub(r",?\s+and\s+(?!.*\s+and\s+)", ", ", _collapse_whitespace(raw_authors))


def harvest_candidates(settings: ArxivSettings, from_date: date, until_date: date) -> list[PaperCandidate]:
    """Harvest every arXiv record changed in [from_date, until_date] (UTC,
    inclusive) from OAI-PMH and return the new submissions among them that
    pass `passes_keyword_filter`. Used by the daily job.

    OAI-PMH selects by the date arXiv last changed a record, not by
    submission date, so the window also returns new versions of old
    papers. Those are dropped by first-version (v1) date: anything first
    submitted more than `max_age_days` before `from_date` isn't new.
    """
    oldest_allowed = from_date - timedelta(days=settings.max_age_days)
    seen: dict[str, PaperCandidate] = {}
    raw_count = 0
    new_count = 0
    pages = 0
    for page in _oai_list_records_pages(from_date, until_date):
        pages += 1
        for record in page.iterfind("oai:ListRecords/oai:record", OAI_NAMESPACES):
            header = record.find("oai:header", OAI_NAMESPACES)
            if header is not None and header.get("status") == "deleted":
                continue
            meta = record.find("oai:metadata/raw:arXivRaw", OAI_NAMESPACES)
            if meta is None:
                continue
            raw_count += 1

            versions = meta.findall("raw:version", OAI_NAMESPACES)
            v1_date_text = next(
                (v.findtext("raw:date", namespaces=OAI_NAMESPACES) for v in versions if v.get("version") == "v1"),
                None,
            )
            if not v1_date_text:
                continue
            published = parsedate_to_datetime(v1_date_text).astimezone(timezone.utc).date()
            if published < oldest_allowed:
                continue
            new_count += 1

            arxiv_id = meta.findtext("raw:id", default="", namespaces=OAI_NAMESPACES).strip()
            abstract = _collapse_whitespace(meta.findtext("raw:abstract", default="", namespaces=OAI_NAMESPACES))
            if not arxiv_id or arxiv_id in seen:
                continue
            if not passes_keyword_filter(abstract, settings.specific_keywords, settings.broad_keywords):
                continue
            # Link to the latest version, matching what the search API's
            # entry_id gives `_candidate_from_result`.
            latest = versions[-1].get("version", "") if versions else ""
            seen[arxiv_id] = PaperCandidate(
                arxiv_id=arxiv_id,
                title=_collapse_whitespace(meta.findtext("raw:title", default="", namespaces=OAI_NAMESPACES)),
                authors=_join_authors(meta.findtext("raw:authors", default="", namespaces=OAI_NAMESPACES)),
                published_date=published.isoformat(),
                abstract=abstract,
                link=f"https://arxiv.org/abs/{arxiv_id}{latest}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}{latest}",
            )

    logger.info(
        "OAI-PMH harvest %s to %s: %d record(s) over %d page(s); %d new submission(s); %d passed the keyword filter",
        from_date,
        until_date,
        raw_count,
        pages,
        new_count,
        len(seen),
    )
    return list(seen.values())


# arXiv answers an unknown or withdrawn id with an "error" entry inside an
# otherwise-normal feed rather than an HTTP error, identifiable by this marker
# in the entry's id.
ARXIV_ERROR_ENTRY_MARKER = "arxiv.org/api/errors"


def fetch_by_ids(arxiv_ids: list[str]) -> list[PaperCandidate]:
    """Fetch specific papers by arXiv id, skipping the keyword search and
    `passes_keyword_filter` entirely -- a paper asked for by id is wanted
    whether or not it would ever have matched the standing queries (see
    manual_deep_dive.py).

    Ids arXiv doesn't return (unknown or withdrawn) are simply absent from the
    result; the caller compares against what it asked for and reports the
    difference, so one bad id doesn't sink the rest of the run.
    """
    if not arxiv_ids:
        return []
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)
    search = arxiv.Search(id_list=list(arxiv_ids), max_results=len(arxiv_ids))
    found: dict[str, PaperCandidate] = {}
    for result in client.results(search):
        if ARXIV_ERROR_ENTRY_MARKER in result.entry_id or result.pdf_url is None:
            continue
        candidate = _candidate_from_result(result)
        found[candidate.arxiv_id] = candidate
    logger.info("arXiv returned %d of the %d requested id(s)", len(found), len(arxiv_ids))
    return list(found.values())


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
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning("PDF download attempt %d/%d failed for %s: %s", attempt, PDF_DOWNLOAD_MAX_RETRIES, pdf_url, exc)
    assert last_error is not None
    raise last_error
