"""Daily pipeline: steps 1-6 of the literature tracker.

Entry point: `uv run lit-daily` (see pyproject.toml [project.scripts]).

Safe to re-run at any time. Every stage checkpoints its progress in the
`papers` sheet's `status` column (see sheets_store.py), so a crash mid-run
just means the next run picks up where it left off -- nothing needs to be
tracked outside the sheet itself.

The triage/deep-read stages themselves live in pipeline_stages.py, shared
with backfill.py (manual runs over an arbitrary past date range).
"""

from __future__ import annotations

import logging
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

from lit_pipeline import sheets_store
from lit_pipeline.arxiv_client import fetch_candidates
from lit_pipeline.config import load_settings
from lit_pipeline.pipeline_stages import run_deep_read_stage, run_mid_summary_stage, run_triage_stage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


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
    run_mid_summary_stage(anthropic_client, settings, papers_ws, index)
    run_deep_read_stage(anthropic_client, settings, papers_ws, deep_reads_ws, index)

    logger.info("Daily pipeline complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
