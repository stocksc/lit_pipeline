"""Thin wrapper around gspread for the two-tab papers/deep_reads sheet.

Auth uses a Google Cloud *service account* -- a "robot" Google identity you
create once and share your Sheet with (Editor access), so headless code
(GitHub Actions) can read/write without an interactive browser login. See
README.md for one-time setup steps.

The `papers` tab's `status` column is the pipeline's only checkpoint: a run
can be interrupted at any point and simply re-run, because every stage reads
`status` to figure out what's already done vs. still pending. See
`load_papers_index()` and the status constants below.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from lit_pipeline.arxiv_client import PaperCandidate
from lit_pipeline.config import GoogleSheetsSettings
from lit_pipeline.pricing import LLMUsage
from lit_pipeline.schemas import DeepReadResult

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PAPERS_HEADERS = [
    "arxiv_id", "title", "authors", "published_date", "abstract", "link",
    "status", "triage_score", "triage_rationale", "matched_interest",
    "triage_input_tokens", "triage_output_tokens", "triage_cost_usd",
    "ingested_at", "triaged_at", "last_error", "retry_count",
    # Appended at the end (rather than interspersed) so this migrates onto
    # an existing sheet by just adding trailing columns, not reordering.
    "mid_summary", "mid_summary_input_tokens", "mid_summary_output_tokens",
    "mid_summary_cost_usd", "mid_summary_at",
    # The Haiku triage score, written once and never touched again --
    # `triage_score` itself gets overwritten with Opus's re-rating after a
    # deep-read, so this is the only place the original guess survives.
    "original_triage_score",
]

DEEP_READS_HEADERS = [
    "arxiv_id", "summary", "relevance", "limitations", "author_affiliations",
    "deep_read_input_tokens", "deep_read_output_tokens", "deep_read_cost_usd",
    "deep_read_at",
    # Opus's own re-rating of the paper, based on the full text rather than
    # just the abstract. This is also copied onto `papers.triage_score` so
    # report tiering always reflects the best-known score.
    "score",
]

# --- status lifecycle -------------------------------------------------
# A triaged row's score routes it to exactly one of two further stages:
# score >= score_threshold -> deep-read; mid_summary_threshold <= score <
# score_threshold -> mid-summary; below mid_summary_threshold -> stays
# "triaged" forever (title-only in reports, no further LLM calls).
STATUS_INGESTED = "ingested"
STATUS_TRIAGED = "triaged"
STATUS_MID_SUMMARY_COMPLETE = "mid_summary_complete"
STATUS_DEEP_READ_COMPLETE = "deep_read_complete"
STATUS_TRIAGE_ERROR = "triage_error"
STATUS_MID_SUMMARY_ERROR = "mid_summary_error"
STATUS_DEEP_READ_ERROR = "deep_read_error"
STATUS_TRIAGE_FAILED_PERMANENT = "triage_failed_permanent"
STATUS_MID_SUMMARY_FAILED_PERMANENT = "mid_summary_failed_permanent"
STATUS_DEEP_READ_FAILED_PERMANENT = "deep_read_failed_permanent"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_client() -> gspread.Client:
    """Authenticate as the service account.

    Reads credentials from GOOGLE_SERVICE_ACCOUNT_JSON (the whole key file's
    contents -- what you'll paste into the GitHub Actions secret) or, for
    local dev convenience, GOOGLE_SERVICE_ACCOUNT_FILE (a path to the
    downloaded key file on your machine). Never commit the key file itself.
    """
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        info = json.loads(raw_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not key_path:
            raise RuntimeError(
                "Set GOOGLE_SERVICE_ACCOUNT_JSON (key file contents) or "
                "GOOGLE_SERVICE_ACCOUNT_FILE (path to the key file) in your environment."
            )
        creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str, headers: list[str]
) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws
    if not ws.row_values(1):
        ws.append_row(headers)
    return ws


def open_sheets(settings: GoogleSheetsSettings) -> tuple[gspread.Worksheet, gspread.Worksheet]:
    client = get_client()
    spreadsheet = client.open_by_key(settings.sheet_id)
    papers_ws = _get_or_create_worksheet(spreadsheet, settings.papers_tab, PAPERS_HEADERS)
    deep_reads_ws = _get_or_create_worksheet(spreadsheet, settings.deep_reads_tab, DEEP_READS_HEADERS)
    return papers_ws, deep_reads_ws


def get_all_records(ws: gspread.Worksheet) -> list[dict]:
    """`Worksheet.get_all_records()`, but with gspread's automatic
    int/float coercion disabled for every column.

    Without this, gspread "numericises" any cell that looks like a number --
    including arxiv_id, which is numeric-looking (e.g. "2607.03180"). A
    trailing zero in the 5-digit suffix is insignificant to a float, so
    "2607.03180" silently becomes 2607.0318 on read, corrupting the id. That
    breaks dedup (the corrupted key never matches a freshly-computed clean
    id, so the paper looks "new" and gets re-ingested every run) and cross-
    tier joins in reporting.py. Every numeric field we actually care about
    (scores, token counts, costs, retry_count) is already parsed defensively
    from strings elsewhere in this codebase (_safe_int/_safe_float/.isdigit()
    checks), so disabling numericise entirely is safe."""
    return ws.get_all_records(numericise_ignore=["all"])


@dataclass
class PaperRow:
    row_number: int  # 1-indexed sheet row (row 1 is the header)
    arxiv_id: str
    status: str
    retry_count: int
    triage_score: int | None
    raw: dict[str, Any]


def load_papers_index(papers_ws: gspread.Worksheet) -> dict[str, PaperRow]:
    """Read the whole `papers` tab into memory, keyed by arxiv_id.

    This is the basis for both dedup (never re-ingest a known id) and resume
    (pick back up any row a prior crashed run left mid-stage, using `status`).
    """
    records = get_all_records(papers_ws)
    index: dict[str, PaperRow] = {}
    for i, record in enumerate(records):
        arxiv_id = str(record.get("arxiv_id", "")).strip()
        if not arxiv_id:
            continue
        retry_count_raw = str(record.get("retry_count", 0)).strip()
        retry_count = int(retry_count_raw) if retry_count_raw.isdigit() else 0
        score_raw = str(record.get("triage_score", "")).strip()
        triage_score = int(score_raw) if score_raw.isdigit() else None
        index[arxiv_id] = PaperRow(
            row_number=i + 2,  # +1 for 1-indexing, +1 for the header row
            arxiv_id=arxiv_id,
            status=str(record.get("status", "")),
            retry_count=retry_count,
            triage_score=triage_score,
            raw=record,
        )
    return index


def append_new_candidates(papers_ws: gspread.Worksheet, candidates: list[PaperCandidate]) -> None:
    if not candidates:
        return
    now = now_iso()
    rows = []
    for c in candidates:
        row = {h: "" for h in PAPERS_HEADERS}
        row.update(
            arxiv_id=c.arxiv_id,
            title=c.title,
            authors=c.authors,
            published_date=c.published_date,
            abstract=c.abstract,
            link=c.link,
            status=STATUS_INGESTED,
            ingested_at=now,
            retry_count=0,
        )
        rows.append([row[h] for h in PAPERS_HEADERS])
    papers_ws.append_rows(rows, value_input_option="RAW")
    logger.info("Appended %d new candidate(s) to %r", len(rows), papers_ws.title)


def _col_letter(index: int) -> str:
    """1-indexed column number -> spreadsheet column letter (1 -> 'A', 27 -> 'AA')."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def build_cell_updates(row_number: int, values: dict[str, object]) -> list[dict]:
    """Turn {column_name: value} into gspread batch_update entries for one row."""
    updates = []
    for key, value in values.items():
        col = PAPERS_HEADERS.index(key) + 1
        updates.append({"range": f"{_col_letter(col)}{row_number}", "values": [[value]]})
    return updates


def flush_cell_updates(papers_ws: gspread.Worksheet, batched: list[dict]) -> None:
    """Send a batch of {range, values} cell updates to `papers` in one API call."""
    if not batched:
        return
    papers_ws.batch_update(batched, value_input_option="RAW")
    batched.clear()


def append_deep_read(
    deep_reads_ws: gspread.Worksheet, arxiv_id: str, result: DeepReadResult, usage: LLMUsage
) -> None:
    row = [
        arxiv_id,
        result.summary,
        " | ".join(result.relevance),
        " | ".join(result.limitations),
        " | ".join(result.author_affiliations),
        usage.input_tokens,
        usage.output_tokens,
        round(usage.cost_usd, 6),
        now_iso(),
        result.score,
    ]
    deep_reads_ws.append_row(row, value_input_option="RAW")
