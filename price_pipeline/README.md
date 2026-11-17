# Atlas 15-minute price collector — Alpaca / yfinance / Massive

Three **separate provider modules** supply a shared CLI and SQLite storage:

- `price_pipeline/alpaca.py` — authenticated Alpaca 15-minute bars (`sip` / `iex`).
- `price_pipeline/yfinance.py` — Yahoo Finance `yfinance` 15-minute bars (no API key).
- `price_pipeline/massive.py` — Massive 15-minute US stock aggregates over authenticated REST, with pagination, rate limits, retries, and UTC conversion.
- `price_pipeline/collector.py` — activity-prioritized company selection and time windows.
- `price_pipeline/storage.py` — UTC validation, insert-only persistence, CSV export and safe one-time feed-constraint migration.
- `price_pipeline/cli.py` — shared flags and provider selection.

## Install

```bash
cd /opt/atlas/main_back_end
python -m pip install -r requirements.txt
```

The launcher uses the project's configured Python runtime. Set `SEC_DB_PATH` in `.env` as before. Set `ALPACA_API_KEY` and `ALPACA_API_SECRET` **only for Alpaca**. Optionally set `PRICE_SOURCE=alpaca` (default) or `PRICE_SOURCE=yfinance` or `PRICE_SOURCE=massive`, plus provider-specific settings.

## Commands

```bash
./extract_prices.sh --source massive --last 7 --symbol AAPL
./extract_prices.sh --source massive --last 7 --limit 100
./extract_prices.sh --source massive --last 7 --dry-run --limit 100
./extract_prices.sh --source yfinance --last 7 --limit 100
./extract_prices.sh --source yfinance --last 15 --symbol AAPL
./extract_prices.sh --source alpaca --feed sip --last 30 --limit 100
./extract_prices.sh --source alpaca --feed iex --last 7 --symbol AAPL
./extract_prices.sh --source yfinance --last 5 --dry-run --limit 20
./extract_prices.sh --last 7        # Uses PRICE_SOURCE in .env (otherwise Alpaca)
```

Windows use `extract_prices.bat` with the same flags. Or use `python -m price_pipeline.cli`.

**`--last N`** means `[current UTC minus N calendar days, current UTC)`; it re-fetches all 15-minute bars in that lookback period. Existing `(company_id, bar_time_utc)` records are **never overwritten** (`ON CONFLICT DO NOTHING`). This can fill missing bars within the requested lookback. It is not a "last N candles" flag and does not fabricate closed-session bars. `N` must be a positive integer. `--last` is required. The collector does not scan existing bars to choose its request period.

**Yahoo constraint:** Yahoo/yfinance offers 15-minute intraday bars for **up to roughly 60 days**. `--source yfinance --last N` rejects N > 60. Without `--last`, a stale checkpoint older than that limit is clamped to the last 60 days; older history is not recoverable from this provider. Yahoo may reject dates right on the 60-day boundary due to server timing. Pick a slightly shorter window if it does. Alpaca has its separate vendor-specific history and feed restrictions.

**Provider settings:** Alpaca asks for `ALPACA_API_KEY`, `ALPACA_API_SECRET`, and optional `--feed sip|iex`. `yfinance` does not need these, and `--feed` is rejected when using yfinance. All three providers request **15-minute** interval data with raw (unadjusted) OHLC. Yahoo requests regular market hours (`prepost=False`); Alpaca continues with its existing feed behavior. Yahoo's standard history does not supply trade count or VWAP: these remain **SQL NULL**, never estimated.

## Company prioritization

1. Compute each company's **latest known news update or filing update** (maximum of `news.published_utc`, `news.updated_at`, `forms.filed_date`, `forms.updated_at`). News must have a linked `news_company_relations.company_id`.
2. Sort newest activity timestamp **first**.
3. For companies with identical/no activity, retain the old descending absolute `acumm_impact` ranking, then ascending company ID.
4. Apply `--limit` **after** sorting. `--symbol` always filters the selected ticker.

Tables not yet created are handled gracefully. Company priority is based on recorded timestamps, not inferred price movements. The CLI reports `latest_activity` with each company.

## SQLite change on first yfinance use

Existing `price_bars.feed` only permits `sip` and `iex`. To label yfinance accurately, the first non-dry-run yfinance run:

1. Makes an **online SQLite backup** of the original database at `<database>.before_yfinance_feed_v1.bak` (never overwrites that file).
2. Within a single transaction, rebuilds only `price_bars` with `CHECK(feed IN ('sip','iex','yfinance'))`, copying all prior price rows and recreating the known index. It refuses unknown price columns or custom price indexes/triggers.
3. Stores `source='yfinance',feed='yfinance'` for new yfinance records; existing Alpaca rows keep their original values.

The database and backup directory must be writable by the extraction process. Run the migration when other applications are not busy writing SQLite. This is a schema change; back up your live database separately as an operational precaution.

The primary key is still `(company_id,bar_time_utc)`: the **first source to insert a candle wins** if both sources have the same timestamp. Later runs do not switch or overwrite that candle's price, source, or feed. CSV exports include both `feed` and `source`.

## Verification

```bash
python -m pytest tests/test_price_pipeline.py tests/test_price_sources.py -q
```

Source and storage logic are mock-tested. Access to Yahoo and Alpaca services requires live connectivity and is not simulated by those tests.

## Massive provider configuration (no new SQLite migration)

Massive (formerly Polygon) requires an **API key value**, which is different
from the key identifier in your dashboard URL. Do **not** paste the dashboard
URL into `.env`, include a key in source files, or expose it to Android.

Add these settings privately to `/opt/atlas/main_back_end/.env`:

```dotenv
PRICE_SOURCE=massive
MASSIVE_API_KEY=YOUR_ACTUAL_MASSIVE_API_KEY
MASSIVE_MIN_INTERVAL_SECONDS=12.5
```

The default 12.5-second minimum interval is safe for the **Stocks Basic**
5-request/minute limit. Paid plans can lower it in their private configuration
if their limit permits. HTTP 429/5xx and network errors have bounded retries;
401/403 stops collection with a credential/entitlement error. On the free
Stocks Basic plan, **current-day bars are not available until after midnight
US Eastern**; higher plans vary between 15-minute delayed and real-time.
The app cannot make stale prices real-time through configuration.

Massive custom bars use
`GET /v2/aggs/ticker/{ticker}/range/15/minute/{from}/{to}`
with `adjusted=false`, `sort=asc`, `limit=50000`; the reader handles pagination
and transforms the vendor's millisecond timestamps to UTC. Source is stored as
`source='massive',feed='sip'` to represent consolidated US aggregates **without
rebuilding your existing `price_bars` table or dropping its 19 update triggers**.
Raw records are inserted only when the `(company_id,bar_time_utc)` key is new.
Current prices, 24h and 7d changes in `company_mobile_summary` are refreshed
by the already-installed price triggers. No Android or Java changes are needed.

First, dry-run the company selection, then read a single symbol:

```bash
cd /opt/atlas/main_back_end
./extract_prices.sh --source massive --last 7 --symbol AAPL --dry-run
./extract_prices.sh --source massive --last 7 --symbol AAPL
```

If the reader is to be used by a scheduled job, set `PRICE_SOURCE=massive`
in the private `.env`; choose a supported lookback in the existing invocation.
**No ingestion or scheduler changes happen until the updated files are
deployed and Massive is explicitly selected.**

This integration is validated with mocked API responses, not a live account.

```bash
python -m pytest -q tests/test_massive_prices.py tests/test_mobile_impact_writer_migration.py
```

## Dedicated Massive all-symbol launcher

Use these scripts to download prices for **every nonblank ticker in the existing
`companies` SQLite table**. They do not read an external watchlist, arbitrarily
limit the number of companies, or depend on the `PRICE_SOURCE` setting:

```bash
cd /opt/atlas/main_back_end
./extract_massive_prices.sh --dry-run   # show entire selected company list, no API key needed
./extract_massive_prices.sh             # ALL tickers, last 7 days by default
./extract_massive_prices.sh --last 30   # ALL tickers, last 30 calendar days
```

Windows: `extract_massive_prices.bat` with the same arguments. For a test only,
add `--symbol AAPL` or `--limit 10`; **omit those flags for the full list**.
`--db /path/to/edgar.sqlite3` can explicitly choose a database if necessary.

This dedicated command forces the Massive provider, while still using the
shared rate-limited Massive client, activity-prioritized company selection,
insert-only `price_bars` storage, and mobile price-update triggers. Bars
already stored by another provider are preserved (first source wins), so
switching to Massive does not replace old OHLC values at duplicate timestamps.
It prints each selected ticker and final counts. Keep `MASSIVE_API_KEY` only in
the private `.env`. On a free tier, the default request pacing is conservative
(12.5 seconds between API calls) and a large list may take substantial time.
Use `--dry-run` to verify which company list/database will be processed.
