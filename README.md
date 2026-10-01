# lit_pipeline — automated arXiv literature tracker

A personal research assistant that watches arXiv for you. Every day it
searches for papers matching your interests, triages each one for
relevance, deep-reads and critiques the ones worth your time, and emails
you a digest — fully automated, running on GitHub Actions, with a single
Google Sheet as its entire database.

Built for tracking a specific niche (fair lending / algorithmic fairness
research, in this instance) that no off-the-shelf tool watches for you.
Retarget it to any topic by editing a few lines of free text.

## What it does

1. **Search** — daily, every new arXiv submission since the last run is
   harvested from arXiv's OAI-PMH metadata feed and kept if its abstract
   matches your keywords/phrases.
2. **Triage** — every new paper's *abstract* gets a 0-10 relevance score
   and a one-line rationale from Claude Haiku (cheap, fast).
3. **Route by score, into three tiers**:
   - **Deep dive** (score ≥ threshold): the full paper is fetched, text-
     extracted, and critiqued by Claude Opus — a ~100-word summary, why
     it's relevant, and what limits its practical usefulness.
   - **Potentially relevant** (mid band): a cheap ~50-word summary from
     the abstract alone, no full-paper fetch.
   - **Everything else**: title and score only, no further LLM calls.
4. **Store** — every paper is one row in a single Google Sheet, which is
   the pipeline's *only* database. A `status` column tracks exactly where
   each paper is, so the whole thing is safe to kill and re-run at any
   point — it just picks up where it left off.
5. **Report** — weekly, a digest email goes out via Resend: deep-dive
   cards up top, mid-tier cards below, a title-only table for the rest,
   and a running cost breakdown.
6. **Backfill on demand** — the same pipeline can be pointed at any past
   date range instead of "today," with a dry-run mode that prices out the
   expensive part *before* you commit to it.
7. **Manual deep dive** — hand it a list of arXiv IDs and it force-runs
   exactly those papers through triage *and* deep-read, whatever they
   score and whether or not your keywords would ever have found them,
   without those off-topic one-offs polluting your weekly digest.

```
GitHub Actions (daily cron)
  -> lit-daily -> arXiv OAI-PMH harvest + keyword filter -> Haiku triage (0-10)
       score >= threshold                  -> Opus deep-read (full paper)  -> "Deep Dive" tier
       mid_threshold <= score < threshold  -> Haiku mid-summary (abstract) -> "Potentially Relevant" tier
       score < mid_threshold               -> nothing further              -> title-only tier
     -> all of it lands as columns on one Google Sheet row per paper
  -> Mondays only, after the above:
     lit-weekly -> Google Sheet -> three-tier HTML digest -> Resend -> your inbox

you, manually
  -> lit-backfill --start-date ... --end-date ... [--dry-run]
     -> same pipeline, scoped to a historical window instead of "today"
  -> lit-deep-dive 2501.12345 2502.09876 [--dry-run]
     -> same pipeline, scoped to papers you named -- deep-read regardless of score
```

## Cool features

A few things that came out of actually running this for a while and
noticing what mattered:

- **Deep-read re-rates the paper, and a downgrade doesn't waste the
  work.** Once Opus has read the *whole* paper, that's a strictly
  better-informed relevance judgment than Haiku's abstract-only guess —
  so the deep-read score overwrites the triage score. If a paper's
  re-rated score drops out of the top tier, it doesn't just vanish: it
  reuses Opus's own summary in the mid-tier report instead of getting
  re-summarized from scratch. Report tiering reads whatever the *current*
  score is at render time, rather than committing to a tier the moment a
  paper is first triaged.
- **References/appendix trimming, not a blunt page cap.** Extracted PDF
  text is cut right at the "References" heading — a bibliography is pure
  token cost with zero summarization value, and cutting there also drops
  any appendix after it. A generous page-count cap backstops the rare
  pathological case (an arXiv search can accidentally sweep up a 250+
  page thesis and hand it to Opus like it's a normal 10-page paper) where
  the heading can't be trusted. On one real 257-page outlier, this took a
  deep-read from ~$2.00 to ~$0.25 with no loss in summary quality.
- **Cost projections that are grounded in your own history, not a
  hardcoded guess.** `lit-backfill --dry-run` runs real (cheap) triage
  over your requested range, then projects the dollar cost of the
  expensive deep-read phase using a live average pulled from your sheet's
  actual historical costs — so the estimate gets more accurate on its own
  as the pipeline runs, with zero maintenance.
- **Crash-safe by construction.** There's no job queue and no separate
  state store. A paper's `status` cell *is* the state machine. Interrupt
  a run for any reason and just run it again. The only other state is a
  single date in the sheet's `harvest_state` tab: the last day ingest
  fully covered. The next run harvests from there, so a day arXiv is down
  is caught up by the next run rather than lost.
- **One sheet, one row per paper.** Triage results, mid-tier summaries,
  full deep-read critiques, every stage's token counts and cost — all of
  it lives as columns on the same row. Open the sheet and everything
  about a paper is right there, sortable and filterable, no joins.

See `plan` conversation / architecture notes for the full design
rationale. Everything below this point covers one-time setup and
day-to-day usage.

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
   Domains in the Resend dashboard. Either way, sending to your own address
   (set as `REPORT_RECIPIENT_EMAIL`, see below) should work immediately.

### 4. Your interests + arXiv queries

Edit `config/settings.yaml`:
- `interests`: free text describing what's relevant to you. This goes
  verbatim into both the triage and deep-read prompts.
- `arxiv.specific_keywords`: plain phrases (no query syntax needed) -- any ONE
  appearing in a paper's abstract is enough to include it.
- `arxiv.broad_keywords`: plain single words that are individually too
  overloaded to trust alone (e.g. "discrimination" is common physics/stats
  vocabulary) -- a paper only qualifies via this list if at least 2 of them
  co-occur in the same abstract.
- `triage.score_threshold`: 0-10 cutoff for moving a paper to deep-read.

### 5. Local `.env` for testing before you push to GitHub

```bash
cp .env.example .env
# then fill in ANTHROPIC_API_KEY, RESEND_API_KEY, REPORT_RECIPIENT_EMAIL,
# and either GOOGLE_SERVICE_ACCOUNT_FILE (path to the downloaded json key) or
# GOOGLE_SERVICE_ACCOUNT_JSON (its contents, for parity with CI)
```

`REPORT_RECIPIENT_EMAIL` (where the digest gets sent) lives here rather than
in `config/settings.yaml` specifically because that file is committed to the
repo -- keeping a personal email address out of version control.

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
| `REPORT_RECIPIENT_EMAIL` | where the weekly digest gets sent |

Adding a secret to the repo doesn't automatically expose it to every
workflow -- each workflow file's `env:` block has to reference
`${{ secrets.NAME }}` explicitly for that job to see it. `lit-daily`
never sends email, but it loads settings the same way `lit-weekly` does,
so `daily.yml`'s env block needs `REPORT_RECIPIENT_EMAIL` wired in too,
even though the daily job never actually uses it for sending.

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

## Backfilling a past date range

`lit-backfill` runs the same ingest -> triage -> deep-read -> email pipeline
as the daily/weekly cron jobs, but scoped to an explicit window matched
against each paper's arXiv **publish** date, not whenever you happen to run
it. Useful for retroactively sweeping a period you weren't tracking yet, or
exploring a topic you don't track daily.

```bash
# Cheap preview first: ingest + triage + mid-summary only, no deep-read,
# no email. Prints a score histogram plus a rough $ cost projection for
# what the deep-read phase would cost, based on your sheet's own
# historical averages -- so you see a real number before committing.
uv run lit-backfill --start-date 2024-01-01 --end-date 2024-01-31 --dry-run

# Full run: also deep-reads everything >= threshold and emails a
# "Backfill Digest" report for just that window.
uv run lit-backfill --start-date 2024-01-01 --end-date 2024-01-31
```

Flags:
- `--start-date` / `--end-date` (required, `YYYY-MM-DD`, both inclusive)
- `--query "..."` -- override `arxiv.specific_keywords` from settings.yaml for this run only (repeatable); omit to use your standing keyword lists
- `--threshold N` -- override `triage.score_threshold` for this run only (e.g. `--threshold 8` for "just the 8-and-ups")
- `--dry-run` -- stop after triage/mid-summary; no deep-read, no email

**Start with `--dry-run` for anything beyond a narrow window** -- deep-reading
is a real per-paper Opus cost, and a broad query over a wide date range can
easily turn up hundreds of papers. It's safe to re-run the same command
repeatedly (including switching from `--dry-run` to a full run afterward):
already-processed papers are skipped, same as the daily job.

## Deep-diving specific papers

`lit-deep-dive` is the manual override for the pipeline's own judgment. The
daily job only deep-reads a paper if arXiv's keyword search surfaced it *and*
Haiku scored it at/above your threshold; this forces named papers through
triage and deep-read regardless of either. Useful when a colleague sends you
something, when a paper you just read cites something worth a look, or when
you simply disagree with the 3/10 the pipeline gave it.

```bash
# Cheap preview: ingest + triage only, then a $ projection for the
# deep-read, based on your sheet's own historical averages.
uv run lit-deep-dive 2501.12345 --dry-run

# The real thing: deep-reads every ID given, whatever it scored, and
# emails a "Manual Deep Dive" report covering just those papers.
uv run lit-deep-dive 2501.12345 2502.09876

# Write the results to the sheet without the email.
uv run lit-deep-dive 2501.12345 --no-email

# Re-read a paper you already have a deep-read for (e.g. after rewriting
# your `interests`), instead of reusing the stored one.
uv run lit-deep-dive 2501.12345 --refresh

# Email the write-up and the paper together: one PDF per paper, deep read
# on page 1, the paper itself appended after it.
uv run lit-deep-dive 2501.12345 2502.09876 --attach-pdfs
```

IDs can be given however you happen to have them -- `2501.12345`,
`2501.12345v2`, `arXiv:2501.12345`, a pasted `arxiv.org/abs/...` URL, or a
pre-2007 style ID like `hep-th/9711200` -- and anything that isn't a
recognizable arXiv ID is rejected up front, before a single paper is paid
for. Papers your sheet has never seen are fetched by
ID and added to it, with no keyword filter applied: you asked for these
specifically, so it doesn't matter whether they'd have matched your standing
queries.

Flags:
- `--dry-run` -- stop after triage; no deep-read, no email
- `--no-email` -- deep-read and store as usual, just don't send the report
- `--refresh` -- re-read papers that already have a deep-read, instead of reusing it
- `--attach-pdfs` -- attach one PDF per paper: the deep-read write-up, then the paper

Because every requested paper is deep-read regardless of score, the report
gives all of them a full deep-dive card rather than sorting them into tiers,
and skips the keyword-hit tables (nothing here was found by keyword).

**A paper that already has a deep-read is reused, not re-read.** Asking for
it again costs nothing and produces the same report card -- so it's safe to
re-run a command, or to include an ID you've dived before alongside new ones,
without paying twice. Such a paper isn't re-triaged either: `triage_score`
holds Opus's post-deep-read re-rating, so a fresh abstract-only score would
overwrite a better-informed one. `--refresh` opts into a genuine re-read when
you want one -- worth it after you've rewritten your `interests`, or when a
read came back poor. A paper whose deep-read *failed* has nothing to reuse,
so it's simply retried.

### Getting the papers themselves attached

`--attach-pdfs` turns the report email into something you can read away from
the sheet. Each paper becomes one PDF: the full deep-read write-up as page 1
(title, authors and affiliations, score with the original triage score and
Opus's reasoning for the change, summary, relevance, limitations), then the
paper's own PDF appended after it. The email body stays the usual report, so
you can still skim the cards and open only what you want. Attachments are
named `2501.12345 - Paper Title.pdf`, so a folder of them sorts by ID and
stays searchable.

Resend caps an email at 40MB after encoding, which a few image-heavy papers
can reach. Rather than truncating, the run splits across numbered emails
(`[1/2]`, `[2/2]`); each one carries the full report body, so any single email
still makes sense on its own when forwarded. A paper too large to fit in *any*
email, or whose PDF can't be downloaded, is attached as the write-up alone and
called out in the body -- the paper is still one click away via its link.

Because a stored deep-read is reused rather than re-read, running this over
IDs you've already dived costs nothing:

```bash
# Free: no LLM calls, just rebuilds and re-sends the bundles.
uv run lit-deep-dive 2501.12345 2502.09876 --attach-pdfs
```

**These papers don't leak into your weekly digest.** Anything this command
ingests that your keyword lists would never have matched is flagged
`manual_only` in the sheet, and the weekly report and backfill reports skip
flagged rows entirely -- including their cost, so an ad-hoc Opus read doesn't
show up as pipeline spend. The flag is deliberately *not* set on a paper that
does match your keywords: the daily job would have found and reported that
one anyway, so hiding it would leave a hole in the digest rather than keep it
clean. Same for a paper already in your sheet when you dive it -- it was
already being tracked, so it keeps reporting as usual (with the deep-read you
just paid for, which may move it up a tier). Papers older than the digest's
lookback window can't appear either way, since every report scopes by arXiv
publish date.

## Running in GitHub Actions

`.github/workflows/daily.yml` runs on cron (13:00 UTC, though GitHub often
starts scheduled runs hours late). On Mondays its last step also sends the
weekly digest, so the email always follows that day's ingest. That step
runs even if the pipeline step failed. `weekly.yml` has no schedule: it's
there to send a digest by hand. Both can be triggered from the Actions tab
(**Run workflow** button). Do this once after your first push to confirm
secrets are wired up correctly before trusting the schedule. A manual
daily run never sends the digest, even on a Monday. GitHub emails the repo
owner automatically if a scheduled workflow run fails, which is sufficient
alerting for a single-user project.

Daily ingest uses arXiv's OAI-PMH interface rather than its search API.
The search API is capacity-constrained and throttles per IP. GitHub's
shared runner IPs were regularly refused (HTTP 429/406) on a run's very
first request. Backfill and manual deep dives still use the search API.
They're run by hand, so if arXiv refuses one, just run it again later.

## Cost

Deep-read (Opus, full paper) dominates the bill -- triage (Haiku, per
abstract) and mid-summary (Haiku, per abstract) both run well under
$0.01/paper regardless of paper length, since they only ever see the
abstract.

Deep-read cost depends heavily on paper length, since the full extracted
text goes to Opus. Extraction trims at the References heading (dropping
the bibliography and any appendix after it) with a generous page-count
cap as a backstop, which keeps typical papers in the tens of thousands of
input tokens and bounds the rare oversized outlier (a thesis or monograph
that happened to match your query) instead of sending it in full.

Real per-paper cost still varies a lot, so don't assume a fixed rate.
Every paper's exact token usage and cost is recorded per-row in the sheet
(`triage_cost_usd`, `mid_summary_cost_usd`, `deep_read_cost_usd`), the
weekly report summarizes cost for its window, and the `--dry-run` mode of
`lit-backfill`/`lit-deep-dive` projects the cost of a prospective run
before you commit to it -- that's the most reliable way to know what
something will actually cost.
