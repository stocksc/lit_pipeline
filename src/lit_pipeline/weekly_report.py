"""Weekly synthesis: step 8 of the literature tracker.

Entry point: `uv run lit-weekly` (see pyproject.toml [project.scripts]).

Deterministic templating over data the daily pipeline already stored -- no
extra LLM call, fully predictable output. Filters by `deep_read_at` rather
than the paper's arXiv publish date: a paper only becomes "reportable" once
its deep read actually finishes, and this filter naturally never re-includes
a paper once it ages out of the trailing window, with no separate
"already emailed" flag needed.
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


def collect_weekly_papers(settings: Settings) -> list[ReportPaper]:
    papers_ws, deep_reads_ws = sheets_store.open_sheets(settings.google_sheets)
    papers_by_id = {str(r["arxiv_id"]): r for r in papers_ws.get_all_records() if r.get("arxiv_id")}
    deep_read_records = deep_reads_ws.get_all_records()

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.weekly_report.lookback_days)

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
        score_raw = str(paper.get("triage_score", "0")).strip()

        report_papers.append(
            ReportPaper(
                arxiv_id=arxiv_id,
                title=str(paper.get("title", "")),
                authors=str(paper.get("authors", "")),
                link=str(paper.get("link", "")),
                triage_score=int(score_raw) if score_raw.isdigit() else 0,
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


def render_report(papers: list[ReportPaper], settings: Settings) -> tuple[str, str]:
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
    html = template.render(papers=papers, window_start=window_start, window_end=today, count=len(papers))

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
    text = "\n".join(lines)
    return html, text


def main() -> int:
    load_dotenv()
    settings = load_settings()
    papers = collect_weekly_papers(settings)
    logger.info("Weekly report covers %d paper(s)", len(papers))

    html, text = render_report(papers, settings)
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
