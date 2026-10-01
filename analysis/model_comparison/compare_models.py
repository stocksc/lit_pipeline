"""Compare Claude models on the pipeline's triage and deep-read steps.

Runs the production calls (`llm_triage.triage_paper`, `llm_deep_read.deep_read_paper`)
on a fixed sample of papers with only the model swapped, records every call's
output, tokens, cost and latency in results.json, and renders a side-by-side
report.html.

    uv run python analysis/model_comparison/compare_models.py snapshot
    uv run python analysis/model_comparison/compare_models.py run [--models M ...] [--papers ID ...]
    uv run python analysis/model_comparison/compare_models.py report

Leakage controls:
- Inputs come only from arXiv (versioned metadata + versioned PDF), snapshotted
  once into inputs/ so every model sees byte-identical text. The papers sheet is
  never read: it holds earlier model output (e.g. triage_score is overwritten by
  the deep read's re-rating).
- Every call is a fresh single-turn request; no prompt caching; repeats are
  separate calls. The only output carried between steps is a model's own triage
  (repeat 1), fed into that same model's deep read.
- No server-side fallbacks, so a refusal stays a refusal instead of being
  answered by another model.
- Nothing is written to the sheet and no email is sent.

`run` is resumable: results.json is saved after every call, completed cells are
skipped, and errored cells are retried, so adding a model later only pays for
its new cells.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

from lit_pipeline.arxiv_client import PaperCandidate, download_pdf_bytes, fetch_by_ids
from lit_pipeline.config import load_settings
from lit_pipeline.llm_deep_read import DEEP_READ_SYSTEM_PROMPT, deep_read_paper
from lit_pipeline.llm_triage import TRIAGE_SYSTEM_PROMPT, triage_paper
from lit_pipeline.pdf_extract import extract_pdf_text
from lit_pipeline.pricing import PRICING
from lit_pipeline.schemas import DeepReadResult, TriageResult

logger = logging.getLogger("compare_models")

HERE = Path(__file__).resolve().parent
INPUTS_DIR = HERE / "inputs"
RESULTS_PATH = HERE / "results.json"
REPORT_PATH = HERE / "report.html"
TEMPLATE_NAME = "report.html.jinja"

# Versioned ids: the comparison pins exactly these versions.
PAPERS = ["2209.07850v5", "2512.23943v2", "2504.21259v2"]

MODELS = ["claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]
MODEL_LABELS = {
    "claude-haiku-4-5": "Haiku 4.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-opus-5-5": "Opus 5.5",
    "claude-fable-5-1": "Fable 5.1",
}
# How each model runs when the request sets neither `thinking` nor `effort`,
# which is how production calls it. From the Models API capabilities and
# Anthropic's migration docs, checked 2026-10-01.
MODEL_NOTES = {
    "claude-haiku-4-5": "No thinking (not enabled by production); no effort control; older tokenizer",
    "claude-sonnet-5-5": "Adaptive thinking (its only thinking mode); API-default effort",
    "claude-opus-5-5": "Adaptive thinking, always on; default effort medium",
    "claude-fable-5-1": "Adaptive thinking, always on; default effort high",
}

TRIAGE_REPEATS = 3
# Production's 1024 is sized for Haiku; thinking models need room to think
# before answering (max_tokens caps thinking + answer together).
TRIAGE_MAX_TOKENS = 16000

# Display only, never sent to a model: production's earlier scores for these
# papers (Haiku 4.5 triage -> Opus 5 deep read), read from the papers sheet on
# 2026-10-01.
PRODUCTION_REFERENCE = {
    "2209.07850": {"triage": 9, "deep_read": 9},
    "2512.23943": {"triage": 9, "deep_read": 9},
    "2504.21259": {"triage": 9, "deep_read": 8},
}

# September 2026 production volume (papers triaged / deep-read, excluding
# manual deep dives) -- the default basis for the monthly cost projection.
DEFAULT_MONTHLY_TRIAGE = 66
DEFAULT_MONTHLY_DEEP_READS = 27

# The deep-read prompt's length targets.
SUMMARY_TARGET_WORDS = 100
BULLETS_TARGET_WORDS = 50


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bare_id(versioned: str) -> str:
    return versioned.rsplit("v", 1)[0]


def _config_hash(interests: str) -> str:
    """Everything that shapes a request apart from the model and the paper.
    Cells from different configurations must never be mixed in one results
    file, so `run` refuses to continue if this changes."""
    parts = [
        TRIAGE_SYSTEM_PROMPT,
        DEEP_READ_SYSTEM_PROMPT,
        json.dumps(TriageResult.model_json_schema(), sort_keys=True),
        json.dumps(DeepReadResult.model_json_schema(), sort_keys=True),
        interests,
        str(TRIAGE_MAX_TOKENS),
    ]
    return _sha256("\n\x00\n".join(parts))


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    settings = load_settings()
    INPUTS_DIR.mkdir(exist_ok=True)
    wanted = [p for p in PAPERS if args.force or not (INPUTS_DIR / f"{_bare_id(p)}.json").exists()]
    if not wanted:
        print("All inputs already snapshotted (use --force to re-fetch).")
        return 0

    found = {c.arxiv_id: c for c in fetch_by_ids(wanted)}
    for versioned in wanted:
        bare = _bare_id(versioned)
        version = versioned[len(bare):]
        candidate = found.get(bare)
        if candidate is None:
            raise SystemExit(f"arXiv returned nothing for {versioned}")
        if not candidate.link.endswith(versioned):
            raise SystemExit(f"Asked for {versioned} but arXiv returned {candidate.link}")

        pdf_text = extract_pdf_text(
            download_pdf_bytes(candidate.pdf_url), max_pages=settings.deep_read.max_pdf_pages
        )
        snapshot = {
            "arxiv_id": bare,
            "version": version,
            "title": candidate.title,
            "authors": candidate.authors,
            "published_date": candidate.published_date,
            "abstract": candidate.abstract,
            "link": candidate.link,
            "pdf_url": candidate.pdf_url,
            "pdf_text": pdf_text,
            "pdf_text_sha256": _sha256(pdf_text),
            "snapshot_at": _now(),
        }
        (INPUTS_DIR / f"{bare}.json").write_text(json.dumps(snapshot, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{versioned}: {len(pdf_text):,} chars of text, sha256 {snapshot['pdf_text_sha256'][:12]}")
    return 0


def _load_inputs(paper_filter: list[str] | None) -> list[dict]:
    inputs = []
    for versioned in PAPERS:
        bare = _bare_id(versioned)
        if paper_filter and bare not in paper_filter and versioned not in paper_filter:
            continue
        path = INPUTS_DIR / f"{bare}.json"
        if not path.exists():
            raise SystemExit(f"No snapshot for {versioned}; run the snapshot command first.")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if _sha256(snapshot["pdf_text"]) != snapshot["pdf_text_sha256"]:
            raise SystemExit(f"{path.name} was modified after it was snapshotted")
        inputs.append(snapshot)
    return inputs


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _load_results() -> dict:
    if RESULTS_PATH.exists():
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    return {"experiment": {}, "papers": {}, "cells": {}}


def _save_results(results: dict) -> None:
    tmp = RESULTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RESULTS_PATH)


def _cell_key(paper: str, model: str, step: str, repeat: int) -> str:
    return f"{paper}|{model}|{step}|{repeat}"


def _call(fn) -> dict:
    """Run one API call and capture its result, usage, cost and latency --
    or the error, if it raised."""
    started = time.monotonic()
    cell: dict = {"started_at": _now()}
    try:
        result, usage = fn()
    except Exception as exc:  # recorded per cell; the run carries on
        cell.update(status="error", error=f"{type(exc).__name__}: {exc}")
    else:
        cell.update(
            status="ok",
            output=result.model_dump(),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )
    cell["latency_s"] = round(time.monotonic() - started, 1)
    return cell


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    models = args.models or MODELS
    unknown = [m for m in models if m not in MODELS]
    if unknown:
        raise SystemExit(f"Unknown model(s) {unknown}; add them to MODELS first")
    missing_prices = [m for m in models if m not in PRICING]
    if missing_prices:
        raise SystemExit(f"No pricing.py entry for {missing_prices}")
    inputs = _load_inputs(args.papers)

    results = _load_results()
    config_hash = _config_hash(settings.interests)
    experiment = results["experiment"]
    if experiment and experiment.get("config_hash") != config_hash:
        raise SystemExit(
            "Prompts, schemas, interests or triage max_tokens changed since results.json was created. "
            "Move results.json aside to start a new experiment rather than mixing configurations."
        )
    if not experiment:
        experiment.update(
            created_at=_now(),
            config_hash=config_hash,
            interests_sha256=_sha256(settings.interests),
            triage_repeats=TRIAGE_REPEATS,
            triage_max_tokens=TRIAGE_MAX_TOKENS,
        )
    for snap in inputs:
        results["papers"][snap["arxiv_id"]] = {
            "version": snap["version"],
            "title": snap["title"],
            "link": snap["link"],
            "pdf_text_sha256": snap["pdf_text_sha256"],
            "pdf_text_chars": len(snap["pdf_text"]),
        }
    _save_results(results)

    client = Anthropic()
    cells = results["cells"]
    total = len(inputs) * len(models) * (TRIAGE_REPEATS + 1)
    done_before = sum(
        1
        for snap in inputs
        for m in models
        for key in [_cell_key(snap["arxiv_id"], m, "triage", r) for r in range(1, TRIAGE_REPEATS + 1)]
        + [_cell_key(snap["arxiv_id"], m, "deep_read", 1)]
        if cells.get(key, {}).get("status") == "ok"
    )
    print(f"{total} cells in scope, {done_before} already complete")
    spent = 0.0
    position = 0

    def record(key: str, cell: dict, label: str) -> None:
        nonlocal spent, position
        cells[key] = cell
        _save_results(results)
        spent += cell.get("cost_usd", 0.0)
        position += 1
        outcome = f"ok ${cell['cost_usd']:.4f}" if cell["status"] == "ok" else cell["status"].upper()
        print(f"[{position}] {label}: {outcome} ({cell.get('latency_s', 0)}s)", flush=True)
        if cell["status"] != "ok":
            print(f"      {cell.get('error') or cell.get('reason')}", flush=True)

    for snap in inputs:
        paper = snap["arxiv_id"]
        candidate = PaperCandidate(
            arxiv_id=paper,
            title=snap["title"],
            authors=snap["authors"],
            published_date=snap["published_date"],
            abstract=snap["abstract"],
            link=snap["link"],
            pdf_url=snap["pdf_url"],
        )
        for model in models:
            label = MODEL_LABELS[model]
            triage_settings = settings.triage.model_copy(update={"model": model})
            for repeat in range(1, TRIAGE_REPEATS + 1):
                key = _cell_key(paper, model, "triage", repeat)
                if cells.get(key, {}).get("status") == "ok":
                    continue
                cell = _call(
                    lambda: triage_paper(
                        client, triage_settings, settings.interests, candidate, max_tokens=TRIAGE_MAX_TOKENS
                    )
                )
                record(key, {"paper": paper, "model": model, "step": "triage", "repeat": repeat, **cell},
                       f"{paper} {label} triage #{repeat}")

            key = _cell_key(paper, model, "deep_read", 1)
            if cells.get(key, {}).get("status") == "ok":
                continue
            triage_1 = cells.get(_cell_key(paper, model, "triage", 1), {})
            base = {"paper": paper, "model": model, "step": "deep_read", "repeat": 1}
            if triage_1.get("status") != "ok":
                record(key, {**base, "status": "skipped", "reason": "this model's triage #1 failed"},
                       f"{paper} {label} deep read")
                continue
            given_score = triage_1["output"]["score"]
            given_rationale = triage_1["output"]["rationale"]
            deep_settings = settings.deep_read.model_copy(update={"model": model})
            cell = _call(
                lambda: deep_read_paper(
                    client,
                    deep_settings,
                    settings.interests,
                    candidate,
                    snap["pdf_text"],
                    triage_score=given_score,
                    triage_rationale=given_rationale,
                )
            )
            record(
                key,
                {**base, "given_triage_score": given_score, "given_triage_rationale": given_rationale, **cell},
                f"{paper} {label} deep read",
            )

    errors = [k for k, c in cells.items() if c.get("status") != "ok"]
    total_cost = sum(c.get("cost_usd", 0.0) for c in cells.values())
    print(f"Spent ${spent:.4f} this run; ${total_cost:.4f} across all recorded cells")
    if errors:
        print(f"{len(errors)} cell(s) not ok: {', '.join(errors)}")
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _words(text: str) -> int:
    return len(text.split())


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def cmd_report(args: argparse.Namespace) -> int:
    results = _load_results()
    if not results["cells"]:
        raise SystemExit("results.json has no cells yet; run the run command first")
    cells = results["cells"]
    papers = results["papers"]
    models = [m for m in MODELS if any(c["model"] == m for c in cells.values())]

    def cell(paper: str, model: str, step: str, repeat: int = 1) -> dict:
        return cells.get(_cell_key(paper, model, step, repeat), {"status": "missing"})

    cost_rows = []
    for m in models:
        row = {"model": m, "label": MODEL_LABELS[m], "note": MODEL_NOTES[m], "pricing": PRICING[m]}
        steps = ("triage", "deep_read")
        for step in steps:
            ok = [c for c in cells.values() if c["model"] == m and c["step"] == step and c["status"] == "ok"]
            row[step] = {
                "n": len(ok),
                "avg_cost": _mean([c["cost_usd"] for c in ok]),
                "avg_in": _mean([c["input_tokens"] for c in ok]),
                "avg_out": _mean([c["output_tokens"] for c in ok]),
                "avg_latency": _mean([c["latency_s"] for c in ok]),
                "total_cost": sum(c["cost_usd"] for c in ok),
                "total_in": sum(c["input_tokens"] for c in ok),
                "total_out": sum(c["output_tokens"] for c in ok),
            }
        row["total_in"] = sum(row[s]["total_in"] for s in steps)
        row["total_out"] = sum(row[s]["total_out"] for s in steps)
        if row["triage"]["avg_cost"] is not None and row["deep_read"]["avg_cost"] is not None:
            row["monthly"] = (
                args.monthly_triage * row["triage"]["avg_cost"]
                + args.monthly_deep_reads * row["deep_read"]["avg_cost"]
            )
        cost_rows.append(row)

    paper_sections = []
    for paper, meta in papers.items():
        columns = []
        for m in models:
            triage = [cell(paper, m, "triage", r) for r in range(1, TRIAGE_REPEATS + 1)]
            deep = cell(paper, m, "deep_read")
            out = deep.get("output") or {}
            columns.append(
                {
                    "model": m,
                    "label": MODEL_LABELS[m],
                    "triage": triage,
                    "triage_scores": [t["output"]["score"] if t["status"] == "ok" else None for t in triage],
                    "deep": deep,
                    "out": out,
                    "summary_words": _words(out.get("summary", "")),
                    "relevance_words": sum(_words(b) for b in out.get("relevance", [])),
                    "limitations_words": sum(_words(b) for b in out.get("limitations", [])),
                }
            )
        paper_sections.append(
            {"id": paper, "meta": meta, "reference": PRODUCTION_REFERENCE.get(paper), "columns": columns}
        )

    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html", "jinja"]))
    html = env.get_template(TEMPLATE_NAME).render(
        experiment=results["experiment"],
        papers=papers,
        cost_rows=cost_rows,
        paper_sections=paper_sections,
        total_cost=sum(c.get("cost_usd", 0.0) for c in cells.values()),
        monthly_triage=args.monthly_triage,
        monthly_deep_reads=args.monthly_deep_reads,
        triage_repeats=TRIAGE_REPEATS,
        summary_target=SUMMARY_TARGET_WORDS,
        bullets_target=BULLETS_TARGET_WORDS,
        threshold=load_settings().triage.score_threshold,
        generated_at=_now(),
    )
    REPORT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {REPORT_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_snapshot = sub.add_parser("snapshot", help="Fetch and freeze the paper inputs")
    p_snapshot.add_argument("--force", action="store_true", help="Re-fetch papers that are already snapshotted")
    p_snapshot.set_defaults(fn=cmd_snapshot)

    p_run = sub.add_parser("run", help="Run triage and deep reads for every paper x model")
    p_run.add_argument("--models", nargs="+", help=f"Subset of {MODELS}")
    p_run.add_argument("--papers", nargs="+", help="Subset of paper ids")
    p_run.set_defaults(fn=cmd_run)

    p_report = sub.add_parser("report", help="Render report.html from results.json")
    p_report.add_argument("--monthly-triage", type=int, default=DEFAULT_MONTHLY_TRIAGE)
    p_report.add_argument("--monthly-deep-reads", type=int, default=DEFAULT_MONTHLY_DEEP_READS)
    p_report.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
