# Atlas main backend — command and module map

This is the complete **main_back_end** source directory, not a file overlay. Runtime credentials and adjustable job defaults are read from `.env` (see `ENV_CONFIGURATION.md`). It includes News collection, News Phase 1 company extraction, News Phase 2 scoring, SEC collection/parsing, SEC Item scoring, price collection, shared helpers, dashboard, scripts and tests.

## Canonical commands

| Job | Preferred shell command | Python module | Unit per OpenAI batch |
|---|---|---|---|
| Collect SEC filings | `./extract_recent_filings.sh` | `sec_edgar.windows_launcher` dispatch | None |
| Parse SEC Items | `./extract_forms_items.sh` | `sec_edgar.windows_launcher` dispatch | None |
| Collect/Phase 1 News | `./extract_news.sh` | `news_pipeline.cli` | News articles, separate stage |
| Score SEC Items | **`./run_ai_sec_scoring.sh`** | **`ai.scoring_cli`** | **20 Items** |
| Score News relationships | **`./run_ai_news_scoring.sh`** | **`news_pipeline.scoring_cli`** | **20 News–company relationships** |
| Collect prices | `./extract_prices.sh` | `price_pipeline.cli` | None |

The former `run_ai_sec_analys.sh` and `run_ai_news_analys.sh` names remain as compatibility wrappers, including their Windows `.bat` counterparts. `python -m ai.cli` also remains supported; `python -m ai.scoring_cli` is its consistent new name. These wrappers all use the same underlying workers and do not create duplicate pipelines.

`ai/worker.py` performs SEC Item scoring. `news_pipeline/scoring.py` performs News-company scoring. Both reuse `shared/ai_scoring.py`, `shared/ai_transport.py`, `shared/ai_errors.py`, and `shared/aggregate_refresh.py` but retain distinct prompts, database writes, failure and status rules.

## Commands

```bash
cd /opt/atlas/main_back_end
./run_ai_sec_scoring.sh --limit 100 --retry-errors
./run_ai_news_scoring.sh --limit 100
```

Both use `.env` defaults (`SEC_AI_BATCH_SIZE=20` and `NEWS_SCORING_BATCH_SIZE=20`). To override for a single run, append `--batch-size 5`. The shell launchers do not hardcode batch sizes.

In SEC batch mode, one HTTP API response is logged as up to twenty individual `[n/100] ANALYSED` or `AI FAILED` results. Those lines refer to database **Items**, not HTTP requests.

SEC evidence that is not a literal passage of the provided Item text triggers one additional single-Item assessment with stronger quotation instructions. Only that Item incurs the extra request. If invalid again, it remains `error` for an explicit `--retry-errors` run. Other scores are saved. The worker still requires a genuine source quote (or `Not stated`) and never accepts fabricated evidence by dropping the validation.

## Cron management

Existing installed scripts have been updated:

```bash
cd /opt/atlas/main_back_end
bash remove_cron.sh           # remove this project's tagged cron jobs only
bash install_cron.sh          # interactively choose each of five intervals
# or
bash install_cron.sh --defaults  # install all five hourly, noninteractive
crontab -l
```

`install_cron.sh` replaces old `run_ai_sec_analys` and `run_ai_news_analys` **tagged** entries with canonical `run_ai_sec_scoring` and `run_ai_news_scoring`. It does not remove untagged/unrelated cron jobs; `remove_cron.sh` has the same limitation by design. Each cron run uses the new shell wrapper, so both scoring jobs use batches of 20 by default. The shared `cron_job.sh` lock prevents Atlas cron jobs from overlapping. Old launcher names are still recognized in manually retained legacy schedules.

Only the crontab belonging to the account running these commands is affected. If Atlas cron runs under another Unix account, install/remove under that account instead.

## Full-project deployment

1. Stop the two AI workers and other Atlas writers; back up `/opt/atlas/main_back_end` and `/opt/atlas/data/edgar.sqlite3`.
2. Unpack this full project into a temporary directory.
3. Copy the project into `/opt/atlas/main_back_end` without overwriting your local private `.env` or customized News feeds. The archive deliberately excludes `.env` and contains only `.env.example`. API credentials are now read from `.env`, not `assets/key.txt`.
4. Reinstall Atlas cron jobs with the new script to convert the tagged legacy entries.
5. Run `python3 -m pytest -q tests` and check launchers with `--help`.

Example (after uploading the ZIP to `/opt/atlas`):

```bash
cd /opt/atlas
unzip -q atlas_main_backend_full_env_utc_archive_fix.zip -d atlas_release_tmp
rsync -av \
  --exclude='assets/key.txt' --exclude='.env' \
  --exclude='assets/news_sources.csv' \
  atlas_release_tmp/main_back_end/ main_back_end/
cd main_back_end
bash remove_cron.sh
bash install_cron.sh
python3 -m pytest -q tests
```

The main-backend database stays at `/opt/atlas/data/edgar.sqlite3`; no schema migration is included or required for the scoring/cron changes. No production database or live OpenAI request has been exercised in this packaged test environment.

**Archive scope:** The complete main backend. The Android app and Android API are separate projects and are intentionally not included or modified.
