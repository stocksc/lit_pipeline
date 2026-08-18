"""Weekly synthesis: step 8 of the literature tracker.

Entry point: `uv run lit-weekly` (see pyproject.toml [project.scripts]).

Deterministic templating over data the daily pipeline already stored -- no
extra LLM call, fully predictable output. Filters by `deep_read_at` rather
than the paper's arXiv publish date: a paper only becomes "reportable" once
its deep read actually finishes, and this filter naturally never re-includes
a paper once it ages out of the trailing window, with no separate
"already emailed" flag needed.

Also rolls up token usage and estimated cost for the week -- triage cost
covers *every* paper triaged in the window (most never clear the threshold),
while deep-read cost only covers the ones that did.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

from lit_pipeline import sheets_store
from lit_pipeline.config import Settings, load_settings
from lit_pipeline.email_resend import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class ReportPaper:
    arxiv_id: str
    title: str
    authors: str
    link: str
    triage_score: int
    summary: str
    key_contributions: list[str]
    methodology: str
    limitations: list[str]
    relevance_to_interests: str
    novel_or_incremental: str
    worth_followup: bool


@dataclass
class CostSummary:
    triage_count: int
    triage_input_tokens: int
    triage_output_tokens: int
    triage_cost_usd: float
    deep_read_count: int
    deep_read_input_tokens: int
    deep_read_output_tokens: int
    deep_read_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.triage_cost_usd + self.deep_read_cost_usd

    @property
    def total_input_tokens(self) -> int:
        return self.triage_input_tokens + self.deep_read_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.triage_output_tokens + self.deep_read_output_tokens


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def collect_weekly_papers(
    papers_records: list[dict], deep_read_records: list[dict], cutoff: datetime
) -> list[ReportPaper]:
    papers_by_id = {str(r["arxiv_id"]): r for r in papers_records if r.get("arxiv_id")}

    report_papers: list[ReportPaper] = []
    for record in deep_read_records:
        deep_read_at = _parse_iso(str(record.get("deep_read_at", "")))
        if deep_read_at is None or deep_read_at < cutoff:
            continue
        arxiv_id = str(record.get("arxiv_id", ""))
        paper = papers_by_id.get(arxiv_id)
        if paper is None:
            logger.warning("deep_reads row %s has no matching papers row; skipping", arxiv_id)
            continue

        contributions = [s.strip() for s in str(record.get("key_contributions", "")).split("|") if s.strip()]
        limitations = [s.strip() for s in str(record.get("limitations", "")).split("|") if s.strip()]

        report_papers.append(
            ReportPaper(
                arxiv_id=arxiv_id,
                title=str(paper.get("title", "")),
                authors=str(paper.get("authors", "")),
                link=str(paper.get("link", "")),
                triage_score=_safe_int(paper.get("triage_score")),
                summary=str(record.get("summary", "")),
                key_contributions=contributions,
                methodology=str(record.get("methodology", "")),
                limitations=limitations,
                relevance_to_interests=str(record.get("relevance_to_interests", "")),
                novel_or_incremental=str(record.get("novel_or_incremental", "")),
                worth_followup=str(record.get("worth_followup", "")).lower() == "true",
            )
        )

    report_papers.sort(key=lambda p: p.triage_score, reverse=True)
    return report_papers


def compute_cost_summary(
    papers_records: list[dict], deep_read_records: list[dict], cutoff: datetime
) -> CostSummary:
    """Triage cost covers every paper triaged this week, regardless of
    whether it cleared the threshold; deep-read cost covers only the subset
    that did. Both are read straight from the per-paper cost columns the
    daily pipeline writes (see sheets_store.py)."""
    triage_count = triage_input = triage_output = 0
    triage_cost = 0.0
    for record in papers_records:
        triaged_at = _parse_iso(str(record.get("triaged_at", "")))
        if triaged_at is None or triaged_at < cutoff:
            continue
        triage_count += 1
        triage_input += _safe_int(record.get("triage_input_tokens"))
        triage_output += _safe_int(record.get("triage_output_tokens"))
        triage_cost += _safe_float(record.get("triage_cost_usd"))

    deep_read_count = deep_input = deep_output = 0
    deep_cost = 0.0
    for record in deep_read_records:
        deep_read_at = _parse_iso(str(record.get("deep_read_at", "")))
        if deep_read_at is None or deep_read_at < cutoff:
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
        deep_read_count=deep_read_count,
        deep_read_input_tokens=deep_input,
        deep_read_output_tokens=deep_output,
        deep_read_cost_usd=deep_cost,
    )


def render_report(
    papers: list[ReportPaper], costs: CostSummary, settings: Settings
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
    today = datetime.now(timezone.utc).date()
    window_start = today - timedelta(days=settings.weekly_report.lookback_days)
    html = template.render(
        papers=papers, costs=costs, window_start=window_start, window_end=today, count=len(papers)
    )

    lines = [f"Weekly Lit Digest: {window_start} to {today} ({len(papers)} paper(s))", ""]
    for p in papers:
        lines.append(f"[{p.triage_score}/10] {p.title}")
        lines.append(f"  {p.authors}")
        lines.append(f"  {p.link}")
        lines.append(f"  Summary: {p.summary}")
        if p.key_contributions:
            lines.append("  Contributions: " + "; ".join(p.key_contributions))
        if p.limitations:
            lines.append("  Limitations: " + "; ".join(p.limitations))
        lines.append(f"  Relevance: {p.relevance_to_interests}")
        lines.append("")

    lines.append("--- Cost this week (estimated) ---")
    lines.append(
        f"Triage:    {costs.triage_count} paper(s), "
        f"{costs.triage_input_tokens + costs.triage_output_tokens:,} tokens, "
        f"${costs.triage_cost_usd:.4f}"
    )
    lines.append(
        f"Deep read: {costs.deep_read_count} paper(s), "
        f"{costs.deep_read_input_tokens + costs.deep_read_output_tokens:,} tokens, "
        f"${costs.deep_read_cost_usd:.4f}"
    )
    lines.append(f"Total:     ${costs.total_cost_usd:.4f}")
    text = "\n".join(lines)
    return html, text


def main() -> int:
    load_dotenv()
    settings = load_settings()
    papers_ws, deep_reads_ws = sheets_store.open_sheets(settings.google_sheets)
    papers_records = papers_ws.get_all_records()
    deep_read_records = deep_reads_ws.get_all_records()

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.weekly_report.lookback_days)
    papers = collect_weekly_papers(papers_records, deep_read_records, cutoff)
    costs = compute_cost_summary(papers_records, deep_read_records, cutoff)
    logger.info(
        "Weekly report covers %d paper(s); estimated cost $%.4f (triage $%.4f, deep-read $%.4f)",
        len(papers),
        costs.total_cost_usd,
        costs.triage_cost_usd,
        costs.deep_read_cost_usd,
    )

    html, text = render_report(papers, costs, settings)
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
