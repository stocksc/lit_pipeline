"""Shared report-building logic for both the weekly cron digest and manual
backfill runs (see backfill.py).

Reports have three tiers, driven by each paper's triage score:

- **High** (score >= score_threshold): full Opus deep-read card --
  `collect_report_papers` / `ReportPaper`.
- **Mid** (mid_summary_threshold <= score < score_threshold): a cheap
  ~50-word Haiku summary from the abstract alone, no authors/affiliations --
  `collect_mid_tier_papers` / `MidTierPaper`.
- **Low** (everything else triaged in the window): just title/date/score --
  `collect_low_tier_table` / `TriagedPaperRow`. This is defined as "every
  triaged paper not already shown in the high or mid tier" rather than by
  score directly, which also gracefully catches any high/mid-tier paper
  that errored out of its richer treatment (retries exhausted) -- it still
  shows up here with at least a title instead of disappearing.

Both callers need all of this, but scoped differently:

- The weekly job scopes by *when the pipeline processed a paper* (`date_field
  = "processed"`): triage/mid-summary cost keyed off `triaged_at`/
  `mid_summary_at`, the deep-read paper list/cost keyed off `deep_read_at`.
  This is a trailing window from "today."
- A backfill scopes by *when the paper was originally published on arXiv*
  (`date_field = "published"`): everything keyed off `published_date`
  instead, over an explicit start/end range. `deep_reads` rows don't carry
  `published_date` themselves, so that mode always joins back to the papers
  row for it.

All date comparisons are date-only (no time-of-day), which is a deliberate
simplification -- irrelevant at once-daily cadence, and backfill's CLI dates
are calendar dates anyway.

`compute_score_histogram` is kept for backfill.py's --dry-run console
output (a 0-10 score distribution is a fast way to eyeball a large
historical sweep before committing to deep-read cost).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DateField = Literal["processed", "published"]

TITLE_SHORT_LENGTH = 100


@dataclass
class ReportPaper:
    arxiv_id: str
    title: str
    authors_display: str
    link: str
    published_date: str
    triage_score: int
    summary: str
    relevance: list[str]
    limitations: list[str]


@dataclass
class MidTierPaper:
    arxiv_id: str
    title: str
    link: str
    published_date: str
    triage_score: int
    summary: str


@dataclass
class TriagedPaperRow:
    published_date: str
    title_short: str
    score: int
    link: str


@dataclass
class CostSummary:
    triage_count: int
    triage_input_tokens: int
    triage_output_tokens: int
    triage_cost_usd: float
    mid_summary_count: int
    mid_summary_input_tokens: int
    mid_summary_output_tokens: int
    mid_summary_cost_usd: float
    deep_read_count: int
    deep_read_input_tokens: int
    deep_read_output_tokens: int
    deep_read_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.triage_cost_usd + self.mid_summary_cost_usd + self.deep_read_cost_usd

    @property
    def total_input_tokens(self) -> int:
        return self.triage_input_tokens + self.mid_summary_input_tokens + self.deep_read_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.triage_output_tokens + self.mid_summary_output_tokens + self.deep_read_output_tokens


def parse_date(value: str) -> date | None:
    """Parses either a bare 'YYYY-MM-DD' (published_date) or a full ISO
    timestamp ('...triaged_at'/'deep_read_at'/'mid_summary_at') and returns
    just the date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def format_date_long(d: date) -> str:
    """'2026-07-01' -> 'July 1, 2026'. Built manually (not strftime's
    %-d/%#d) since those leading-zero-strip flags aren't portable between
    Windows and Unix."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _in_range(d: date | None, start: date, end: date) -> bool:
    return d is not None and start <= d <= end


def _short_title(title: str) -> str:
    if len(title) <= TITLE_SHORT_LENGTH:
        return title
    return title[:TITLE_SHORT_LENGTH] + "..."


def collect_report_papers(
    papers_records: list[dict],
    deep_read_records: list[dict],
    start: date,
    end: date,
    date_field: DateField,
) -> list[ReportPaper]:
    papers_by_id = {str(r["arxiv_id"]): r for r in papers_records if r.get("arxiv_id")}

    report_papers: list[ReportPaper] = []
    for record in deep_read_records:
        arxiv_id = str(record.get("arxiv_id", ""))
        paper = papers_by_id.get(arxiv_id)
        if paper is None:
            logger.warning("deep_reads row %s has no matching papers row; skipping", arxiv_id)
            continue

        if date_field == "processed":
            paper_date = parse_date(str(record.get("deep_read_at", "")))
        else:
            paper_date = parse_date(str(paper.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue

        relevance = [s.strip() for s in str(record.get("relevance", "")).split("|") if s.strip()]
        limitations = [s.strip() for s in str(record.get("limitations", "")).split("|") if s.strip()]
        affiliations = [s.strip() for s in str(record.get("author_affiliations", "")).split("|") if s.strip()]
        authors = str(paper.get("authors", ""))
        authors_display = f"{authors} ({', '.join(affiliations)})" if affiliations else authors

        report_papers.append(
            ReportPaper(
                arxiv_id=arxiv_id,
                title=str(paper.get("title", "")),
                authors_display=authors_display,
                link=str(paper.get("link", "")),
                published_date=str(paper.get("published_date", "")),
                triage_score=_safe_int(paper.get("triage_score")),
                summary=str(record.get("summary", "")),
                relevance=relevance,
                limitations=limitations,
            )
        )

    report_papers.sort(key=lambda p: p.triage_score, reverse=True)
    return report_papers


def collect_mid_tier_papers(
    papers_records: list[dict], start: date, end: date, date_field: DateField
) -> list[MidTierPaper]:
    """Papers with a completed mid-tier summary (a non-empty `mid_summary`)
    in the window. No authors/affiliations -- this tier skips the PDF fetch
    entirely, so there's nothing to extract them from."""
    results: list[MidTierPaper] = []
    for record in papers_records:
        summary = str(record.get("mid_summary", "")).strip()
        if not summary:
            continue
        if date_field == "processed":
            paper_date = parse_date(str(record.get("mid_summary_at", "")))
        else:
            paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue
        results.append(
            MidTierPaper(
                arxiv_id=str(record.get("arxiv_id", "")),
                title=str(record.get("title", "")),
                link=str(record.get("link", "")),
                published_date=str(record.get("published_date", "")),
                triage_score=_safe_int(record.get("triage_score")),
                summary=summary,
            )
        )
    results.sort(key=lambda p: p.triage_score, reverse=True)
    return results


def compute_cost_summary(
    papers_records: list[dict],
    deep_read_records: list[dict],
    start: date,
    end: date,
    date_field: DateField,
) -> CostSummary:
    """Triage cost covers every paper triaged in the window, regardless of
    tier; mid-summary cost covers the subset that got a mid-tier summary;
    deep-read cost covers the subset that got a full deep-read. All three
    are read straight from the per-paper cost columns the pipeline writes
    (see sheets_store.py)."""
    triage_count = triage_input = triage_output = 0
    triage_cost = 0.0
    mid_summary_count = mid_summary_input = mid_summary_output = 0
    mid_summary_cost = 0.0
    for record in papers_records:
        if date_field == "processed":
            triage_date = parse_date(str(record.get("triaged_at", "")))
        else:
            triage_date = parse_date(str(record.get("published_date", "")))
        if _in_range(triage_date, start, end):
            triage_count += 1
            triage_input += _safe_int(record.get("triage_input_tokens"))
            triage_output += _safe_int(record.get("triage_output_tokens"))
            triage_cost += _safe_float(record.get("triage_cost_usd"))

        if str(record.get("mid_summary", "")).strip():
            if date_field == "processed":
                mid_date = parse_date(str(record.get("mid_summary_at", "")))
            else:
                mid_date = parse_date(str(record.get("published_date", "")))
            if _in_range(mid_date, start, end):
                mid_summary_count += 1
                mid_summary_input += _safe_int(record.get("mid_summary_input_tokens"))
                mid_summary_output += _safe_int(record.get("mid_summary_output_tokens"))
                mid_summary_cost += _safe_float(record.get("mid_summary_cost_usd"))

    papers_by_id = {str(r["arxiv_id"]): r for r in papers_records if r.get("arxiv_id")}
    deep_read_count = deep_input = deep_output = 0
    deep_cost = 0.0
    for record in deep_read_records:
        arxiv_id = str(record.get("arxiv_id", ""))
        paper = papers_by_id.get(arxiv_id)
        if date_field == "processed":
            paper_date = parse_date(str(record.get("deep_read_at", "")))
        else:
            paper_date = parse_date(str(paper.get("published_date", ""))) if paper else None
        if not _in_range(paper_date, start, end):
            continue
        deep_read_count += 1
        deep_input += _safe_int(record.get("deep_read_input_tokens"))
        deep_output += _safe_int(record.get("deep_read_output_tokens"))
        deep_cost += _safe_float(record.get("deep_read_cost_usd"))

    return CostSummary(
        triage_count=triage_count,
        triage_input_tokens=triage_input,
        triage_output_tokens=triage_output,
        triage_cost_usd=triage_cost,
        mid_summary_count=mid_summary_count,
        mid_summary_input_tokens=mid_summary_input,
        mid_summary_output_tokens=mid_summary_output,
        mid_summary_cost_usd=mid_summary_cost,
        deep_read_count=deep_read_count,
        deep_read_input_tokens=deep_input,
        deep_read_output_tokens=deep_output,
        deep_read_cost_usd=deep_cost,
    )


def compute_score_histogram(
    papers_records: list[dict], start: date, end: date, date_field: DateField
) -> dict[int, int]:
    """Counts triaged papers by score (0-10), zero-filled, over the window.
    Used by backfill.py's --dry-run console output. `date_field="published"`
    and `"processed"` both key off fields already on the papers row
    (published_date / triaged_at) -- no join needed."""
    histogram = {score: 0 for score in range(11)}
    for record in papers_records:
        if date_field == "processed":
            paper_date = parse_date(str(record.get("triaged_at", "")))
        else:
            paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue
        score_raw = str(record.get("triage_score", "")).strip()
        if not score_raw.isdigit():
            continue
        score = int(score_raw)
        if 0 <= score <= 10:
            histogram[score] += 1
    return histogram


def collect_low_tier_table(
    papers_records: list[dict],
    exclude_ids: set[str],
    start: date,
    end: date,
    date_field: DateField,
) -> tuple[list[TriagedPaperRow], int]:
    """Every triaged paper in the window NOT already shown in the high or
    mid tier (pass the arxiv_ids from `collect_report_papers` and
    `collect_mid_tier_papers` as `exclude_ids`) as (published_date, short
    title, score) rows, sorted by score descending (ties by published_date
    descending). Returns (rows, total count) -- uncapped, so the two are
    always equal; total count is kept in the signature for compatibility
    with callers/the template."""
    rows: list[TriagedPaperRow] = []
    for record in papers_records:
        arxiv_id = str(record.get("arxiv_id", ""))
        if arxiv_id in exclude_ids:
            continue
        if date_field == "processed":
            paper_date = parse_date(str(record.get("triaged_at", "")))
        else:
            paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue
        score_raw = str(record.get("triage_score", "")).strip()
        if not score_raw.isdigit():
            continue
        rows.append(
            TriagedPaperRow(
                published_date=str(record.get("published_date", "")),
                title_short=_short_title(str(record.get("title", ""))),
                score=int(score_raw),
                link=str(record.get("link", "")),
            )
        )

    rows.sort(key=lambda r: (r.score, r.published_date), reverse=True)
    return rows, len(rows)


def render_report(
    report_title: str,
    papers: list[ReportPaper],
    mid_tier_papers: list[MidTierPaper],
    costs: CostSummary,
    triage_rows: list[TriagedPaperRow],
    triage_total: int,
    window_start: date,
    window_end: date,
) -> tuple[str, str]:
    """Returns (html, text)."""
    # This environment only ever renders one, always-HTML template, so escape
    # unconditionally rather than relying on select_autoescape's filename-based
    # detection (which wouldn't match the ".html.jinja" double extension).
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html.jinja")
    html = template.render(
        report_title=report_title,
        papers=papers,
        mid_tier_papers=mid_tier_papers,
        costs=costs,
        triage_rows=triage_rows,
        triage_total=triage_total,
        window_start=window_start,
        window_end=window_end,
        count=len(papers),
    )

    lines = [report_title, f"{len(papers)} relevant paper{'s' if len(papers) != 1 else ''} found", ""]
    for p in papers:
        lines.append(f"[{p.triage_score}/10] {p.title}")
        lines.append(f"  {p.published_date}")
        lines.append(f"  {p.authors_display}")
        lines.append(f"  {p.link}")
        lines.append(f"  Summary: {p.summary}")
        if p.relevance:
            lines.append("  Relevance: " + "; ".join(p.relevance))
        if p.limitations:
            lines.append("  Limitations: " + "; ".join(p.limitations))
        lines.append("")

    if mid_tier_papers:
        lines.append(f"--- Potentially Relevant ({len(mid_tier_papers)}) ---")
        for p in mid_tier_papers:
            lines.append(f"[{p.triage_score}/10] {p.title}")
            lines.append(f"  {p.published_date}")
            lines.append(f"  {p.link}")
            lines.append(f"  {p.summary}")
            lines.append("")

    lines.append(f"--- Other Papers Reviewed ({triage_total}) ---")
    for row in triage_rows:
        lines.append(f"  [{row.score:>2}] {row.title_short}  {row.published_date}")
        lines.append(f"    {row.link}")
    if triage_total > len(triage_rows):
        lines.append(f"  ... + {triage_total - len(triage_rows)} more not shown")
    lines.append("")

    lines.append("--- Cost this window (estimated) ---")
    lines.append(
        f"Triage:      {costs.triage_count} paper(s), "
        f"{costs.triage_input_tokens + costs.triage_output_tokens:,} tokens, "
        f"${costs.triage_cost_usd:.4f}"
    )
    lines.append(
        f"Mid-summary: {costs.mid_summary_count} paper(s), "
        f"{costs.mid_summary_input_tokens + costs.mid_summary_output_tokens:,} tokens, "
        f"${costs.mid_summary_cost_usd:.4f}"
    )
    lines.append(
        f"Deep read:   {costs.deep_read_count} paper(s), "
        f"{costs.deep_read_input_tokens + costs.deep_read_output_tokens:,} tokens, "
        f"${costs.deep_read_cost_usd:.4f}"
    )
    lines.append(f"Total:       ${costs.total_cost_usd:.4f}")
    text = "\n".join(lines)
    return html, text
