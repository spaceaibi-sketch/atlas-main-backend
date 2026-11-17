# Massive 15-minute price pipeline — threaded/vectorized update

## Commands

From the existing backend project root, after replacing the files in this ZIP:

```bash
# From the project root (where backend.sh and main_back_end/ live):
bash backend.sh update --with-cron
bash backend.sh status

# Or update cron separately without reinstalling packages/services:
bash backend.sh cron-install
bash backend.sh status

# Manual Massive run (e.g. four network workers, last seven calendar days):
cd main_back_end
bash extract_massive_prices.sh -thread 4 --last 7

# Optional: fail the command if possible interior session gaps remain:
bash extract_massive_prices.sh -thread 4 --last 7 --strict-gaps
```

Four workers are the default for Massive even if the existing private `.env`
has no `MASSIVE_THREADS` key. Override with `-thread 1` for sequential fetches.

Cron installer registers six owned schedules and preserves unrelated entries.
The new entry is `extract_massive_prices` at minute 50 of each hour, gated by
`ATLAS_CRON_MASSIVE_PRICES_HOURS` (defaults to 1, 0 disables).
For existing private `main_back_end/.env`, optionally add:

```ini
MASSIVE_THREADS=4
ATLAS_CRON_MASSIVE_PRICES_HOURS=1
# Shared API pacing across all worker sessions; do NOT divide by threads.
MASSIVE_MIN_INTERVAL_SECONDS=12.5
```

Do not overwrite the private `.env` file with `.env.example` or add credentials
to cron entries. The cron wrapper uses the normal Python configuration loader.
`backend.sh update` installs the new `exchange-calendars` dependency.

## Data integrity

- Provider aggregate pages are converted/filtered as pandas columns.
- Parallel HTTP fetch workers never touch SQLite. The Massive pipeline has
  **one dedicated SQLite writer thread** and a bounded in-memory work queue.
  The writer uses the existing atomic staging merge, and checks that all
  accepted bar keys are present before acknowledging the batch.
- Every planned company/period is durably recorded before network fetch.
  Every fetched response (including a session gap-repair response) is saved to
  an atomic, fsynced, checksum-validated disk spool before DB insertion.
  Failed, interrupted, or database-locked work remains on disk and is retried
  automatically on the next run, using the ORIGINAL requested period.
  Replays never overwrite already inserted OHLC data.
- A filesystem lock prevents two Massive price runs sharing a DB from
  running simultaneously. Other SEC/news/AI processes can still hold SQLite
  locks; the single price writer waits, retries, then exits nonzero with work
  preserved on disk if it still cannot write.
- For Massive, invalid optional volume/trade-count/VWAP cannot reject a valid
  OHLC bar; unavailable volume is persisted as zero only because the legacy
  `price_bars` column is `NOT NULL`. Timestamp alignment and OHLC price
  consistency remain strict. Duplicates keep the first value.
- Persisted prices are never overwritten by a later provider response.
- For Massive, completed/published XNYS regular-session slots *between observed
  bars in the same session* are checked. A single follow-up Massive request
  attempts to fill detected interior gaps; unresolved holes are logged/reported.
- This is **not** a guarantee every expected interval has a trade: symbols can
  legitimately have no trades or be halted, the free tier is delayed, and
  historical lookback outside `--last` is not audited. No synthetic prices.
- `--strict-gaps` returns a nonzero exit code when checked gaps remain.
  `--no-gap-repair` avoids the extra request, and can be used with
  `--strict-gaps` to check without backfilling.
- Four threads mean four concurrent HTTP fetch workers, **not** four parallel
  writers; they share Massive request-start pacing, with global 429 cooldown.
- Every interrupted/failed Massive job retains a disk-spooled record;
  `pending_durable=N` in the logs is the number still to be recovered.
  Run exit code is nonzero when any job remains. The next run automatically
  replays the backlog *before* newly requested symbols (even with `--symbol`).
- A missing provider response, an invalid OHLC row, permanent media failure,
  and external deletion of already committed rows are outside the guarantees of
  this queue. Keep regular DB + spool backups. Never delete the spool manually.

## Deployment warning

Run `backend.sh` as the account that owns the backend/crontab, not through
`sudo bash backend.sh` from another account. No services, crontabs, databases,
or `.env` files are modified by delivering this update ZIP: execute installer
commands above on your server when ready.

## Hourly cron cadence (follow-up fix)

The hourly default is now `ATLAS_CRON_MASSIVE_PRICES_HOURS=1`.
The cron wrapper stores the successful run's **start timestamp** and compares
whole wall-clock-hour slots so a job completing at 50:30 does not prevent the
following 50:00 hourly run. Runs are best-effort: the shared project `flock`
prevents concurrent jobs and can skip a tick if another Atlas job holds the lock.
Existing private `.env` files are never modified by an update. If your `.env`
contains `ATLAS_CRON_MASSIVE_PRICES_HOURS=24`, set it to `1` manually, then run
`bash backend.sh cron-install` as the cron-owning user to refresh the entries.

## Massive single-writer, crash-recovery settings

All options are optional; defaults are shown below. Put overrides in the
existing private `.env` if necessary, without replacing it:

```ini
MASSIVE_THREADS=7
ATLAS_SQLITE_BUSY_TIMEOUT_MS=60000
ATLAS_PRICE_LOCK_RETRY_ATTEMPTS=8
ATLAS_PRICE_LOCK_RETRY_SECONDS=2
```

Spool path for `/opt/atlas/data/edgar.sqlite3`:

`/opt/atlas/data/edgar.sqlite3.massive_price_spool/`

The spool must be on persistent storage with write permissions for the Atlas
account. Before each run the script enumerates any unfinished original
requests, then records the new requests. Logs show `recovered`, `planned`,
`completed`, and `pending_durable`. If a process crashes after a SQLite commit
but before clearing a spool item, replay is safe because the SQLite primary key
prevents duplicate OHLC inserts. If a write stays locked beyond all retries,
the run fails without discarding its staged work; the next scheduled/manual
run retries it. Do not remove SQLite's `-wal` / `-shm` files or disable other
Atlas jobs to work around lock errors.

For a targeted verification:

```bash
cd /opt/atlas/main_back_end
bash extract_massive_prices.sh --symbol AAPL --last 7 --no-gap-repair --threads 7
```

Look for `[PRICE QUEUE] single_writer=1`, a positive `bars_saved` for new bars,
`missing_after_merge=0`, and `pending_durable=0`. No SQLite migration needed.
