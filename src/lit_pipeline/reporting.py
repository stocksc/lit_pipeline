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

Both callers scope by *when the paper was originally published on arXiv*
(`published_date`), not by when the pipeline happened to process it --
that keeps a manual backfill's papers confined to the historical window it
was run for, instead of leaking into whatever regular weekly digest
happens to run afterward just because that's when they were evaluated.

Everything here operates on a single `papers_records` list (one dict per
sheet row) -- triage, mid-summary, and deep-read results all live as
columns on the same row (see sheets_store.py), so there's no cross-tab
join anywhere in this module.

All date comparisons are date-only (no time-of-day), which is a deliberate
simplification -- irrelevant at once-daily cadence, and both callers' date
ranges are calendar dates anyway.

`compute_score_histogram` is kept for backfill.py's --dry-run console
output (a 0-10 score distribution is a fast way to eyeball a large
historical sweep before committing to deep-read cost).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from lit_pipeline.arxiv_client import matches_literally

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

TITLE_SHORT_LENGTH = 100


@dataclass
class ReportPaper:
    arxiv_id: str
    title: str
    authors_display: str
    link: str
    published_date: str
    triage_score: int
    original_triage_score: int
    summary: str
    relevance: list[str]
    limitations: list[str]
    # Opus writes one on every deep-read. The email report only surfaces it in
    # the mid tier (where a downgrade needs explaining), but the PDF cover
    # page in pdf_bundle.py shows it whenever the score moved at all.
    score_rationale: str


@dataclass
class MidTierPaper:
    arxiv_id: str
    title: str
    link: str
    published_date: str
    triage_score: int
    original_triage_score: int
    summary: str
    # Only ever populated for the downgraded-deep-read source (Opus always
    # writes one, but a cheap-path mid_summary row never has one to read).
    score_rationale: str


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


def _original_score_or(record: dict, current_score: int) -> int:
    """original_triage_score is blank for any row triaged before that column
    existed. Falling back to 0 there (like _safe_int does) would read as a
    fake "downgrade from 0" against a real current score -- fall back to
    current_score instead, so a genuinely-missing original reads as
    unchanged rather than as a 0/10 rating that was never actually given."""
    raw = str(record.get("original_triage_score", "")).strip()
    return int(raw) if raw.isdigit() else current_score


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


def exclude_manual_only(papers_records: list[dict]) -> list[dict]:
    """Drop rows a manual deep dive added that the standing keyword lists
    would never have surfaced (the `manual_only` column -- see
    sheets_store.py and manual_deep_dive.py).

    Those papers are in the sheet only because someone asked for them by id,
    so the scheduled weekly digest and backfill reports shouldn't carry them:
    they were never part of what the pipeline was watching for, and their
    ad-hoc Opus cost isn't part of what the pipeline spent on its own.

    Deliberately *not* applied to a paper that matches the keyword lists,
    even when a manual dive is what pulled it in early: the daily job would
    have ingested and reported that one anyway, so hiding it would punch a
    hole in the digest rather than keep the digest clean.

    manual_deep_dive.py's own report never applies this -- those papers are
    the entire point of that run.
    """
    return [r for r in papers_records if not str(r.get("manual_only", "")).strip()]


def collect_report_papers(
    papers_records: list[dict],
    start: date,
    end: date,
    score_threshold: int,
) -> list[ReportPaper]:
    report_papers: list[ReportPaper] = []
    for record in papers_records:
        summary = str(record.get("deep_read_summary", "")).strip()
        if not summary:
            continue

        # `triage_score` is Opus's re-rating as of the deep-read (see
        # pipeline_stages.run_deep_read_stage), not the original Haiku
        # triage score -- a paper re-rated below threshold no longer
        # qualifies for the high tier, even though it has a deep-read
        # result. It falls to collect_mid_tier_papers/collect_low_tier_table
        # instead.
        if _safe_int(record.get("triage_score")) < score_threshold:
            continue

        paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue

        relevance = [s.strip() for s in str(record.get("deep_read_relevance", "")).split("|") if s.strip()]
        limitations = [s.strip() for s in str(record.get("deep_read_limitations", "")).split("|") if s.strip()]
        affiliations = [
            s.strip() for s in str(record.get("deep_read_author_affiliations", "")).split("|") if s.strip()
        ]
        authors = str(record.get("authors", ""))
        authors_display = f"{authors} ({', '.join(affiliations)})" if affiliations else authors

        report_papers.append(
            ReportPaper(
                arxiv_id=str(record.get("arxiv_id", "")),
                title=str(record.get("title", "")),
                authors_display=authors_display,
                link=str(record.get("link", "")),
                published_date=str(record.get("published_date", "")),
                triage_score=_safe_int(record.get("triage_score")),
                original_triage_score=_original_score_or(record, _safe_int(record.get("triage_score"))),
                summary=summary,
                relevance=relevance,
                limitations=limitations,
                score_rationale=str(record.get("deep_read_score_rationale", "")).strip(),
            )
        )

    report_papers.sort(key=lambda p: p.triage_score, reverse=True)
    return report_papers


def collect_mid_tier_papers(
    papers_records: list[dict],
    start: date,
    end: date,
    mid_summary_threshold: int,
    score_threshold: int,
) -> list[MidTierPaper]:
    """Two sources feed this tier, both just different columns on the same
    `papers` row:

    1. The cheap path: a non-empty `mid_summary`, generated by Haiku from
       the abstract alone -- no authors/affiliations, since this path
       skips the PDF fetch entirely.
    2. The downgraded-deep-read path: a paper that got a full Opus
       deep-read (`deep_read_summary` non-empty), but whose re-rated
       `triage_score` (see pipeline_stages.run_deep_read_stage) landed in
       the mid band instead of the high band. Reuses `deep_read_summary`
       as-is rather than requesting a second, shorter summary.

    Band-exclusivity in routing means a given paper should only ever match
    one of the two, but the deep-read summary is preferred if somehow both
    are present.
    """
    results: list[MidTierPaper] = []

    for record in papers_records:
        score = _safe_int(record.get("triage_score"))
        if not (mid_summary_threshold <= score < score_threshold):
            continue

        deep_read_summary = str(record.get("deep_read_summary", "")).strip()
        mid_summary = str(record.get("mid_summary", "")).strip()
        summary = deep_read_summary or mid_summary
        if not summary:
            continue

        paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue

        results.append(
            MidTierPaper(
                arxiv_id=str(record.get("arxiv_id", "")),
                title=str(record.get("title", "")),
                link=str(record.get("link", "")),
                published_date=str(record.get("published_date", "")),
                triage_score=score,
                original_triage_score=_original_score_or(record, score),
                summary=summary,
                score_rationale=str(record.get("deep_read_score_rationale", "")).strip(),
            )
        )

    results.sort(key=lambda p: p.triage_score, reverse=True)
    return results


def compute_cost_summary(
    papers_records: list[dict],
    start: date,
    end: date,
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
    deep_read_count = deep_input = deep_output = 0
    deep_cost = 0.0
    for record in papers_records:
        paper_date = parse_date(str(record.get("published_date", "")))
        in_window = _in_range(paper_date, start, end)
        if not in_window:
            continue

        triage_count += 1
        triage_input += _safe_int(record.get("triage_input_tokens"))
        triage_output += _safe_int(record.get("triage_output_tokens"))
        triage_cost += _safe_float(record.get("triage_cost_usd"))

        if str(record.get("mid_summary", "")).strip():
            mid_summary_count += 1
            mid_summary_input += _safe_int(record.get("mid_summary_input_tokens"))
            mid_summary_output += _safe_int(record.get("mid_summary_output_tokens"))
            mid_summary_cost += _safe_float(record.get("mid_summary_cost_usd"))

        if str(record.get("deep_read_summary", "")).strip():
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


def compute_avg_deep_read_cost(papers_records: list[dict]) -> tuple[float, int] | None:
    """Average `deep_read_cost_usd` across every deep-read paper
    sheet-wide -- deliberately not scoped to any date range, since this is
    used to project the cost of papers that haven't been deep-read yet
    (there's no in-range data to average for those). Self-corrects over
    time as more papers get processed under current settings (e.g. the
    PDF-trimming cut) -- not a hardcoded constant. Returns
    (avg_cost, sample_size), or None if there's no historical data yet."""
    costs = [
        _safe_float(r.get("deep_read_cost_usd"))
        for r in papers_records
        if str(r.get("deep_read_summary", "")).strip()
    ]
    if not costs:
        return None
    return sum(costs) / len(costs), len(costs)


def compute_keyword_hit_counts(
    papers_records: list[dict],
    start: date,
    end: date,
    specific_keywords: list[str],
    broad_keywords: list[str],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Counts how many papers triaged in the window literally mention each
    configured keyword in their abstract -- same word-boundary match
    arxiv_client.py uses to filter arXiv's stemmed search results, so the
    counts here reflect what actually drove a match, not arXiv's looser
    stemmed search. A paper matching multiple terms counts toward each one.
    Reads straight off the abstract text already stored per row -- no new
    arXiv calls. Returns (specific_hits, broad_hits), each sorted
    alphabetically by term -- kept separate since they're different
    matching rules (specific: any one qualifies; broad: needs 2+ to
    co-occur), not just two halves of one list."""
    specific_counts = {t: 0 for t in specific_keywords}
    broad_counts = {t: 0 for t in broad_keywords}
    for record in papers_records:
        paper_date = parse_date(str(record.get("published_date", "")))
        if not _in_range(paper_date, start, end):
            continue
        abstract = str(record.get("abstract", ""))
        for term in specific_keywords:
            if matches_literally(term, abstract):
                specific_counts[term] += 1
        for term in broad_keywords:
            if matches_literally(term, abstract):
                broad_counts[term] += 1
    specific_hits = sorted(specific_counts.items(), key=lambda kv: kv[0].lower())
    broad_hits = sorted(broad_counts.items(), key=lambda kv: kv[0].lower())
    return specific_hits, broad_hits


def compute_score_histogram(papers_records: list[dict], start: date, end: date) -> dict[int, int]:
    """Counts triaged papers by score (0-10), zero-filled, over the window.
    Used by backfill.py's --dry-run console output."""
    histogram = {score: 0 for score in range(11)}
    for record in papers_records:
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


def jinja_env() -> Environment:
    """The Jinja environment both templates render through (pdf_bundle.py uses
    it for the deep-dive cover page).

    Escaping is unconditional rather than left to `select_autoescape`, whose
    filename-based detection wouldn't match a ".html.jinja" double extension.
    """
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def render_report(
    report_title: str,
    papers: list[ReportPaper],
    mid_tier_papers: list[MidTierPaper],
    costs: CostSummary,
    triage_rows: list[TriagedPaperRow],
    triage_total: int,
    specific_keyword_hits: list[tuple[str, int]],
    broad_keyword_hits: list[tuple[str, int]],
    window_start: date,
    window_end: date,
    attachment_note: str = "",
) -> tuple[str, str]:
    """Returns (html, text).

    `attachment_note`, when given, renders as a banner above the cards --
    manual_deep_dive.py uses it to say which papers are attached to which
    email once a run's PDFs are split across several (see email_resend.py).
    """
    template = jinja_env().get_template("report.html.jinja")
    html = template.render(
        report_title=report_title,
        papers=papers,
        mid_tier_papers=mid_tier_papers,
        costs=costs,
        triage_rows=triage_rows,
        triage_total=triage_total,
        specific_keyword_hits=specific_keyword_hits,
        broad_keyword_hits=broad_keyword_hits,
        window_start=window_start,
        window_end=window_end,
        attachment_note=attachment_note,
    )

    lines = [report_title, ""]
    if attachment_note:
        lines += [attachment_note, ""]
    for p in papers:
        score_label = (
            f"{p.triage_score}/10 (originally {p.original_triage_score}/10)"
            if p.original_triage_score != p.triage_score
            else f"{p.triage_score}/10"
        )
        lines.append(f"[{score_label}] {p.title}")
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
            score_label = (
                f"{p.triage_score}/10 (originally {p.original_triage_score}/10)"
                if p.original_triage_score != p.triage_score
                else f"{p.triage_score}/10"
            )
            lines.append(f"[{score_label}] {p.title}")
            lines.append(f"  {p.published_date}")
            lines.append(f"  {p.link}")
            lines.append(f"  {p.summary}")
            if p.original_triage_score != p.triage_score and p.score_rationale:
                lines.append(f"  Why the rating dropped: {p.score_rationale}")
            lines.append("")

    # Skipped entirely when empty, matching the template -- a manual deep
    # dive gives every paper a full card, leaving nothing for this tier.
    if triage_total:
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

    if specific_keyword_hits:
        lines.append("")
        lines.append("--- Exact Keywords ---")
        for term, hits in specific_keyword_hits:
            lines.append(f"  {term}: {hits}")

    if broad_keyword_hits:
        lines.append("")
        lines.append("--- Broad Keywords (2+ Matches Needed) ---")
        for term, hits in broad_keyword_hits:
            lines.append(f"  {term}: {hits}")

    text = "\n".join(lines)
    return html, text
