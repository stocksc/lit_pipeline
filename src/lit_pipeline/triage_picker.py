"""Triage every paper in a date range, then hand-pick which ones get deep-read.

Entry point: `uv run lit-pick --start-date 2023-01-01 --end-date 2023-12-31`
(see pyproject.toml [project.scripts]).

backfill.py decides what's worth Opus's time by score alone. This is for when
you'd rather decide yourself: it runs the same keyword search and the same
triage stage over a window, then writes a standalone HTML page of every
triaged paper -- score, rationale, authors, collapsible abstract -- with a
checkbox on each, browsable by research-taxonomy category as well as by
search, score and date. The page opens in your browser; tick what you want,
then copy the ids (or a ready-made `lit-deep-dive` command, or a .txt for
`lit-deep-dive --ids-file`) and hand them to manual_deep_dive.py. The page
itself never spends anything: nothing is deep-read until you run that
command.

The search runs a calendar month at a time. arXiv's search API won't page
past 10,000 results for one query, and a year of the standing keyword lists
can go past that in one go, while a month stays well under. Each month's new
papers are added to the sheet as soon as that month is fetched, and if arXiv
refuses a month (after the arxiv package's own retries) the search stops
there instead of pressing on into a server that's pushing back. Everything
fetched so far is still triaged and shown, the page says which part of the
window is missing, and the run exits non-zero with the command to finish it.

Mid-summaries are deliberately skipped: the triage rationale is what the
page shows, so a second cheap summary would be spend nobody reads.

A long run's terminal output is a live progress display (see _Display)
rather than a log: the libraries underneath narrate every request they make,
which over a few hundred arXiv pages and Claude calls buries anything worth
reading, so their INFO lines are kept out of it. Warnings and errors still
print, above the bars.

Rows this command adds are marked `added_by=picker`, so the daily job leaves
them alone (see sheets_store.daily_owns) and a high scorer here waits for
you to pick it rather than being deep-read by the next daily run. Same as
everywhere else in the pipeline, a run is safe to repeat: papers that are
already triaged are skipped, so re-running a window only pays for papers
that are new to the sheet.

--dry-run stops before triage, printing how many papers still need it and
roughly what that will cost, projected from the sheet's own historical
average. --no-fetch skips arXiv and works from what's already in the sheet:
it still triages anything in the window that's waiting, which is how a long
run that was interrupted mid-triage resumes without repeating the whole
search, and once everything is triaged it's a free rebuild of the page (the
way to refresh the "deep read on file" badges after a deep dive). The two
combine: --no-fetch --dry-run is a free count of what's waiting.
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import sys
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import arxiv
from anthropic import Anthropic
from dotenv import load_dotenv
from gspread import Worksheet
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Column
from rich.text import Text

from lit_pipeline import reporting, sheets_store
from lit_pipeline.arxiv_client import SEARCH_PAGE_SIZE, fetch_candidates
from lit_pipeline.config import PROJECT_ROOT, Settings, load_settings
from lit_pipeline.pipeline_stages import TRIAGE_BATCH_SIZE, rows_to_triage, run_triage_stage, scope_to_published_range
from lit_pipeline.pricing import LLMUsage
from lit_pipeline.schemas import TriageResult
from lit_pipeline.sheets_store import PaperRow
from lit_pipeline.taxonomy import load_taxonomy

# Everything user-facing goes through one rich Console, so log warnings print
# cleanly above the live progress bars instead of tearing through them.
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_path=False, markup=False, log_time_format="[%X]")],
)
logger = logging.getLogger(__name__)

# Loggers that narrate every step at INFO -- httpx with one line per Claude
# call, and this pipeline's own per-stage lines that the progress display
# now says better. Their warnings and errors still come through. (The arxiv
# package's logger is handled separately, by _ArxivPageWatcher.) Quieted in
# main() rather than at import, so importing this module doesn't change what
# any other command prints.
QUIETED_LOGGERS = (
    "httpx",
    "anthropic",
    "lit_pipeline.arxiv_client",
    "lit_pipeline.pipeline_stages",
    "lit_pipeline.sheets_store",
)

TITLE_STATUS_LENGTH = 30
BAR_WIDTH = 28

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

# arXiv asks for no more than one request every three seconds across its
# APIs. fetch_candidates spaces out its own requests, but each call makes a
# fresh client with no memory of the previous one, so the gap between one
# month's last request and the next month's first has to be kept here.
ARXIV_REQUEST_GAP_SECONDS = 3.0

# Rough per-paper triage figure, only used to say something useful on a
# --dry-run against a sheet with no triage history to average yet. Matches
# the README's per-triage cost.
FALLBACK_TRIAGE_COST_HINT = "roughly $0.01/paper"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Triage every paper in a date range and write an HTML page for hand-picking "
            "which ones get a deep dive."
        )
    )
    parser.add_argument("--start-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--query",
        action="append",
        dest="specific_keywords",
        metavar="KEYWORD",
        help="Override config/settings.yaml's arxiv.specific_keywords for this run only; repeatable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No triage: print how many papers need it and a rough $ cost, then stop without writing a page",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help=(
            "Don't search arXiv; work from what's already in the sheet. Triages anything in the window "
            "still waiting (resuming an interrupted run), so it's free once everything is triaged"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Where to write the page (default: output/picks_<start>_<end>.html)",
    )
    parser.add_argument("--no-open", action="store_true", help="Write the page without opening a browser")
    args = parser.parse_args(argv)
    if args.end_date < args.start_date:
        parser.error("--end-date must not be before --start-date")
    return args


class _ArxivPageWatcher(logging.Handler):
    """Counts a month's search pages for the progress display by reading the
    arxiv package's own request logging -- the only place it reports how big
    a search is -- without printing any of it. Its warnings are passed on."""

    REQUEST_LINE = re.compile(r"Requesting page \(first: (?:True|False), try: (\d+)\)")
    FIRST_PAGE_LINE = re.compile(r"Got first page: \d+ of (\d+) total results")

    def __init__(self, on_change) -> None:
        super().__init__()
        self.on_change = on_change
        self.reset()

    def reset(self) -> None:
        self.total_results: int | None = None
        self.pages_requested = 0
        self.retrying = False

    @property
    def total_pages(self) -> int | None:
        if self.total_results is None:
            return None
        return max(1, math.ceil(self.total_results / SEARCH_PAGE_SIZE))

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            logger.warning("arxiv: %s", message)
            return
        if match := self.REQUEST_LINE.match(message):
            self.retrying = int(match.group(1)) > 0
            if not self.retrying:
                self.pages_requested += 1
        elif match := self.FIRST_PAGE_LINE.match(message):
            self.total_results = int(match.group(1))
        elif message.startswith("Got empty first page"):
            self.total_results = 0
        else:
            return
        self.on_change()


class _StatusColumn(TextColumn):
    """The free-text end of a progress row, and the only column allowed to
    give up width when the terminal is too narrow for the whole row --
    truncated with an ellipsis rather than wrapped. Without it rich shrinks
    the bar instead, so the bar's length would change with the text beside it."""

    def __init__(self, text_format: str) -> None:
        # no_wrap=False is what marks a column as shrinkable to rich's table
        # layout; the Text rendered into it still never wraps (see render).
        super().__init__(text_format, table_column=Column(no_wrap=False))

    def render(self, task) -> Text:
        text = super().render(task)
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text


class _Display:
    """The live terminal display for a run: a progress bar for each phase,
    a line per finished month, and the totals the end-of-run summary prints.
    When output isn't a terminal (redirected to a file, say), rich prints
    the bars' final state instead of animating them."""

    def __init__(self) -> None:
        self.progress = Progress(
            TextColumn("[bold]{task.description:<15}"),
            BarColumn(bar_width=BAR_WIDTH, table_column=Column(width=BAR_WIDTH, no_wrap=True)),
            MofNCompleteColumn(),
            TextColumn("{task.fields[unit]}"),
            TimeElapsedColumn(),
            TextColumn("elapsed,"),
            TimeRemainingColumn(),
            TextColumn("left "),
            _StatusColumn("{task.fields[status]}"),
            console=console,
        )
        self.watcher = _ArxivPageWatcher(self._show_page)
        self.search_task = None
        self.triage_task = None
        self.month: date | None = None
        self.months_searched = self.results = self.matched = self.added = 0
        self.triaged = self.triage_failed = 0
        self.triage_spent = 0.0

    def __enter__(self) -> "_Display":
        arxiv_logger = logging.getLogger("arxiv")
        arxiv_logger.setLevel(logging.INFO)
        arxiv_logger.propagate = False
        arxiv_logger.addHandler(self.watcher)
        return self

    def __exit__(self, *exc_info) -> None:
        # Started only once a phase begins, so a run with nothing to search
        # or triage doesn't leave an empty progress area behind.
        if self.progress.live.is_started:
            self.progress.stop()
        arxiv_logger = logging.getLogger("arxiv")
        arxiv_logger.removeHandler(self.watcher)
        arxiv_logger.propagate = True

    def _add_task(self, description: str, total: int, unit: str):
        if not self.progress.live.is_started:
            self.progress.start()
        return self.progress.add_task(description, total=total, unit=unit, status="")

    # --- search phase
    def search_started(self, months: int) -> None:
        self.search_task = self._add_task("Searching arXiv", months, "months")

    def month_started(self, month_start: date) -> None:
        self.month = month_start
        self.watcher.reset()
        self._show_page()

    def _show_page(self) -> None:
        if self.search_task is None or self.month is None:
            return
        pages = self.watcher.total_pages
        status = f"{self.month:%Y-%m}: page {self.watcher.pages_requested}/{pages if pages is not None else '?'}"
        if self.watcher.retrying:
            status += " (arXiv slow, retrying)"
        self.progress.update(self.search_task, status=status)

    def month_done(self, month_start: date, matched: int, added: int) -> None:
        results = self.watcher.total_results or 0
        self.months_searched += 1
        self.results += results
        self.matched += matched
        self.added += added
        self.progress.console.print(
            f"  [green]{month_start:%Y-%m}[/]  {results:>6,} results  {matched:>4} match your keywords  {added:>4} new"
        )
        self.progress.advance(self.search_task)

    def search_stopped(self) -> None:
        if self.search_task is not None:
            self.progress.update(self.search_task, status="[red]stopped: arXiv refused[/]")

    # --- triage phase
    def triage_started(self, papers: int) -> None:
        self.triage_task = self._add_task("Triaging", papers, "papers")

    def paper_done(self, row: PaperRow, result: TriageResult | None, usage: LLMUsage | None) -> None:
        if result is None or usage is None:
            self.triage_failed += 1
            last = f"[red]failed[/] {row.arxiv_id}"
        else:
            self.triaged += 1
            self.triage_spent += usage.cost_usd
            title = str(row.raw.get("title", ""))
            if len(title) > TITLE_STATUS_LENGTH:
                title = title[: TITLE_STATUS_LENGTH - 3] + "..."
            # escape: a title like "[Re] Fairness..." would otherwise be read as rich markup.
            last = escape(title)
        self.progress.update(self.triage_task, advance=1, status=f"${self.triage_spent:.2f} spent · {last}")


def _month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into calendar-month pieces, clipped to the range at
    both ends: 2023-01-15..2023-03-10 -> Jan 15-31, Feb 1-28, Mar 1-10."""
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        next_month = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        windows.append((cursor, min(end, next_month - timedelta(days=1))))
        cursor = next_month
    return windows


def _fetch_and_ingest(
    settings: Settings, papers_ws: Worksheet, start: date, end: date, display: _Display
) -> date | None:
    """Search arXiv a month at a time, adding each month's new papers to the
    sheet as soon as it's fetched (see the module docstring).

    Returns None once every month is fetched, or the first day of the month
    arXiv refused, where fetching stopped. The months before it are already
    in the sheet, so nothing fetched so far is lost.
    """
    known_ids = set(sheets_store.load_papers_index(papers_ws))
    months = _month_windows(start, end)
    display.search_started(len(months))
    for i, (chunk_start, chunk_end) in enumerate(months):
        if i:
            time.sleep(ARXIV_REQUEST_GAP_SECONDS)
        display.month_started(chunk_start)
        try:
            candidates = fetch_candidates(settings.arxiv, published_after=chunk_start, published_before=chunk_end)
        # OSError covers a dropped connection: the arxiv package re-raises
        # requests' ConnectionError, which is an IOError, once its retries run out.
        except (arxiv.ArxivError, OSError) as exc:
            display.search_stopped()
            logger.error("arXiv refused the search for %s to %s: %s", chunk_start, chunk_end, exc)
            return chunk_start
        new_candidates = [c for c in candidates if c.arxiv_id not in known_ids]
        sheets_store.append_new_candidates(papers_ws, new_candidates, added_by=sheets_store.ADDED_BY_PICKER)
        known_ids.update(c.arxiv_id for c in new_candidates)
        display.month_done(chunk_start, matched=len(candidates), added=len(new_candidates))
    return None


def _log_how_to_finish(stopped_at: date, args: argparse.Namespace) -> None:
    logger.warning(
        "arXiv stopped responding, so %s to %s wasn't searched; everything before that is in "
        "the sheet. Give it an hour or so, then fetch the rest with\n"
        "  uv run lit-pick --start-date %s --end-date %s\n"
        "and rebuild the page for the whole window, without searching arXiv again, with\n"
        "  uv run lit-pick --start-date %s --end-date %s --no-fetch",
        stopped_at,
        args.end_date,
        stopped_at,
        args.end_date,
        args.start_date,
        args.end_date,
    )


def _print_triage_projection(papers_ws: Worksheet, scoped_count: int, pending: int) -> None:
    console.print(f"[bold]Dry run:[/] {pending:,} of {scoped_count:,} paper(s) in range still need triage.")
    if not pending:
        return
    avg_triage = reporting.compute_avg_triage_cost(sheets_store.get_all_records(papers_ws))
    if avg_triage is None:
        console.print(
            "  No triage cost history yet to project from; triage typically runs "
            f"{FALLBACK_TRIAGE_COST_HINT} as a rough starting estimate."
        )
        return
    avg_cost, n_history = avg_triage
    console.print(
        f"  Projected cost: ~${pending * avg_cost:.2f} "
        f"({pending:,} x ${avg_cost:.4f}, the average of {n_history:,} past triage(s) in the sheet)."
    )


def _print_summary(
    display: _Display,
    settings: Settings,
    papers: list[reporting.PickerPaper],
    out_path: Path,
    elapsed_seconds: float,
    searched: bool,
) -> None:
    minutes, seconds = divmod(int(elapsed_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    console.print(f"\n[bold green]Done[/] in {hours}:{minutes:02d}:{seconds:02d}.")
    if searched:
        console.print(
            f"  Searched   {display.months_searched} month(s): {display.results:,} results, "
            f"{display.matched:,} matched your keywords, {display.added:,} new to the sheet"
        )
    failed = f", {display.triage_failed} failed" if display.triage_failed else ""
    console.print(f"  Triaged    {display.triaged:,} paper(s) this run for ${display.triage_spent:.2f}{failed}")
    high = sum(1 for p in papers if p.triage_score >= settings.triage.score_threshold)
    mid = sum(1 for p in papers if settings.triage.mid_summary_threshold <= p.triage_score < settings.triage.score_threshold)
    console.print(
        f"  In window  {len(papers):,} triaged: {high:,} scored {settings.triage.score_threshold}+ "
        f"(your deep-read threshold), {mid:,} scored "
        f"{settings.triage.mid_summary_threshold}-{settings.triage.score_threshold - 1}, "
        f"{len(papers) - high - mid:,} lower"
    )
    console.print(f"  Page       {out_path.resolve()}")


def _print_interrupted(args: argparse.Namespace, display: _Display) -> None:
    if display.triage_task is not None:
        console.print(
            "\n[yellow]Stopped during triage.[/] Results are saved in batches of "
            f"{TRIAGE_BATCH_SIZE}, so up to {TRIAGE_BATCH_SIZE - 1} of the most recent will be redone. "
            "Pick up where it left off, without searching arXiv again, with\n"
            f"  uv run lit-pick --start-date {args.start_date} --end-date {args.end_date} --no-fetch"
        )
    elif display.month is not None:
        console.print(
            f"\n[yellow]Stopped while searching {display.month:%Y-%m}.[/] The months before it are in the "
            "sheet. Finish the search with\n"
            f"  uv run lit-pick --start-date {display.month} --end-date {args.end_date}\n"
            "then rebuild the page for the whole window with\n"
            f"  uv run lit-pick --start-date {args.start_date} --end-date {args.end_date} --no-fetch"
        )
    else:
        console.print("\n[yellow]Stopped.[/]")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    for name in QUIETED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    began = time.monotonic()

    settings = load_settings()
    if args.specific_keywords:
        settings.arxiv.specific_keywords = args.specific_keywords

    mode = " (dry run)" if args.dry_run else ""
    mode += " (no arXiv search)" if args.no_fetch else ""
    console.print(f"[bold]lit-pick[/] {args.start_date} to {args.end_date}{mode}")
    papers_ws = sheets_store.open_sheets(settings.google_sheets)

    stopped_at: date | None = None
    display = _Display()
    try:
        with display:
            if not args.no_fetch:
                stopped_at = _fetch_and_ingest(settings, papers_ws, args.start_date, args.end_date, display)

            index = sheets_store.load_papers_index(papers_ws)
            scoped_index = scope_to_published_range(index, args.start_date, args.end_date)
            pending = rows_to_triage(settings, scoped_index)

            # Runs after a partial fetch too (what did arrive is worth
            # triaging now, and a re-run won't pay for it again), and with
            # --no-fetch, which is how an interrupted triage resumes without
            # searching arXiv all over again.
            if pending and not args.dry_run:
                display.triage_started(len(pending))
                run_triage_stage(Anthropic(), settings, papers_ws, scoped_index, on_paper=display.paper_done)
    except KeyboardInterrupt:
        _print_interrupted(args, display)
        return 130

    if args.dry_run:
        _print_triage_projection(papers_ws, len(scoped_index), len(pending))
        if stopped_at:
            _log_how_to_finish(stopped_at, args)
        console.print("No triage run and no page written.")
        return 1 if stopped_at else 0

    all_records = sheets_store.get_all_records(papers_ws)
    # Same exclusion scope_to_published_range applies to the working set, so
    # the page shows exactly what this command searched and triaged.
    papers_records = reporting.exclude_manual_only(all_records)
    taxonomy = load_taxonomy(settings.taxonomy.file)
    papers, untriaged_count = reporting.collect_picker_papers(
        papers_records, args.start_date, args.end_date, taxonomy=taxonomy
    )
    costs = reporting.compute_cost_summary(papers_records, args.start_date, args.end_date)

    html = reporting.render_picker(
        report_title=f"{settings.weekly_report.subject_prefix}: Pick Papers",
        papers=papers,
        untriaged_count=untriaged_count,
        triage_cost_usd=costs.triage_cost_usd,
        window_start=args.start_date,
        window_end=args.end_date,
        unsearched_from=stopped_at,
        taxonomy=taxonomy,
        min_score=settings.taxonomy.min_score,
    )
    out_path = args.out or DEFAULT_OUTPUT_DIR / f"picks_{args.start_date.isoformat()}_{args.end_date.isoformat()}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    if untriaged_count:
        logger.warning(
            "%d paper(s) in range have no triage score (triage failed, or a dry run left them) "
            "and are left off the page.",
            untriaged_count,
        )
    _print_summary(display, settings, papers, out_path, time.monotonic() - began, searched=not args.no_fetch)
    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())
    if stopped_at:
        _log_how_to_finish(stopped_at, args)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
