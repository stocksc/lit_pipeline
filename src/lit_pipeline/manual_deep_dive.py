"""Force specific arXiv papers through triage + deep-read, whatever they score.

Entry point: `uv run lit-deep-dive 2501.12345 2502.09876`
(see pyproject.toml [project.scripts]).

The daily job and backfills both decide for themselves which papers are worth
Opus's time: a paper is only deep-read if arXiv's keyword search surfaced it
*and* triage scored it at/above `triage.score_threshold`. This is the manual
override for when you already know a paper matters -- a colleague sent it to
you, it's cited by something you just read, or the pipeline scored it 3 and
you disagree.

Given a list of ids, it ingests whatever the sheet hasn't seen yet (fetched
straight by id, with no keyword filter -- you asked for these by name),
triages each paper that doesn't already have a deep-read on file, then
deep-reads it regardless of what it scored, and emails a report covering
every requested paper -- freshly read or reused.

Because there's no threshold in play, the report gives every paper a full
deep-dive card rather than tiering it by score, and skips the keyword-hit
tables (nothing here was found by keyword).

The mid-summary stage is deliberately not run: these papers are all going to
Opus anyway, so a cheap abstract-only summary would be spend with nothing to
show for it.

A paper pulled in here does NOT thereby join the weekly digest. Anything
ingested by this command that your standing keyword lists would never have
matched is flagged `manual_only` in the sheet, and the scheduled digest and
backfill reports skip flagged rows (see reporting.exclude_manual_only). A
paper that does match those keywords is left unflagged and reports normally,
because the daily job would have found and reported it anyway -- hiding it
would leave a hole in the digest rather than keep it clean. The flag is set
once, at ingest; a paper already in the sheet before you dive it is never
flagged, since by definition it was already being tracked.

--attach-pdfs turns the report email into something you can actually read
away from the sheet: one PDF per paper, the deep-read write-up as page 1 and
the paper itself appended after it (see pdf_bundle.py). The email body stays
the usual report. Resend caps an email at 40MB after base64, so a run whose
PDFs exceed that is split across numbered emails rather than truncated.

--dry-run mirrors backfill.py's: ingest + triage only, then print what the
deep-read phase would cost (projected from the sheet's own historical
average), stopping before any Opus spend. Same as everywhere else in this
pipeline, a run is safe to repeat: a paper that already has a deep-read in
the sheet is reused exactly as it stands, so asking for it again costs
nothing and reports the same. Pass --refresh when you genuinely want one
re-read from scratch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv

from gspread import Worksheet

from lit_pipeline import pdf_bundle, reporting, sheets_store
from lit_pipeline.arxiv_client import PaperCandidate, fetch_by_ids, normalize_arxiv_id, passes_keyword_filter
from lit_pipeline.config import Settings, load_settings
from lit_pipeline.email_resend import (
    ATTACHMENT_BUDGET_BYTES,
    Attachment,
    base64_size,
    pack_attachments,
    send_email,
)
from lit_pipeline.pipeline_stages import run_deep_read_stage, run_triage_stage
from lit_pipeline.sheets_store import PaperRow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Rough per-paper Opus figure, only used to say something useful on a --dry-run
# against a sheet with no deep-read history to average yet. Same fallback
# backfill.py quotes.
FALLBACK_DEEP_READ_COST_HINT = "roughly $0.25-0.40/paper"

TITLE_LOG_LENGTH = 80


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Force specific arXiv papers through triage + deep-read, ignoring the "
            "score threshold that would normally gate the deep-read."
        )
    )
    parser.add_argument(
        "arxiv_ids",
        nargs="+",
        metavar="ARXIV_ID",
        help=(
            "One or more arXiv ids to deep-dive. Accepts '2501.12345', "
            "'2501.12345v2', 'arXiv:2501.12345', or a full arxiv.org/abs/... URL"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Ingest + triage only; print each paper's score and a rough $ cost "
            "projection for the deep-read, and stop before deep-read/email"
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Re-read papers that already have a deep-read instead of reusing the "
            "stored one -- a fresh Opus read, and a fresh bill, per paper"
        ),
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Deep-read and write to the sheet as usual, but skip the report email",
    )
    parser.add_argument(
        "--attach-pdfs",
        action="store_true",
        help=(
            "Attach one PDF per paper to the email: the deep-read write-up as page 1, "
            "the paper's own PDF after it"
        ),
    )
    args = parser.parse_args(argv)
    if args.attach_pdfs and args.no_email:
        parser.error("--attach-pdfs has nothing to attach to with --no-email")
    return args


def _normalize_requested_ids(raw_ids: list[str]) -> list[str]:
    """Normalize and de-duplicate the ids given on the command line, keeping
    the order they were given in. Every bad id is collected and reported at
    once -- a typo in the fourth of five ids shouldn't be discovered only
    after the first three have been paid for."""
    requested: list[str] = []
    invalid: list[str] = []
    for raw in raw_ids:
        try:
            arxiv_id = normalize_arxiv_id(raw)
        except ValueError:
            invalid.append(raw)
            continue
        if arxiv_id not in requested:
            requested.append(arxiv_id)
    if invalid:
        raise SystemExit("Not a recognizable arXiv id: " + ", ".join(repr(i) for i in invalid))
    return requested


def _ingest_new_papers(papers_ws: Worksheet, settings: Settings, candidates: list[PaperCandidate]) -> None:
    """Add papers the sheet has never seen, flagging the ones your standing
    keyword lists would never have surfaced so they stay out of the scheduled
    reports (see the module docstring and reporting.exclude_manual_only).

    The keyword check is the same one the daily job filters arXiv's search
    results with, so "would this have shown up on its own?" is answered by
    exactly the rule that would have answered it.
    """
    on_query: list[PaperCandidate] = []
    off_query: list[PaperCandidate] = []
    for candidate in candidates:
        matched = passes_keyword_filter(
            candidate.abstract, settings.arxiv.specific_keywords, settings.arxiv.broad_keywords
        )
        (on_query if matched else off_query).append(candidate)

    added_by = sheets_store.ADDED_BY_DEEP_DIVE
    sheets_store.append_new_candidates(papers_ws, on_query, added_by=added_by)
    sheets_store.append_new_candidates(papers_ws, off_query, added_by=added_by, manual_only=True)
    if on_query:
        logger.info(
            "%d new paper(s) match your keyword lists, so they report as usual: %s",
            len(on_query),
            ", ".join(c.arxiv_id for c in on_query),
        )
    if off_query:
        logger.info(
            "%d new paper(s) don't match your keyword lists, so they're flagged manual_only "
            "and stay out of the weekly digest: %s",
            len(off_query),
            ", ".join(c.arxiv_id for c in off_query),
        )


def _split_reusable(
    scoped_index: dict[str, PaperRow], refresh: bool
) -> tuple[dict[str, PaperRow], dict[str, PaperRow]]:
    """Split the requested papers into (reuse as-is, still to process).

    A paper that already has a `deep_read_summary` is left completely
    untouched -- not re-read, and not even re-triaged. Skipping the Opus read
    is the obvious saving, but skipping the triage matters just as much for
    correctness: `triage_score` holds Opus's post-deep-read re-rating (see
    sheets_store.PAPERS_HEADERS), so re-triaging without re-reading would
    overwrite that with a fresh abstract-only triage score and leave the
    stored deep-read summary sitting next to a rating that didn't come from
    reading the paper.

    A paper whose deep-read failed has no summary to reuse, so it lands in
    the process side and gets another attempt.
    """
    if refresh:
        return {}, dict(scoped_index)
    reusable: dict[str, PaperRow] = {}
    to_process: dict[str, PaperRow] = {}
    for arxiv_id, row in scoped_index.items():
        target = reusable if str(row.raw.get("deep_read_summary", "")).strip() else to_process
        target[arxiv_id] = row
    return reusable, to_process


def _published_window(papers_records: list[dict]) -> tuple[date, date]:
    """Everything in reporting.py scopes by publish date, so a hand-picked set
    of papers needs a window that simply covers whatever was asked for: their
    earliest publish date to their latest. Unlike the weekly/backfill reports,
    the window here is derived from the papers rather than chosen up front."""
    dates = [
        d
        for d in (reporting.parse_date(str(r.get("published_date", ""))) for r in papers_records)
        if d is not None
    ]
    if not dates:
        today = datetime.now(timezone.utc).date()
        return today, today
    return min(dates), max(dates)


def _log_triage_scores(rows: list[PaperRow]) -> None:
    for row in sorted(rows, key=lambda r: r.triage_score or 0, reverse=True):
        score = row.triage_score if row.triage_score is not None else "--"
        title = str(row.raw.get("title", ""))[:TITLE_LOG_LENGTH]
        logger.info("  [%s/10] %s %s", score, row.arxiv_id, title)


def _log_projected_deep_read_cost(papers_ws: Worksheet, paper_count: int) -> None:
    """Every requested paper gets deep-read here, threshold or not, so unlike
    backfill.py's projection there's no at-or-above-threshold subset to
    count -- it's the full list, including any paper being re-read."""
    if paper_count == 0:
        logger.info("Nothing new to deep-read -- every requested paper already has one.")
        return
    avg_deep_read = reporting.compute_avg_deep_read_cost(sheets_store.get_all_records(papers_ws))
    if avg_deep_read is None:
        logger.info(
            "%d paper(s) would be deep-read, but there's no historical deep-read cost "
            "data yet to project from. Opus deep-read typically runs %s as a rough "
            "starting estimate.",
            paper_count,
            FALLBACK_DEEP_READ_COST_HINT,
        )
        return
    avg_cost, n_history = avg_deep_read
    logger.info(
        "Projected deep-read cost: %d paper(s) x $%.4f "
        "(avg over %d historical deep-read(s) sheet-wide) = $%.2f",
        paper_count,
        avg_cost,
        n_history,
        paper_count * avg_cost,
    )


def _build_attachments(
    papers: list[reporting.ReportPaper], report_title: str, settings: Settings
) -> tuple[list[Attachment], list[str]]:
    """One PDF per paper: write-up first, paper appended (see pdf_bundle.py).

    Returns (attachments, degraded_ids), where a degraded paper is attached as
    its write-up alone -- either its PDF couldn't be downloaded, or the paper
    is so large that no email could carry it. Both are worth telling the
    reader about, since the attachment they get is thinner than the others.
    """
    attachments: list[Attachment] = []
    degraded: list[str] = []
    for paper in papers:
        cover = pdf_bundle.build_cover_pdf(paper, report_title, settings.deep_read.model)
        paper_pdf = pdf_bundle.fetch_paper_pdf(paper)
        bundle = pdf_bundle.build_bundle_pdf(cover, paper_pdf)
        if base64_size(len(bundle)) > ATTACHMENT_BUDGET_BYTES:
            # Splitting across emails can't rescue a single paper this big --
            # no email can hold it. The write-up is the part that isn't one
            # click away on arXiv, so send that.
            logger.warning(
                "%s is %.1f MB, too large for any single email -- attaching the write-up alone",
                paper.arxiv_id,
                len(bundle) / 1024 / 1024,
            )
            bundle = cover
            degraded.append(paper.arxiv_id)
        elif paper_pdf is None:
            degraded.append(paper.arxiv_id)
        filename = pdf_bundle.attachment_filename(paper)
        attachments.append(Attachment(filename=filename, content=bundle))
        logger.info("  %s -> %s (%.1f MB)", paper.arxiv_id, filename, len(bundle) / 1024 / 1024)
    return attachments, degraded


def _attachment_note(
    pack: list[Attachment], part: int, total_parts: int, degraded: list[str]
) -> str:
    """The banner above the cards, naming what's attached to this particular
    email. Only says anything when there's something to say."""
    if not pack:
        return ""
    lines = [f"{len(pack)} paper(s) attached -- the deep read is page 1 of each PDF."]
    if total_parts > 1:
        lines.append(
            f"Email {part} of {total_parts}: the attachments were too large to send at once, "
            "so they're split across several emails. This one carries "
            + ", ".join(a.filename.removesuffix(".pdf") for a in pack)
            + "."
        )
    if degraded:
        lines.append(
            "Attached as the write-up only (the paper's PDF couldn't be fetched, or was too "
            "large to email): " + ", ".join(degraded) + ". The links above still work."
        )
    return " ".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    requested_ids = _normalize_requested_ids(args.arxiv_ids)

    settings = load_settings()
    client = Anthropic()
    papers_ws = sheets_store.open_sheets(settings.google_sheets)

    index = sheets_store.load_papers_index(papers_ws)
    unknown_to_sheet = [i for i in requested_ids if i not in index]
    if unknown_to_sheet:
        logger.info("Fetching %d requested paper(s) not yet in the sheet...", len(unknown_to_sheet))
        _ingest_new_papers(papers_ws, settings, fetch_by_ids(unknown_to_sheet))
        # Re-read so the newly appended rows have row numbers to write back to.
        index = sheets_store.load_papers_index(papers_ws)

    scoped_index = {i: index[i] for i in requested_ids if i in index}
    not_found = [i for i in requested_ids if i not in index]
    if not_found:
        logger.warning(
            "Skipping %d id(s) arXiv didn't return (unknown or withdrawn?): %s",
            len(not_found),
            ", ".join(not_found),
        )
    if not scoped_index:
        raise SystemExit("None of the requested ids could be resolved on arXiv -- nothing to do.")

    reusable, to_process = _split_reusable(scoped_index, refresh=args.refresh)
    if reusable:
        logger.info(
            "Reusing the deep-read already in the sheet for %d paper(s) -- pass --refresh "
            "to read them again: %s",
            len(reusable),
            ", ".join(reusable),
        )

    run_triage_stage(client, settings, papers_ws, to_process, force=True)
    if to_process:
        logger.info("Triage scores (informational only -- every paper below is deep-read regardless):")
        _log_triage_scores(list(to_process.values()))

    if args.dry_run:
        _log_projected_deep_read_cost(papers_ws, len(to_process))
        logger.info("Dry run complete -- no deep-read or email performed.")
        return 0

    run_deep_read_stage(client, settings, papers_ws, to_process, force=True)

    papers_records = [
        record
        for record in sheets_store.get_all_records(papers_ws)
        if str(record.get("arxiv_id", "")).strip() in scoped_index
    ]
    window_start, window_end = _published_window(papers_records)
    # score_threshold=0: every paper that came back with a deep-read gets a
    # full card no matter how Opus re-rated it. The point of the run was to
    # see the deep-read, so re-applying the threshold that would have excluded
    # the paper in the first place would throw away exactly what was asked
    # for. That also leaves the mid tier empty by construction.
    report_papers = reporting.collect_report_papers(
        papers_records, window_start, window_end, score_threshold=0
    )
    costs = reporting.compute_cost_summary(papers_records, window_start, window_end)
    # Anything requested that has no deep-read to show (its deep-read errored
    # out) still surfaces here by title and score, rather than vanishing from
    # the report of a run that was explicitly about it.
    shown_ids = {p.arxiv_id for p in report_papers}
    triage_rows, triage_total = reporting.collect_low_tier_table(
        papers_records, shown_ids, window_start, window_end
    )
    # `costs` covers every requested paper, reused ones included, so it's what
    # these papers have cost in total rather than what this run spent. The
    # run's own spend is the same sum over just the papers it processed.
    fresh_records = [r for r in papers_records if str(r.get("arxiv_id", "")).strip() in to_process]
    run_costs = reporting.compute_cost_summary(fresh_records, window_start, window_end)
    logger.info(
        "Report covers %d of %d requested paper(s): %d reused an existing deep-read, "
        "%d went to Opus this run.",
        len(report_papers),
        len(requested_ids),
        len(reusable),
        len(to_process),
    )
    logger.info(
        "Cost on record for these papers: $%.4f (of which this run: $%.4f)",
        costs.total_cost_usd,
        run_costs.total_cost_usd,
    )

    if args.no_email:
        logger.info("Manual deep dive complete -- results are in the sheet, email skipped.")
        return 0

    # report_title (no count -- shown in the email body) and subject (keeps
    # the count -- shown in the mail client's subject line) deliberately
    # diverge, same split as the weekly/backfill reports.
    report_title = f"{settings.weekly_report.subject_prefix}: Manual Deep Dive"
    subject = f"{report_title} ({len(report_papers)} paper(s))"

    attachments: list[Attachment] = []
    degraded: list[str] = []
    if args.attach_pdfs and report_papers:
        logger.info("Building %d PDF bundle(s) (write-up + paper)...", len(report_papers))
        attachments, degraded = _build_attachments(report_papers, report_title, settings)

    # `or [[]]` keeps the no-attachment case sending exactly one email; a
    # returned `oversized` is impossible here, since _build_attachments already
    # shrank anything that couldn't fit to its cover page.
    packs, _oversized = pack_attachments(attachments)
    packs = packs or [[]]

    # Every part carries the whole report, not just its own slice, so any one
    # of them stands alone when forwarded or archived later. The banner is
    # what says which papers are attached to this particular email.
    for part, pack in enumerate(packs, start=1):
        html, text = reporting.render_report(
            report_title=report_title,
            papers=report_papers,
            mid_tier_papers=[],
            costs=costs,
            triage_rows=triage_rows,
            triage_total=triage_total,
            # Hand-picked papers weren't found by keyword, so a keyword-hit
            # breakdown of them would be measuring nothing.
            specific_keyword_hits=[],
            broad_keyword_hits=[],
            window_start=window_start,
            window_end=window_end,
            attachment_note=_attachment_note(pack, part, len(packs), degraded),
        )
        send_email(
            sender=settings.weekly_report.sender_email,
            recipient=settings.weekly_report.recipient_email,
            subject=subject if len(packs) == 1 else f"{subject} [{part}/{len(packs)}]",
            html=html,
            text=text,
            attachments=pack or None,
        )

    logger.info(
        "Manual deep dive complete: %d paper(s) reported%s, sent in %d email(s).",
        len(report_papers),
        f" with {len(attachments)} PDF attachment(s)" if attachments else "",
        len(packs),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
