"""Manually backfill the pipeline for an arbitrary past date range.

Entry point: `uv run lit-backfill --start-date 2024-01-01 --end-date 2024-01-31`
(see pyproject.toml [project.scripts]).

Runs the same ingest -> triage -> deep-read -> email pipeline as the daily/
weekly cron jobs, but scoped to an explicit [start_date, end_date] window
matched against each paper's arXiv *publish* date (not when the backfill
happens to run) -- see arxiv_client.fetch_candidates and reporting.py's
date_field="published" mode. Reuses the exact same triage/deep-read stage
functions as the daily job (pipeline_stages.py), just handed a pre-filtered
index so a backfill run never touches unrelated pending rows left over from
regular daily runs.

Use --dry-run first for anything beyond a narrow window: deep-reading is a
real per-paper Opus cost, and a broad query over a wide date range can
easily turn up hundreds of papers.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from anthropic import Anthropic
from dotenv import load_dotenv

from lit_pipeline import reporting, sheets_store
from lit_pipeline.arxiv_client import fetch_candidates
from lit_pipeline.config import load_settings
from lit_pipeline.email_resend import send_email
from lit_pipeline.pipeline_stages import run_deep_read_stage, run_mid_summary_stage, run_triage_stage
from lit_pipeline.sheets_store import PaperRow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill ingest -> triage -> deep-read -> email for a past date range."
    )
    parser.add_argument("--start-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        metavar="QUERY",
        help="Override config/settings.yaml's arxiv.queries for this run only; repeatable",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        help="Override triage.score_threshold from settings.yaml for this run only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ingest + triage + mid-summary only; print a score breakdown and stop before deep-read/email",
    )
    return parser.parse_args(argv)


def _scope_to_published_range(index: dict[str, PaperRow], start: date, end: date) -> dict[str, PaperRow]:
    scoped: dict[str, PaperRow] = {}
    for arxiv_id, row in index.items():
        published = reporting.parse_date(str(row.raw.get("published_date", "")))
        if published is not None and start <= published <= end:
            scoped[arxiv_id] = row
    return scoped


def _records_from_index(index: dict[str, PaperRow]) -> list[dict]:
    """Builds a papers_records-shaped list straight from in-memory rows, with
    `triage_score` overridden from the live `PaperRow.triage_score` attribute
    -- `row.raw` is a snapshot from before run_triage_stage ran and is never
    updated in place, so reading it directly here would see stale/blank
    scores for rows that were just triaged this run."""
    records = []
    for row in index.values():
        record = dict(row.raw)
        record["triage_score"] = row.triage_score if row.triage_score is not None else ""
        records.append(record)
    return records


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    if args.end_date < args.start_date:
        raise SystemExit("--end-date must not be before --start-date")

    settings = load_settings()
    if args.queries:
        settings.arxiv.queries = args.queries
    if args.threshold is not None:
        settings.triage.score_threshold = args.threshold

    client = Anthropic()
    papers_ws, deep_reads_ws = sheets_store.open_sheets(settings.google_sheets)

    logger.info("Searching arXiv %s to %s...", args.start_date, args.end_date)
    candidates = fetch_candidates(settings.arxiv, published_after=args.start_date, published_before=args.end_date)
    logger.info("Found %d candidate(s) in range", len(candidates))

    index = sheets_store.load_papers_index(papers_ws)
    new_candidates = [c for c in candidates if c.arxiv_id not in index]
    sheets_store.append_new_candidates(papers_ws, new_candidates)
    index = sheets_store.load_papers_index(papers_ws)

    scoped_index = _scope_to_published_range(index, args.start_date, args.end_date)
    logger.info("%d paper(s) in range are tracked in the sheet (new + previously seen)", len(scoped_index))

    run_triage_stage(client, settings, papers_ws, scoped_index)
    run_mid_summary_stage(client, settings, papers_ws, scoped_index)

    papers_records = _records_from_index(scoped_index)
    histogram = reporting.compute_score_histogram(papers_records, args.start_date, args.end_date, date_field="published")
    total_triaged = sum(histogram.values())
    at_or_above = sum(count for score, count in histogram.items() if score >= settings.triage.score_threshold)

    logger.info("Score breakdown for %s to %s (%d triaged):", args.start_date, args.end_date, total_triaged)
    for score in range(10, -1, -1):
        if histogram[score]:
            logger.info("  %2d: %d", score, histogram[score])
    logger.info(
        "%d paper(s) scoring >= %d (threshold) %s",
        at_or_above,
        settings.triage.score_threshold,
        "would be deep-read on a full run." if args.dry_run else "will be deep-read now.",
    )

    if args.dry_run:
        logger.info("Dry run complete -- no deep-read or email performed.")
        return 0

    run_deep_read_stage(client, settings, papers_ws, deep_reads_ws, scoped_index)

    papers_records = sheets_store.get_all_records(papers_ws)
    deep_read_records = sheets_store.get_all_records(deep_reads_ws)
    report_papers = reporting.collect_report_papers(
        papers_records, deep_read_records, args.start_date, args.end_date, date_field="published"
    )
    mid_tier_papers = reporting.collect_mid_tier_papers(
        papers_records, args.start_date, args.end_date, date_field="published"
    )
    costs = reporting.compute_cost_summary(
        papers_records, deep_read_records, args.start_date, args.end_date, date_field="published"
    )
    shown_ids = {p.arxiv_id for p in report_papers} | {p.arxiv_id for p in mid_tier_papers}
    triage_rows, triage_total = reporting.collect_low_tier_table(
        papers_records, shown_ids, args.start_date, args.end_date, date_field="published"
    )

    # report_title (no date -- shown in the email body) and subject (keeps
    # the date -- shown in the mail client's subject line) deliberately
    # diverge here.
    html, text = reporting.render_report(
        report_title=settings.weekly_report.subject_prefix,
        papers=report_papers,
        mid_tier_papers=mid_tier_papers,
        costs=costs,
        triage_rows=triage_rows,
        triage_total=triage_total,
        window_start=args.start_date,
        window_end=args.end_date,
    )
    subject = (
        f"{settings.weekly_report.subject_prefix}: "
        f"{reporting.format_date_long(args.start_date)} - {reporting.format_date_long(args.end_date)}"
    )
    send_email(
        sender=settings.weekly_report.sender_email,
        recipient=settings.weekly_report.recipient_email,
        subject=subject,
        html=html,
        text=text,
    )
    logger.info(
        "Backfill complete: %d paper(s) deep-read, %d mid-tier, emailed.",
        len(report_papers),
        len(mid_tier_papers),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
