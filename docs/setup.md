# Setup

Getting your own copy running takes four accounts (Anthropic, Google Cloud,
Resend and GitHub) and about half an hour. Everything below is one-time,
apart from editing `config/settings.yaml` whenever your interests change.

Back to the [README](../README.md).

## 1. Anthropic API key

Create a key at [console.anthropic.com](https://console.anthropic.com) →
Settings → API Keys. This is `ANTHROPIC_API_KEY`.

## 2. Google Sheet and service account

The pipeline keeps all of its state in one Google Sheet. A **service
account** is a Google identity your code can log in as without a browser,
which GitHub Actions needs. You share the Sheet with its email address and
it can then read and write the Sheet on its own.

1. In the [Google Cloud Console](https://console.cloud.google.com), create a
   new project (free).
2. APIs & Services → Enable APIs → enable the **Google Sheets API**.
3. APIs & Services → Credentials → Create Credentials → Service Account.
   Any name works (e.g. `lit-tracker`).
4. Open the new service account → Keys → Add Key → Create new key → JSON.
   This downloads a `.json` key file. **Never commit this file.**
5. Create an empty Google Sheet and copy its ID from the URL:
   `https://docs.google.com/spreadsheets/d/THIS_PART/edit`.
6. Share the Sheet with the service account's email, with **Editor**
   access. The address looks like
   `lit-tracker@your-project.iam.gserviceaccount.com` and is the
   `client_email` field in the downloaded JSON.
7. Put the sheet ID in `config/settings.yaml` under `google_sheets.sheet_id`.

The pipeline creates its tabs and header row on first run.

## 3. Resend (email)

1. Sign up at [resend.com](https://resend.com) (the free tier is plenty).
2. Create an API key. This is `RESEND_API_KEY`.
3. Set `weekly_report.sender_email` in `config/settings.yaml`. Resend's
   shared `onboarding@resend.dev` sender works immediately for sending to
   your own address. To send from your own domain, verify it under Domains
   in the Resend dashboard first.

## 4. Tell it what you care about

Everything about *what* gets tracked lives in `config/settings.yaml`. No
code changes are needed for any of it.

| Setting | What it does |
|---|---|
| `interests` | A free-text briefing on what's relevant to you and what isn't. It goes verbatim into the triage and deep-read prompts, so write it the way you'd brief a research assistant, including what a perfect 10 looks like. |
| `arxiv.specific_keywords` | Plain phrases (no query syntax). Any one of them in an abstract is enough to include the paper. |
| `arxiv.broad_keywords` | Single words too overloaded to trust alone, e.g. "discrimination" is common physics vocabulary. A paper qualifies through this list only if at least two of them appear in the same abstract. |
| `arxiv.max_age_days` | The daily harvest also sees new *versions* of old papers. Anything first submitted more than this many days before the window is skipped. It's also how far back the very first harvest reaches. |
| `triage.model`, `deep_read.model` | Which Claude model runs each step. Any model listed in `src/lit_pipeline/pricing.py` gets its cost tracked. |
| `triage.score_threshold` | Papers scoring at or above this (0–10) get a full deep read. Default 7. |
| `triage.mid_summary_threshold` | Papers between this and `score_threshold` get a short summary from the abstract. Default 4. |
| `deep_read.max_pdf_pages` | Backstop page cap for PDF extraction (the main trim is at the References heading). Default 50. |
| `weekly_report.lookback_days`, `subject_prefix` | The digest's window and subject line. |
| `retries.max_retry_count` | How many failed attempts at a step (one per run) before the paper is parked for inspection. Default 3. |

## 5. Local `.env`

```bash
cp .env.example .env
```

Then fill in `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `REPORT_RECIPIENT_EMAIL`,
and either `GOOGLE_SERVICE_ACCOUNT_FILE` (the path to the downloaded key) or
`GOOGLE_SERVICE_ACCOUNT_JSON` (the key's contents, which is what CI uses).

`REPORT_RECIPIENT_EMAIL` is where the digest goes. It's read from the
environment rather than `settings.yaml` because that file is committed, and
a personal address shouldn't be.

## 6. Run it locally

```bash
uv sync
uv run lit-daily    # harvest, triage, summarize, deep-read
uv run lit-weekly   # build and send the digest for the last week
```

Every command is safe to interrupt and re-run: anything already finished is
skipped. `notebooks/exploration.ipynb` uses the same code and is handy for
trying one paper at a time.

The manual commands (`lit-backfill`, `lit-deep-dive`) are described in the
[README](../README.md#beyond-the-daily-run).

## 7. Push to GitHub and add secrets

```bash
gh repo create --source=. --private --push   # or create it on github.com and add a remote
```

Then in the repo: **Settings → Secrets and variables → Actions → New
repository secret**, and add:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the JSON key file |
| `RESEND_API_KEY` | from step 3 |
| `REPORT_RECIPIENT_EMAIL` | where the digest goes |

A repo secret is only visible to a job whose `env:` block references it.
`daily.yml` passes `REPORT_RECIPIENT_EMAIL` even though the daily step never
sends mail, because loading settings requires it.

`config/settings.yaml`, including the sheet ID, is committed as ordinary
config. Access to the data is controlled by who the Sheet is shared with.

## 8. The schedule

- **`daily.yml`** runs at 13:00 UTC (GitHub often starts scheduled runs late,
  which doesn't matter here). On Mondays its last step sends the weekly
  digest, so the email always follows that day's harvest. That step runs
  even if the pipeline step failed, so one bad day doesn't swallow the
  week's email.
- **`weekly.yml`** has no schedule. Its **Run workflow** button sends a
  digest on demand. A manual run of `daily.yml` never sends one, even on a
  Monday.

After your first push, run both from the Actions tab once to confirm the
secrets are wired up before trusting the schedule. GitHub emails the repo
owner when a scheduled run fails, which is enough alerting for a one-person
tool.
