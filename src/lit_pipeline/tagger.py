"""Tag papers in the sheet with their research-taxonomy category.

Entry point: `uv run lit-tag` (see pyproject.toml [project.scripts]).

Every triaged paper carries two tags, mirroring original_triage_score and
triage_score (see sheets_store.PAPERS_HEADERS):

- original_taxonomy: placed from the title and abstract alone.
- taxonomy: the current tag. A paper with a deep read is placed again with
  the deep-read summary in view, since the full text can show it belongs
  elsewhere; otherwise it's the same as original_taxonomy.

A paper scoring below taxonomy.min_score is tagged "other" with no API call:
the original tag by its original score, the current tag by its current one.
So a paper a deep read re-rated below the threshold keeps its abstract-based
category in original_taxonomy and becomes "other" in taxonomy.

New papers are tagged as they're triaged and deep-read (pipeline_stages.py),
with the same call this uses (llm_tag.py). This command is the backfill for
papers triaged before tagging existed, the catch-up for a paper left untagged
because its tagging call failed, and the way to re-tag after editing
config/taxonomy.yaml.

By default it touches only rows whose tags are missing, name a category the
taxonomy no longer has, or don't match the paper's score (a category on a
paper now below the threshold, say). So a re-run after an interruption picks
up where it stopped, and a re-run after renaming or removing a category
re-tags just the papers that were in it. --all re-tags every row in scope,
e.g. after rewriting definitions.
--start-date and --end-date limit the scope by publish date; --limit caps how
many papers go to Claude, for a look at a sample before a full run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date

from anthropic import Anthropic
from dotenv import load_dotenv
from gspread import Worksheet
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from lit_pipeline import reporting, sheets_store
from lit_pipeline.config import Settings, load_settings
from lit_pipeline.llm_tag import tag_paper
from lit_pipeline.sheets_store import PaperRow
from lit_pipeline.taxonomy import OTHER, Taxonomy, load_taxonomy

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=False, log_time_format="[%X]")],
)
logger = logging.getLogger(__name__)

# Per-request INFO lines from the HTTP and API clients would bury the
# progress bar; their warnings and errors still print. Quieted in main(), so
# importing this module doesn't change what other commands print.
QUIETED_LOGGERS = ("httpx", "anthropic")

# Rows per sheet write while calling Claude: few enough that an interrupted
# run loses little, and each batch takes long enough to tag that writes stay
# far inside the Sheets API's quota of 60 a minute.
TAG_BATCH_ROWS = 8

# Rough per-call figure for --dry-run: with the taxonomy cached, a call is
# mostly the paper's few hundred input tokens plus thinking and a one-line
# answer. The 2018-2021 backfill averaged half a cent on Opus 5.5.
ESTIMATED_COST_PER_CALL = 0.006


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tag papers in the sheet with their research-taxonomy category.")
    parser.add_argument("--start-date", type=date.fromisoformat, help="Only papers published on/after this date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="Only papers published on/before this date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="Re-tag every paper in scope, not just missing or stale tags")
    parser.add_argument("--limit", type=int, metavar="N", help="Send at most N papers to Claude this run")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be tagged and a rough cost, then stop")
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.end_date < args.start_date:
        parser.error("--end-date must not be before --start-date")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


@dataclass
class TagJob:
    row: PaperRow
    original_score: int
    score: int
    abstract_call: bool  # tag from the abstract alone
    deep_call: bool  # tag again with the deep-read summary

    @property
    def calls(self) -> int:
        return int(self.abstract_call) + int(self.deep_call)


def original_score(row: PaperRow) -> int:
    """original_triage_score, falling back to the current score on rows
    triaged before that column existed (same rule as reporting.py)."""
    raw = str(row.raw.get("original_triage_score", "")).strip()
    return int(raw) if raw.isdigit() else (row.triage_score or 0)


def tag_fits(tag: str, score: int, taxonomy: Taxonomy, min_score: int) -> bool:
    """Whether an existing tag is still right for a paper with this score."""
    if score < min_score:
        return tag == OTHER
    return tag != OTHER and taxonomy.is_tag(tag)


def plan_jobs(rows: list[PaperRow], taxonomy: Taxonomy, min_score: int, retag_all: bool) -> list[TagJob]:
    """The rows that need tags, and which calls each needs. Untriaged rows
    are skipped; they're tagged once they have a score."""
    jobs: list[TagJob] = []
    for row in rows:
        if row.triage_score is None:
            continue
        score = row.triage_score
        original = original_score(row)
        existing_original = str(row.raw.get("original_taxonomy", "")).strip()
        existing_current = str(row.raw.get("taxonomy", "")).strip()
        if not retag_all and tag_fits(existing_original, original, taxonomy, min_score) and tag_fits(
            existing_current, score, taxonomy, min_score
        ):
            continue
        has_deep_read = bool(str(row.raw.get("deep_read_summary", "")).strip())
        jobs.append(
            TagJob(
                row=row,
                original_score=original,
                score=score,
                abstract_call=original >= min_score or (score >= min_score and not has_deep_read),
                deep_call=score >= min_score and has_deep_read,
            )
        )
    return jobs


def in_scope(row: PaperRow, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    published = reporting.parse_date(str(row.raw.get("published_date", "")))
    if published is None:
        return False
    return (start is None or published >= start) and (end is None or published <= end)


class _Spend:
    def __init__(self) -> None:
        self.calls = 0
        self.cost = 0.0
        self.cache_reads = 0

    def add(self, usage) -> None:
        self.calls += 1
        self.cost += usage.cost_usd
        self.cache_reads += int(usage.cache_read_input_tokens > 0)


def tags_for(job: TagJob, client: Anthropic, settings: Settings, taxonomy: Taxonomy, spend: _Spend) -> dict[str, str]:
    raw = job.row.raw
    min_score = settings.taxonomy.min_score
    title, abstract = str(raw.get("title", "")), str(raw.get("abstract", ""))
    abstract_tag = None
    if job.abstract_call:
        abstract_tag, usage = tag_paper(client, settings, taxonomy, title, abstract)
        spend.add(usage)
    if job.score < min_score:
        current = OTHER
    elif job.deep_call:
        relevance = "; ".join(s.strip() for s in str(raw.get("deep_read_relevance", "")).split("|") if s.strip())
        current, usage = tag_paper(
            client, settings, taxonomy, title, abstract,
            deep_read_summary=str(raw.get("deep_read_summary", "")), deep_read_relevance=relevance,
        )
        spend.add(usage)
    else:
        current = abstract_tag
    return {"original_taxonomy": abstract_tag if job.original_score >= min_score else OTHER, "taxonomy": current}


def _scope_label(args: argparse.Namespace) -> str:
    if args.start_date or args.end_date:
        return f"papers published {args.start_date or 'any time'} to {args.end_date or 'now'}"
    return "every paper in the sheet"


def _print_branch_summary(taxonomy: Taxonomy, tags: list[str]) -> None:
    counts = Counter()
    for tag in tags:
        branch = taxonomy.top_level(tag)
        counts[f"{branch.number} {branch.label}" if branch else taxonomy.display(tag)] += 1
    for name, n in sorted(counts.items(), key=lambda kv: (kv[0] in ("Other", "Unsorted"), kv[0])):
        console.print(f"    {n:>5,}  {name}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    for name in QUIETED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    settings = load_settings()
    taxonomy = load_taxonomy(settings.taxonomy.file)
    min_score = settings.taxonomy.min_score
    # Opening the sheet adds the two tag columns the first time this runs.
    papers_ws: Worksheet = sheets_store.open_sheets(settings.google_sheets)
    index = sheets_store.load_papers_index(papers_ws)
    rows = [row for row in index.values() if in_scope(row, args.start_date, args.end_date)]
    triaged = sum(1 for row in rows if row.triage_score is not None)
    jobs = plan_jobs(rows, taxonomy, min_score, args.all)

    free_jobs = [job for job in jobs if job.calls == 0]
    call_jobs = [job for job in jobs if job.calls]
    console.print(
        f"[bold]lit-tag[/] {_scope_label(args)}: {len(rows):,} rows, {triaged:,} triaged, "
        f"{triaged - len(jobs):,} already tagged"
    )
    console.print(
        f"  To tag: {len(jobs):,} ({len(free_jobs):,} below {min_score}, tagged 'other' at no cost; "
        f"{len(call_jobs):,} sent to Claude, {sum(j.calls for j in call_jobs):,} call(s))"
    )
    if args.limit is not None and len(call_jobs) > args.limit:
        call_jobs = call_jobs[: args.limit]
        console.print(f"  --limit: sending the first {len(call_jobs)} of them this run")
    planned_calls = sum(job.calls for job in call_jobs)
    if args.dry_run:
        console.print(
            f"Dry run: about ${planned_calls * ESTIMATED_COST_PER_CALL:.2f} for {planned_calls:,} call(s) "
            f"(at roughly ${ESTIMATED_COST_PER_CALL:.3f} each). Nothing written."
        )
        return 0

    began = time.monotonic()
    pending: list[dict] = []
    written_rows = 0
    results: list[str] = []
    failed: list[str] = []
    spend = _Spend()

    def stage(row: PaperRow, tags: dict[str, str]) -> None:
        nonlocal written_rows
        pending.extend(sheets_store.build_cell_updates(row.row_number, tags))
        results.append(tags["taxonomy"])
        written_rows += 1

    try:
        # The free rows go in one write: there's nothing to lose by redoing
        # them, and hundreds of them a batch at a time would hit the quota.
        for job in free_jobs:
            stage(job.row, {"original_taxonomy": OTHER, "taxonomy": OTHER})
        sheets_store.flush_cell_updates(papers_ws, pending)
        if call_jobs:
            client = Anthropic()
            progress = Progress(
                TextColumn("[bold]Tagging[/]"),
                BarColumn(bar_width=28),
                MofNCompleteColumn(),
                TextColumn("papers"),
                TimeElapsedColumn(),
                TextColumn("elapsed,"),
                TimeRemainingColumn(),
                TextColumn("left  {task.fields[status]}"),
                console=console,
            )
            with progress:
                task = progress.add_task("tag", total=len(call_jobs), status="")
                for job in call_jobs:
                    try:
                        tags = tags_for(job, client, settings, taxonomy, spend)
                    except Exception as exc:  # isolate a failure to this one paper; a re-run retries it
                        failed.append(job.row.arxiv_id)
                        logger.warning("Tagging failed for %s: %s", job.row.arxiv_id, exc)
                    else:
                        stage(job.row, tags)
                        if len(pending) >= TAG_BATCH_ROWS * len(tags):
                            sheets_store.flush_cell_updates(papers_ws, pending)
                    progress.update(task, advance=1, status=f"${spend.cost:.2f} spent")
    except KeyboardInterrupt:
        sheets_store.flush_cell_updates(papers_ws, pending)
        console.print(
            f"\n[yellow]Stopped.[/] {written_rows:,} paper(s) tagged so far are saved; "
            "run the same command again to tag the rest."
        )
        return 130
    sheets_store.flush_cell_updates(papers_ws, pending)

    minutes, seconds = divmod(int(time.monotonic() - began), 60)
    console.print(f"\n[bold green]Done[/] in {minutes}:{seconds:02d}.")
    console.print(
        f"  Tagged     {written_rows:,} paper(s) with {spend.calls:,} call(s) for ${spend.cost:.2f}"
        + (f"; {len(failed)} failed (run again to retry): {', '.join(failed)}" if failed else "")
    )
    if spend.calls:
        console.print(f"  Cache      {spend.cache_reads:,} of {spend.calls:,} call(s) read the taxonomy from cache")
    if results:
        console.print("  Current tags by branch:")
        _print_branch_summary(taxonomy, results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
