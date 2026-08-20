"""Triage and deep-read stages, shared by the daily cron job (daily_pipeline.py)
and manual backfills (backfill.py).

Both callers derive their working set entirely from the `index` argument
(a `dict[str, PaperRow]`, typically from `sheets_store.load_papers_index`,
optionally pre-filtered by the caller -- e.g. backfill scopes it to a date
range before calling these). `settings.retries.max_retry_count` and
`settings.triage.score_threshold` flow through whichever `Settings` instance
the caller passes, so a backfill's in-memory threshold/query overrides apply
automatically with no changes needed here.
"""

from __future__ import annotations

import logging

from anthropic import Anthropic
from gspread import Worksheet

from lit_pipeline import pdf_extract, sheets_store
from lit_pipeline.arxiv_client import PaperCandidate, download_pdf_bytes
from lit_pipeline.config import Settings
from lit_pipeline.llm_deep_read import deep_read_paper
from lit_pipeline.llm_mid_summary import mid_summary_paper
from lit_pipeline.llm_triage import triage_paper
from lit_pipeline.sheets_store import PaperRow

logger = logging.getLogger(__name__)

# Flush triage updates to the sheet every N papers, rather than one write per
# paper (slow, burns Sheets API write quota) or only at the very end
# (loses all progress on a mid-stage crash).
TRIAGE_BATCH_SIZE = 8


def _candidate_from_row(row: PaperRow) -> PaperCandidate:
    """Reconstruct a PaperCandidate from an existing sheet row, for resuming
    a paper that a prior run already ingested but didn't finish processing."""
    r = row.raw
    link = str(r.get("link", ""))
    return PaperCandidate(
        arxiv_id=row.arxiv_id,
        title=str(r.get("title", "")),
        authors=str(r.get("authors", "")),
        published_date=str(r.get("published_date", "")),
        abstract=str(r.get("abstract", "")),
        link=link,
        pdf_url=link.replace("/abs/", "/pdf/"),
    )


def run_triage_stage(
    client: Anthropic,
    settings: Settings,
    papers_ws: Worksheet,
    index: dict[str, PaperRow],
) -> None:
    to_triage = [
        row
        for row in index.values()
        if row.status == sheets_store.STATUS_INGESTED
        or (
            row.status == sheets_store.STATUS_TRIAGE_ERROR
            and row.retry_count < settings.retries.max_retry_count
        )
    ]
    logger.info("Triaging %d paper(s)", len(to_triage))

    batched: list[dict] = []
    for row in to_triage:
        candidate = _candidate_from_row(row)
        try:
            result, usage = triage_paper(client, settings.triage, settings.interests, candidate)
        except Exception as exc:  # isolate any failure to this one paper
            logger.warning("Triage failed for %s: %s", row.arxiv_id, exc)
            next_retry = row.retry_count + 1
            status = (
                sheets_store.STATUS_TRIAGE_FAILED_PERMANENT
                if next_retry >= settings.retries.max_retry_count
                else sheets_store.STATUS_TRIAGE_ERROR
            )
            batched.extend(
                sheets_store.build_cell_updates(
                    row.row_number,
                    {"status": status, "last_error": str(exc)[:500], "retry_count": next_retry},
                )
            )
        else:
            batched.extend(
                sheets_store.build_cell_updates(
                    row.row_number,
                    {
                        "status": sheets_store.STATUS_TRIAGED,
                        "triage_score": result.score,
                        "original_triage_score": result.score,
                        "triage_rationale": result.rationale,
                        "matched_interest": result.matched_interest or "",
                        "triage_input_tokens": usage.input_tokens,
                        "triage_output_tokens": usage.output_tokens,
                        "triage_cost_usd": round(usage.cost_usd, 6),
                        "triaged_at": sheets_store.now_iso(),
                        "last_error": "",
                    },
                )
            )
            row.status = sheets_store.STATUS_TRIAGED
            row.triage_score = result.score

        if len(batched) >= TRIAGE_BATCH_SIZE:
            sheets_store.flush_cell_updates(papers_ws, batched)

    sheets_store.flush_cell_updates(papers_ws, batched)


def run_mid_summary_stage(
    client: Anthropic,
    settings: Settings,
    papers_ws: Worksheet,
    index: dict[str, PaperRow],
) -> None:
    """Papers scoring in [mid_summary_threshold, score_threshold) get a
    cheap ~50-word summary from the abstract alone -- no PDF fetch. Below
    mid_summary_threshold, a row just stays "triaged" forever (title-only
    in reports); this stage never touches those."""
    to_summarize = [
        row
        for row in index.values()
        if (
            row.status == sheets_store.STATUS_TRIAGED
            and settings.triage.mid_summary_threshold
            <= (row.triage_score or 0)
            < settings.triage.score_threshold
        )
        or (
            row.status == sheets_store.STATUS_MID_SUMMARY_ERROR
            and row.retry_count < settings.retries.max_retry_count
        )
    ]
    logger.info("Mid-summarizing %d paper(s)", len(to_summarize))

    batched: list[dict] = []
    for row in to_summarize:
        candidate = _candidate_from_row(row)
        try:
            result, usage = mid_summary_paper(client, settings.triage, settings.interests, candidate)
        except Exception as exc:  # isolate any failure to this one paper
            logger.warning("Mid-summary failed for %s: %s", row.arxiv_id, exc)
            next_retry = row.retry_count + 1
            status = (
                sheets_store.STATUS_MID_SUMMARY_FAILED_PERMANENT
                if next_retry >= settings.retries.max_retry_count
                else sheets_store.STATUS_MID_SUMMARY_ERROR
            )
            batched.extend(
                sheets_store.build_cell_updates(
                    row.row_number,
                    {"status": status, "last_error": str(exc)[:500], "retry_count": next_retry},
                )
            )
        else:
            batched.extend(
                sheets_store.build_cell_updates(
                    row.row_number,
                    {
                        "status": sheets_store.STATUS_MID_SUMMARY_COMPLETE,
                        "mid_summary": result.summary,
                        "mid_summary_input_tokens": usage.input_tokens,
                        "mid_summary_output_tokens": usage.output_tokens,
                        "mid_summary_cost_usd": round(usage.cost_usd, 6),
                        "mid_summary_at": sheets_store.now_iso(),
                        "last_error": "",
                    },
                )
            )
            row.status = sheets_store.STATUS_MID_SUMMARY_COMPLETE

        if len(batched) >= TRIAGE_BATCH_SIZE:
            sheets_store.flush_cell_updates(papers_ws, batched)

    sheets_store.flush_cell_updates(papers_ws, batched)


def run_deep_read_stage(
    client: Anthropic,
    settings: Settings,
    papers_ws: Worksheet,
    index: dict[str, PaperRow],
) -> None:
    to_deep_read = [
        row
        for row in index.values()
        if (
            row.status == sheets_store.STATUS_TRIAGED
            and (row.triage_score or 0) >= settings.triage.score_threshold
        )
        or (
            row.status == sheets_store.STATUS_DEEP_READ_ERROR
            and row.retry_count < settings.retries.max_retry_count
        )
    ]
    logger.info("Deep-reading %d paper(s)", len(to_deep_read))

    for row in to_deep_read:
        candidate = _candidate_from_row(row)
        try:
            pdf_bytes = download_pdf_bytes(candidate.pdf_url)
            pdf_text = pdf_extract.extract_pdf_text(pdf_bytes, max_pages=settings.deep_read.max_pdf_pages)
            result, usage = deep_read_paper(client, settings.deep_read, settings.interests, candidate, pdf_text)
        except Exception as exc:  # isolate any failure (download, extraction, API) to this one paper
            logger.warning("Deep read failed for %s: %s", row.arxiv_id, exc)
            next_retry = row.retry_count + 1
            status = (
                sheets_store.STATUS_DEEP_READ_FAILED_PERMANENT
                if next_retry >= settings.retries.max_retry_count
                else sheets_store.STATUS_DEEP_READ_ERROR
            )
            sheets_store.flush_cell_updates(
                papers_ws,
                sheets_store.build_cell_updates(
                    row.row_number,
                    {"status": status, "last_error": str(exc)[:500], "retry_count": next_retry},
                ),
            )
            continue

        sheets_store.flush_cell_updates(
            papers_ws,
            sheets_store.build_cell_updates(
                row.row_number,
                {
                    "status": sheets_store.STATUS_DEEP_READ_COMPLETE,
                    "triage_score": result.score,
                    "deep_read_summary": result.summary,
                    "deep_read_relevance": " | ".join(result.relevance),
                    "deep_read_limitations": " | ".join(result.limitations),
                    "deep_read_author_affiliations": " | ".join(result.author_affiliations),
                    "deep_read_input_tokens": usage.input_tokens,
                    "deep_read_output_tokens": usage.output_tokens,
                    "deep_read_cost_usd": round(usage.cost_usd, 6),
                    "deep_read_at": sheets_store.now_iso(),
                    "last_error": "",
                },
            ),
        )
