"""Daily pipeline: steps 1-6 of the literature tracker.

Entry point: `uv run lit-daily` (see pyproject.toml [project.scripts]).

Safe to re-run at any time. Every stage checkpoints its progress in the
`papers` sheet's `status` column (see sheets_store.py), so a crash mid-run
just means the next run picks up where it left off -- nothing needs to be
tracked outside the sheet itself.
"""

from __future__ import annotations

import logging
import sys

from anthropic import Anthropic
from dotenv import load_dotenv
from gspread import Worksheet

from lit_pipeline import sheets_store
from lit_pipeline.arxiv_client import PaperCandidate, download_pdf_bytes, fetch_candidates
from lit_pipeline.config import Settings, load_settings
from lit_pipeline.llm_deep_read import deep_read_paper
from lit_pipeline.llm_triage import triage_paper
from lit_pipeline.sheets_store import PaperRow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
            result = triage_paper(client, settings.triage, settings.interests, candidate)
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
                        "triage_rationale": result.rationale,
                        "matched_interest": result.matched_interest or "",
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


def run_deep_read_stage(
    client: Anthropic,
    settings: Settings,
    papers_ws: Worksheet,
    deep_reads_ws: Worksheet,
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
            result = deep_read_paper(client, settings.deep_read, settings.interests, candidate, pdf_bytes)
        except Exception as exc:  # isolate any failure (download, API, parsing) to this one paper
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

        sheets_store.append_deep_read(deep_reads_ws, row.arxiv_id, result)
        sheets_store.flush_cell_updates(
            papers_ws,
            sheets_store.build_cell_updates(
                row.row_number,
                {"status": sheets_store.STATUS_DEEP_READ_COMPLETE, "last_error": ""},
            ),
        )


def main() -> int:
    load_dotenv()
    settings = load_settings()
    anthropic_client = Anthropic()
    papers_ws, deep_reads_ws = sheets_store.open_sheets(settings.google_sheets)

    logger.info("Fetching arXiv candidates...")
    candidates = fetch_candidates(settings.arxiv)

    index = sheets_store.load_papers_index(papers_ws)
    new_candidates = [c for c in candidates if c.arxiv_id not in index]
    sheets_store.append_new_candidates(papers_ws, new_candidates)

    # Re-read so newly appended rows have row numbers and are visible to triage.
    index = sheets_store.load_papers_index(papers_ws)

    run_triage_stage(anthropic_client, settings, papers_ws, index)
    run_deep_read_stage(anthropic_client, settings, papers_ws, deep_reads_ws, index)

    logger.info("Daily pipeline complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
