**Uploading this backend to GitHub?** See [GITHUB_UPLOAD.md](GITHUB_UPLOAD.md) for safe publishing and Git setup.

**Command naming / 20-item AI batching / cron installation:** see [PROJECT_COMMANDS.md](PROJECT_COMMANDS.md).

# Atlas — SEC EDGAR + AI-first News Research Collector

Standalone Python 3.13 / SQLite / Plotly Dash project. **The canonical
mobile API contract contains five tables:** `companies`, `raw_items`,
`relationships`, `summary`, `price_bars`. The Python collectors/scorers still
access legacy `news`, `forms`, `items` and `news_company_relations` tables in
parts of their write path. Compatibility/migration machinery bridges them to
the canonical schema; a five-table-only SQLite cutover is NOT currently safe
for all Python jobs. Do not delete legacy production tables based on a mobile
API test. See `FIVE_TABLE_CLEANUP_README.md` and the guarded migration script.
The project retains downloaded reports, SEC filing URLs, original Item text,
and saved AI assessments across restarts.

## New: unified processing trace + Android-style company workspace

The Dash navigation now includes **Atlas app**, a company-centric workspace with
search and watchlist, company overview, 15-minute stored-price diagram with
scored News / SEC marker overlays, News and SEC tabs, record detail with
reasoning and original text, responsive layout, theme toggle and paginated lists.
Open **`http://127.0.0.1:8050/mobile`** after starting the existing dashboard.
The original Overview, Companies, SEC filings, Items, News and **Data quality**
pages remain in place; Data quality's source, markup and callbacks have not
been changed. Dashboard browsing does not call paid AI APIs.

SEC, news, correlation/verification, Item AI, relation AI, price collection,
per-candle validation and company-aggregate refresh steps now append to
**`../data/atlas_processing.log`** in timestamped JSONL format. Override with
`ATLAS_LOG_PATH` if all pipeline processes must use a different shared path.
Logs contain exact IDs, decisions, values and explanations, but exclude API
secrets and full copyrighted source/AI prompt bodies. This applies to new
processing runs and does not retroactively reconstruct historical steps.

See **`docs/UNIFIED_PROCESSING_LOG_AND_MOBILE_DASH.md`** for paths, examples,
coverage, caveats and testing details.

## Dashboard (legacy explorer)

The browser interface has six top navigation pages: **Overview**, **Companies**,
**SEC filings**, **Items**, **News**, and **Data quality**. Each list has server-side pagination
and relevant search/status filters. Select a row to open its full details on the
same page. On narrow screens, detail panels use the available screen width.

The Data quality page displays the status of SEC filings, extracted Items, news,
news-event grouping, and company/news relationships. For each status it shows
all-time count, count of records published/first saved in the last 3 hours, and
last status-record update. Latest activity includes the most recent Item first
extraction. It flags parsing and matching anomalies, including records missing
correlation reasons. The page is read-only and adds no database columns.

The compact top statistics strip is restricted to **10% of viewport height**.
It retains total / last-three-hours counts for news and extracted Items, plus
latest news publication, news AI analysis, Item extraction, and Item AI analysis.
**Last Item Extracted** is derived from `items.created_at` (initial extraction);
a later re-extraction of the same saved Item cannot be timestamped independently
without a new field, which this web-only release deliberately does not add.

SEC links, saved reports, source text, direct AI Item actions, and the exact
stored `Company|Reason` correlations are available on their respective detail
pages. The dashboard **reads** the existing SQLite data; browsing never runs a
SEC collector or incurs an OpenAI API request. Item AI actions run only when
explicitly clicked.

Run `dash.bat` or `./dash.sh`, then open `http://127.0.0.1:8050/`.
The underlying `atlas/data/` database/reports layout is unchanged. No schema
migration is introduced by the dashboard changes in this release.

## New atlas layout and exact company/reason correlation

```
atlas/
├── main_back_end/          application code, launchers, tests, assets
└── data/                   persistent edgar.sqlite3, reports, archives, cron state/logs
```

**Run setup, extraction, scoring and Dash from `atlas/main_back_end/`.** SQLite,
report files and raw HTTP archives live in `atlas/data/`, one directory above.
Move your OLD `data` folder contents into `atlas/data` after backing up first.
The ZIP includes **no production database**, and the runtime must never create
a second database in `main_back_end/data`. A pre-existing `SEC_DB_PATH` environment
variable overrides the default; update it to point to the new data location.

The news ingest workflow now makes two **batch-level** AI requests: (1) factual
event grouping, (2) context-first company extraction as `ID|Company|Reason`
lines (`ID|NONE` when none). **No DIRECT/INDIRECT identifiers** are output.
Only after AI has proposed companies does the deterministic exact/unique SEC
catalog resolver run, followed by a proper-name/curated-evidence check.
Weak word correlations (`news` ≠ `NEWS CORP`, `paid` ≠ `PAID INC`) no longer
feed the active correlation path. A verified contract/operation can substantiate
an unmentioned company's exposure to a government announcement; use the optional
`atlas/data/company_exposures.json` and `company_ceos.json` for evidence records.

New fields: `news.company_reasons_json` = JSON array of `"Company|Reason"`
strings; `news_company_relations.correlation_reason` = that specific company's
reason. `ensure_schema()` adds columns to existing SQLite databases and takes
an online backup beforehand. The dashboard displays the reason. See
`docs/AI_FIRST_CORRELATION_AND_WORKFLOW_AUDIT.md` for limitations, migration,
and identified inefficient workflows.

Preview / opt-in refresh of old unscored keyword links:

```bash
python -m news_pipeline.review_existing --limit 100
python -m news_pipeline.review_existing --limit 100 --apply
```

The second command incurs OpenAI API costs. Already-scored historic links remain
untouched and require separate verification.

## v9.1 fixes (17 October 2026)

- Fixed the Windows SEC metadata launcher: `extract_recent_filings.bat` now
  dispatches correctly through `sec_edgar.windows_launcher` (the historical
  `extract` alias also remains valid for `setup.bat`).
- Made the SEC primary-document finder tolerant of alternate official filing
  index table layouts. If a valid primary-document link cannot be found there,
  it retries via the SEC *complete-submission* SGML document headers, checking
  the actual `<TYPE>` and `<FILENAME>` instead of guessing a report name.
  SEC access block pages are reported more clearly. A failed form remains
  retryable. Reports and saved Item data remain unchanged.
- These changes require no database migration and do not touch your `.env`,
  `.env`, or `../data/`.

## SEC Atom collection: no date cutoff

`extract_recent_filings.bat` / `.sh` now processes **every parseable filing**
returned by the SEC `getcurrent` Atom feed pages for exact 8-K, 10-K, and
10-Q. It no longer filters by the last 24 hours or stops when old records
appear. Paging continues until SEC returns fewer than 100 entries, or the
configured `--max-atom-pages` safety limit (default: 100 **per form**).
`--hours` and `--now` are accepted for backward compatibility but ignored.

Example: `./extract_recent_filings.bat --max-atom-pages 100` on Windows.
The per-page log now reports `returned`, `valid`, `new`, `already_seen`, and
`excluded`, not `in_window`. Existing SEC filing index URLs remain uniquely deduplicated in SQLite.
The existing business rules still apply: only exact SEC form types and
selected 8-K Item codes are stored; this changes **only the date filter**.

**Scope limitation:** `getcurrent` does not expose every historical SEC
submission. This change processes all entries retrievable by paginating that
endpoint, not the entire EDGAR archive. A pagination limit or SEC feed cap
may prevent exhaustive retrieval; a warning is logged rather than claiming
complete history.

## Exactly five processing commands

| Windows (PowerShell) | Linux / cron | What it does |
|---|---|---|
| `./extract_recent_filings.bat` | `./extract_recent_filings.sh` | Incremental SEC Atom metadata for exact 8-K, 10-K and 10-Q |
| `./extract_forms_items.bat --limit 0` | `./extract_forms_items.sh --limit 0` | Reuse/download report files and parse Items |
| `./extract_news.bat --batch-size 500` | `./extract_news.sh --batch-size 500` | Last-3h RSS collection, URL dedup, **two AI batches** (grouping then company/reason extraction), followed by catalog identity resolution |
| `./run_ai_sec_analys.bat --limit 0` | `./run_ai_sec_analys.sh --limit 0` | Analyse pending SEC Items using the OpenAI API |
| `./run_ai_news_analys.bat --limit 0` | `./run_ai_news_analys.sh --limit 0` | **Batch AI** impact/confidence/monetary/one-week scenarios for news/entity relationships |

On Windows, use `*.bat` (PowerShell syntax ` .\extract_news.bat `, etc.).
`dash.bat` and `dash.sh` start the **viewer**, not a sixth processing job.
`setup.bat` and `setup.sh` install/setup prerequisites, not scheduled jobs.
Every root-level `.bat` has a same-named `.sh` counterpart, including the
`dash`, `setup` and `python_runtime` helpers. Linux scripts reuse installed
Python or an already-existing `.venv` and **do not create a new environment**.
Obsolete processing aliases (`analyse_items`, `run_ai_analysis`,
`run_ai_analys`) and old Windows Task Scheduler installers are not included.
If replacing files inside an existing installation, manually delete any old
launchers left there by a previous ZIP extraction; ZIP extraction does not
remove obsolete local files.

## Installing safely

1. Stop running Dash and collectors.
2. Extract the ZIP as `atlas/main_back_end/` plus sibling `atlas/data/`.
   Do not overwrite/mix code and persistent data folders.
3. **Move** the contents of your backed-up old `data/` into `atlas/data/` (database and reports),
   `.env` — this private file is intentionally **not in the ZIP**.
4. Run `setup.bat` (Windows) or `./setup.sh` (Linux) when dependencies require setup.
5. Set `OPENAI_API_KEY` in your private `.env` file (see `ENV_CONFIGURATION.md`).
   Set `SEC_USER_AGENT` to your correct contact in `.env` for SEC access.
6. Launch Dash with `dash.bat` or `./dash.sh`.

Use `--dry-run` with either standalone AI command to check work without paid
calls. `--limit 0` means **unlimited pending** and may incur API charges.
OpenAI requests are never made automatically when extracting SEC Items.
News **event-deduplication** does call OpenAI as part of `extract_news` unless
`--no-ai` is passed. News **impact scoring** is a separate fifth command.

### News stages

1. `extract_news` reads `assets/news_sources.csv` (43 configured feeds).
2. It retains only news published during the strict previous three hours and
   skips existing normalized URLs against the entire SQLite `news` table.
3. OpenAI compares up to 500 article records per request, groups the same
   factual event, picks the best main article, links duplicate articles via
   `news.main_news_id`, and marks duplicate coverage `skipped`.
4. **Only main articles** receive a second AI request to extract `ID|Company|Reason`
   from the context, including substantiated exposure from curated evidence.
   Exact catalog matching and a proper-name/evidence gate run only after AI.
   The valid results are saved with reasons; no generic-word name scanning.
5. `run_ai_news_analys` scores the **linked main news/entity pairs** (default
   batch size 20), requiring a numeric value and explanation for each AI field.
   Updates are atomic per request and remain idempotent across reruns. A lone
   relation is deferred until there is another one; no per-article call is made.
6. Main news changes to `analysed` when **all its linked relationships** have
   succeeded. Reports grouped as duplicates remain in SQLite with main links.

News scoring examples:

```powershell
.\run_ai_news_analys.bat --dry-run
.\run_ai_news_analys.bat --limit 0 --batch-size 20
.\run_ai_news_analys.bat --limit 0 --retry-errors
```

The news scoring contract and its text live in
`news_pipeline/scoring.py` and `news_pipeline/NEWS_SCORING_PROMPT.md`.
SEC Item scoring remains in `ai/` and is independent of news scoring.
Neither pipeline treats zero monetary effect as proof of no effect; when the
amount is unquantifiable, 0 is a placeholder with an explicit explanation.
One-week % estimates are speculative scenarios, not actual measured returns.

## Linux cron — prompts for five independent intervals

```bash
chmod +x *.sh scripts/run_python.sh
./install_cron.sh
# asks each: every N hours? [1]
# Enter means every 1 hour. Enter 0 to disable that job.
crontab -l
# To remove only this project's entries:
./remove_cron.sh
```

`install_cron.sh` installs the five selected jobs into your active user crontab,
**without touching other jobs**. It also replaces old SEC EDGAR cron lines and
avoids creating duplicates on repeated installation. A newly installed schedule
starts on its next cron minute (not immediately). Hourly minute offsets are
0, 10, 20, 30, 40 for metadata, Items, News, SEC AI, and News AI.

Any positive interval in hours (e.g. 5, 7, 25) is supported. `cron_job.sh` runs
hourly and checks elapsed time since each job's last successful completion,
rather than using `*/N` hour fields that silently reset every day. Jobs share
`../data/cron_jobs.lock` via `flock` so they do not overlap; a busy job is skipped
and considered again the following hour. A failed job is eligible for retry
at the next hourly check. Logs and last-success stamps live in `../data/logs/` and `../data/cron_state/`.

**Requirements:** Linux/Unix with bash, `crontab`, `flock` (`util-linux`),
a running cron service, Python/dependencies installed (`./setup.sh`). No cron
schedule is installed automatically by setup. Enabling AI jobs can incur API
charges; choose `0` for jobs you do not want to schedule.
Windows `.bat` scripts are **not** scheduled by Linux cron.

## Dashboard

Run `dash.bat` (or `./dash.sh`). Left navigation: **Companies → Forms → Items
→ News**. Select any SEC Item or company-linked main news article to see full
stored text (or labeled RSS summary fallback), source link, and AI scores with
each explanation beside its number.

Compact top counts are explicitly labeled: total SEC companies, forms,
Items, analysed Items, all news, news published in the last 3 hours, news
statuses in the last three hours, all-time main and duplicate news counts,
news-linked SEC companies, and relationship count. The SEC form-type count
cards show total and parsed form counts. Company sorting supports latest SEC
filing, latest linked news, and alphabetical name.

## Files and modules

```
sec_edgar/       SEC Atom metadata, report downloads, parsing, SQLite core
news_pipeline/   RSS, URL dedup, batch AI event grouping, matching, scoring
ai/             OpenAI client and SEC Item scoring
shared/         Shared validated AI field contract
dashboard/      Query helpers, news view and reusable metric cards
app.py          Dash callbacks and layout
assets/         RSS feed config and dashboard CSS; API keys are in .env only
scripts/        Shared Linux Python launcher
install_cron.sh, cron_job.sh, remove_cron.sh     Linux scheduler
```

The production `../data/` database and API key are excluded from distribution. Tests use
mocked AI responses; a successful automated test is not a guarantee about
Windows browser rendering or OpenAI service availability.

---

## v10 schema migration and raw source archives

This distribution uses numeric `id` primary keys in SEC companies/forms/items,
`company_id`/`form_id` relationships, one UTC `filed_date`, and per-subject
write-once raw SEC HTML/XML and news RSS/article archives. Opening an existing
v9 five-table DB performs an online backup and atomic migration before use.
See [docs/SCHEMA_V10.md](docs/SCHEMA_V10.md) for field removals, archival layout,
consequences of dropping `part`, examples, and known compatibility limits.

## Test migration status

Run `python -m pytest -q` from `atlas/main_back_end/` for the maintained v10+/v11
regression suite. Historic pre-v10 test fixtures are preserved but isolated in
`tests_legacy_pre_v10/`, because they call schema fields and matching functions
retired in the v10 migration. They have **not** been ported or claimed to pass.

## v12.2 seven-day company analytics

The **Companies** page now lists `acumm_impact`, `acumm_monetary` (USD), and
`accum_confdent` for each company. Open one company to see all analyzed News
relationships and SEC Items within the last seven days, including impact,
confidence, financial effect, 7-day scenario, AI reason/evidence, connection
reason, age weight, and the original sources. Only scores with `analysed`
status contribute. The formula, duplicates/zero limitations, manual refresh
command, and migration backup procedure are described in
`docs/COMPANY_7_DAY_ANALYTICS.md`. The Data Quality page and status filters
show status strings from actual code, not human-invented processing states.

## Per-batch company connection report and exact AI request capture (v12.3)

Each successful `extract_news.sh` / `.bat` AI batch now logs a concise second line:

```text
AI CONNECTIONS [1] ai_news_with_companies=4 extracted_companies=6 resolved_companies=5 connected_news=3 connected_relations=5
```

All numbers refer to **that batch**, not lifetime totals. `ai_news_with_companies`
means articles for which the AI proposed a company; `extracted_companies` means
AI-proposed pairs. `resolved_companies` are identities passing the deterministic
and contextual checks. `connected_news` counts distinct articles with committed
company relationships; `connected_relations` counts those company links in SQLite.
When a batch fails, an explicit log line says no new connections were committed.
The final `ai` JSON includes cumulative values for these counts.

The exact static instructions sent to the news grouping and company extraction
models are in `news_pipeline/BATCH_DEDUP_PROMPT.md` and
`news_pipeline/COMPANY_EXTRACTION_PROMPT.md`. To save the **full actual request**
(`model`, complete `instructions`, full article `input`, response format) for
each request during a run, use:

```bash
./extract_news.sh --ai-only --batch-size 20 --save-ai-requests
```

Captured requests are placed in `atlas/data/ai_requests/`, even when an OpenAI
request times out. They may include article text and supplied exposure data;
handle these files as internal source material. No API key is recorded. Capturing
is off by default and does not change correlation or AI status logic.

## Alpaca 15-minute price pipeline (additive)

The `price_pipeline/` package downloads *observed* US-equity 15-minute OHLCV
bars into `price_bars`, linked via `company_id` to `companies.id`. The
launchers `extract_prices.bat` and `extract_prices.sh` backfill up to one year
for new symbols and scan the latest 30 days for gaps on subsequent runs,
prioritizing saved cumulative-impact rankings. All changes are additive to
the existing SEC/news data. Setup, Alpaca credentials, arguments, data-quality
limitations and Android endpoints: [price_pipeline/README.md](price_pipeline/README.md).

## Centralized runtime configuration

All active API credentials and operational settings are documented in `.env.example` and `ENV_CONFIGURATION.md`. Copy `.env.example` to `.env`, move the old OpenAI key from `assets/key.txt` to `OPENAI_API_KEY`, and keep `.env` private (`chmod 600 .env`). Both AI scoring launchers and cron use defaults from `.env`; CLI options can override defaults for one run. JSON SEC catalog archives now have distinct `.meta.json` sidecars to prevent the previous FileExistsError.
