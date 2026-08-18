# lit_pipeline — automated arXiv literature tracker

Pulls candidate papers from arXiv daily, scores them for relevance with
Claude Haiku, deep-reads (and critiques) the ones that clear the bar with
Claude Opus, stores everything in a Google Sheet, and emails a weekly digest.

See `plan` conversation / architecture notes for the full design rationale.
This README covers the one-time setup and day-to-day usage.

## How it fits together

```
GitHub Actions (daily cron)  ->  lit-daily   ->  arXiv search -> Haiku triage -> Opus deep-read -> Google Sheet
GitHub Actions (weekly cron) ->  lit-weekly  ->  Google Sheet -> HTML report  -> Resend -> your inbox
```

The Google Sheet is the *only* durable state. Both scripts are safe to
re-run any time (locally or via GitHub's "Run workflow" button) -- they
resume from whatever the sheet's `status` column says is left to do.

## One-time setup

### 1. Anthropic API key

Create a key at [console.anthropic.com](https://console.anthropic.com) ->
Settings -> API Keys. You'll use this as `ANTHROPIC_API_KEY`.

### 2. Google Sheet + service account

A **service account** is a robot Google identity your code can log in as
without a browser. You create it once, share your Sheet with its email
address, and it can then read/write that Sheet headlessly (essential for
GitHub Actions, which has no browser).

1. Go to [Google Cloud Console](https://console.cloud.google.com), create a
   new project (free).
2. APIs & Services -> Enable APIs -> enable the **Google Sheets API**.
3. APIs & Services -> Credentials -> Create Credentials -> Service Account.
   Give it any name (e.g. `lit-tracker`).
4. Open the new service account -> Keys -> Add Key -> Create new key -> JSON.
   This downloads a `.json` key file -- **never commit this file**.
5. Create a new Google Sheet (any name, empty is fine). Copy its ID out of
   the URL: `https://docs.google.com/spreadsheets/d/THIS_PART/edit`.
6. Click Share on the Sheet and share it with the service account's email
   (looks like `lit-tracker@your-project.iam.gserviceaccount.com`, found in
   the downloaded JSON as `client_email`), with **Editor** access.
7. Put the sheet ID in `config/settings.yaml` under `google_sheets.sheet_id`.

### 3. Resend (email delivery)

1. Sign up at [resend.com](https://resend.com) (free tier).
2. Create an API key -> this is `RESEND_API_KEY`.
3. For `weekly_report.sender_email` in `config/settings.yaml`: Resend's
   shared `onboarding@resend.dev` sender works out of the box while you're
   getting set up; sending from your own domain requires verifying it under
   Domains in the Resend dashboard. Either way, sending to your own
   `recipient_email` should work immediately.

### 4. Your interests + arXiv queries

Edit `config/settings.yaml`:
- `interests`: free text describing what's relevant to you. This goes
  verbatim into both the triage and deep-read prompts.
- `arxiv.queries`: arXiv search-syntax strings (field prefixes `au:`, `abs:`,
  `ti:`, `cat:`, combinable with `AND`/`OR`). See the
  [arXiv API query manual](https://arxiv.org/help/api/user-manual#query_details).
- `triage.score_threshold`: 0-10 cutoff for moving a paper to deep-read.

### 5. Local `.env` for testing before you push to GitHub

```bash
cp .env.example .env
# then fill in ANTHROPIC_API_KEY, RESEND_API_KEY, and either
# GOOGLE_SERVICE_ACCOUNT_FILE (path to the downloaded json key) or
# GOOGLE_SERVICE_ACCOUNT_JSON (its contents, for parity with CI)
```

### 6. Push to GitHub + add repo secrets

```bash
git add -A
git commit -m "Initial literature tracker"
gh repo create --source=. --private --push   # or create manually on github.com and add a remote
```

Then in the GitHub repo: **Settings -> Secrets and variables -> Actions ->
New repository secret**, add:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the downloaded JSON key file |
| `RESEND_API_KEY` | from step 3 |

(`config/settings.yaml` -- including the sheet ID -- is committed to the
repo as regular config, not a secret; access is still gated by who the
Sheet is shared with.)

## Running locally

```bash
uv sync
uv run lit-daily     # steps 1-6: ingest, triage, deep-read
uv run lit-weekly     # step 8: build + send the weekly digest
```

Or explore interactively in `notebooks/exploration.ipynb` (uses the same
`src/lit_pipeline` code, good for testing one paper at a time before
running the whole pipeline).

## Running in GitHub Actions

`.github/workflows/daily.yml` and `weekly.yml` run on cron (UTC) and can
also be triggered manually from the Actions tab (**Run workflow** button) --
do this once after your first push to confirm secrets are wired up correctly
before trusting the schedule. GitHub emails the repo owner automatically if
a scheduled workflow run fails, which is sufficient alerting for a
single-user project.

## Cost

Triage (Haiku, per abstract): well under $0.001/paper. Deep read (Opus,
full PDF): roughly $0.05-0.15/paper depending on length. Actual spend
depends entirely on how broad your queries and threshold are -- keep an eye
on usage at console.anthropic.com for the first couple of weeks.
