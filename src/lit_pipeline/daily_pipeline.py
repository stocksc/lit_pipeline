"""Daily pipeline: steps 1-6 of the literature tracker.

Entry point: `uv run lit-daily` (see pyproject.toml [project.scripts]).

Safe to re-run at any time. Every stage checkpoints its progress in the
`papers` sheet's `status` column (see sheets_store.py), so a crash mid-run
just means the next run picks up where it left off -- nothing needs to be
tracked outside the sheet itself.

Ingest harvests arXiv by date from the last day it fully covered (the
`harvest_state` checkpoint), not just "the last N papers," so if a day's
run fails outright, the next successful run covers that day too.

The triage/deep-read stages themselves live in pipeline_stages.py, shared
with backfill.py (manual runs over an arbitrary past date range).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic
from dotenv import load_dotenv

from lit_pipeline import sheets_store
from lit_pipeline.arxiv_client import harvest_candidates
from lit_pipeline.config import load_settings
from lit_pipeline.pipeline_stages import run_deep_read_stage, run_mid_summary_stage, run_triage_stage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    load_dotenv()
    settings = load_settings()
    anthropic_client = Anthropic()
    papers_ws = sheets_store.open_sheets(settings.google_sheets)

    # Start from the checkpoint day itself, not the day after: records
    # stamped later that day (after the last run) would otherwise be
    # skipped. Anything re-harvested is dropped by the dedup below.
    today = datetime.now(timezone.utc).date()
    from_date = sheets_store.load_harvest_checkpoint(papers_ws) or today - timedelta(days=settings.arxiv.max_age_days)
    logger.info("Harvesting arXiv records changed %s to %s...", from_date, today)
    candidates = harvest_candidates(settings.arxiv, from_date, today)

    index = sheets_store.load_papers_index(papers_ws)
    new_candidates = [c for c in candidates if c.arxiv_id not in index]
    sheets_store.append_new_candidates(papers_ws, new_candidates, added_by=sheets_store.ADDED_BY_DAILY)
    # Only once the harvest's papers are safely in the sheet -- a run that
    # dies before this leaves the checkpoint alone, so the next run re-covers
    # the same days.
    sheets_store.save_harvest_checkpoint(papers_ws, today)

    # Re-read so newly appended rows have row numbers and are visible to triage.
    # Rows a backfill or deep dive added are left to that command -- otherwise
    # a --dry-run's high scorers would get deep-read here, unasked.
    index = {
        arxiv_id: row
        for arxiv_id, row in sheets_store.load_papers_index(papers_ws).items()
        if sheets_store.daily_owns(row)
    }

    run_triage_stage(anthropic_client, settings, papers_ws, index)
    run_mid_summary_stage(anthropic_client, settings, papers_ws, index)
    run_deep_read_stage(anthropic_client, settings, papers_ws, index)

    logger.info("Daily pipeline complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
