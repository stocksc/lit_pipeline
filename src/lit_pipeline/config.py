"""Loads config/settings.yaml into typed, validated settings objects."""

from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml
from pydantic import BaseModel

# src/lit_pipeline/config.py -> project root is three parents up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class ArxivSettings(BaseModel):
    queries: list[str]
    max_results_per_query: int = 50
    max_age_days: int = 7


class TriageSettings(BaseModel):
    model: str = "claude-haiku-4-5"
    score_threshold: int = 7


class DeepReadSettings(BaseModel):
    model: str = "claude-opus-5"


class GoogleSheetsSettings(BaseModel):
    sheet_id: str
    papers_tab: str = "papers"
    deep_reads_tab: str = "deep_reads"


class WeeklyReportSettings(BaseModel):
    lookback_days: int = 7
    recipient_email: str
    sender_email: str
    subject_prefix: str = "Weekly Lit Digest"


class RetrySettings(BaseModel):
    max_retry_count: int = 3


class Settings(BaseModel):
    interests: str
    arxiv: ArxivSettings
    triage: TriageSettings
    deep_read: DeepReadSettings
    google_sheets: GoogleSheetsSettings
    weekly_report: WeeklyReportSettings
    retries: RetrySettings = RetrySettings()


def load_settings(path: Union[Path, str, None] = None) -> Settings:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Settings.model_validate(raw)
