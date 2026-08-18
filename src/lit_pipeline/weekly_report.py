"""Weekly synthesis: step 8 of the literature tracker.

Entry point: `uv run lit-weekly` (see pyproject.toml [project.scripts]).

Deterministic templating over data the daily pipeline already stored -- no
extra LLM call, fully predictable output. Scoped by `date_field="processed"`
(see reporting.py): a paper only becomes "reportable" once its deep read
actually finishes, and a trailing window naturally never re-includes a paper
once it ages out, with no separate "already emailed" flag needed.

The actual paper-list/cost/histogram logic lives in reporting.py, shared
with backfill.py (which scopes by arXiv publish date over an explicit range
instead of a trailing window from today).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from lit_pipeline import reporting, sheets_store
from lit_pipeline.config import load_settings
from lit_pipeline.email_resend import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    load_dotenv()
    settings = load_settings()
    papers_ws, deep_reads_ws = sheets_store.open_sheets(settings.google_sheets)
    papers_records = papers_ws.get_all_records()
    deep_read_records = deep_reads_ws.get_all_records()

    window_end = datetime.now(timezone.utc).date()
    window_start = window_end - timedelta(days=settings.weekly_report.lookback_days)

    papers = reporting.collect_report_papers(
        papers_records, deep_read_records, window_start, window_end, date_field="processed"
    )
    costs = reporting.compute_cost_summary(
        papers_records, deep_read_records, window_start, window_end, date_field="processed"
    )
    histogram = reporting.compute_score_histogram(
        papers_records, window_start, window_end, date_field="processed"
    )
    logger.info(
        "Weekly report covers %d paper(s); estimated cost $%.4f (triage $%.4f, deep-read $%.4f)",
        len(papers),
        costs.total_cost_usd,
        costs.triage_cost_usd,
        costs.deep_read_cost_usd,
    )

    html, text = reporting.render_report(
        report_title=settings.weekly_report.subject_prefix,
        papers=papers,
        costs=costs,
        histogram=histogram,
        window_start=window_start,
        window_end=window_end,
    )
    subject = f"{settings.weekly_report.subject_prefix}: {len(papers)} paper(s)"
    send_email(
        sender=settings.weekly_report.sender_email,
        recipient=settings.weekly_report.recipient_email,
        subject=subject,
        html=html,
        text=text,
    )
    logger.info("Weekly report sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
