"""Weekly synthesis: step 8 of the literature tracker.

Entry point: `uv run lit-weekly` (see pyproject.toml [project.scripts]).

Deterministic templating over data the daily pipeline already stored -- no
extra LLM call, fully predictable output. Scoped by arXiv `published_date`
(see reporting.py) over a trailing window from today -- a manual backfill
run for some other historical range doesn't leak into this week's email
just because it happened to finish processing during this week.

The actual paper-list/cost/triage-table logic lives in reporting.py, shared
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
    papers_ws = sheets_store.open_sheets(settings.google_sheets)
    papers_records = sheets_store.get_all_records(papers_ws)

    window_end = datetime.now(timezone.utc).date()
    window_start = window_end - timedelta(days=settings.weekly_report.lookback_days)

    papers = reporting.collect_report_papers(
        papers_records,
        window_start,
        window_end,
        score_threshold=settings.triage.score_threshold,
    )
    mid_tier_papers = reporting.collect_mid_tier_papers(
        papers_records,
        window_start,
        window_end,
        mid_summary_threshold=settings.triage.mid_summary_threshold,
        score_threshold=settings.triage.score_threshold,
    )
    costs = reporting.compute_cost_summary(papers_records, window_start, window_end)
    shown_ids = {p.arxiv_id for p in papers} | {p.arxiv_id for p in mid_tier_papers}
    triage_rows, triage_total = reporting.collect_low_tier_table(papers_records, shown_ids, window_start, window_end)
    logger.info(
        "Weekly report covers %d full paper(s), %d mid-tier; estimated cost $%.4f "
        "(triage $%.4f, mid-summary $%.4f, deep-read $%.4f)",
        len(papers),
        len(mid_tier_papers),
        costs.total_cost_usd,
        costs.triage_cost_usd,
        costs.mid_summary_cost_usd,
        costs.deep_read_cost_usd,
    )

    # report_title (no date -- shown in the email body) and subject (keeps
    # the date -- shown in the mail client's subject line) deliberately
    # diverge here.
    html, text = reporting.render_report(
        report_title=settings.weekly_report.subject_prefix,
        papers=papers,
        mid_tier_papers=mid_tier_papers,
        costs=costs,
        triage_rows=triage_rows,
        triage_total=triage_total,
        window_start=window_start,
        window_end=window_end,
    )
    subject = (
        f"{settings.weekly_report.subject_prefix}: "
        f"{reporting.format_date_long(window_start)} - {reporting.format_date_long(window_end)}"
    )
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
