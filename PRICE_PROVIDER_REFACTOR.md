# Price-source refactor (October 2026)

## Implemented

- `--source alpaca|yfinance` in `price_pipeline.cli`; `.env` default `PRICE_SOURCE=alpaca`.
- `--last DAYS` lookback window; no flag retains the original max-stored-bar incremental behavior.
- Both provider modules return compatible **15-minute** raw OHLCV bars; new Yahoo module uses a timezone-aware UTC index and leaves trade count/VWAP missing, rather than inventing values.
- Prioritize companies by latest stored, linked news and/or SEC filing update; fall back to old absolute-impact ordering and company ID.
- Keep one unique candle per `(company_id, bar_time_utc)`, never overwrite prior Alpaca/Yahoo records.
- Add a backed-up transactional upgrade to let the existing `price_bars.feed` CHECK constraint store `yfinance` correctly. The old file is backed up at `<database>.before_yfinance_feed_v1.bak`.
- Append `source` to CSV exports (existing columns remain in their original order).
- Updated `.env.example`, Python requirements, README, and regression tests.
- Linux launcher executable modes corrected for packaging.

## Test results

`python -m pytest tests -q`: 198 passed. These use mocked vendor data, not live network access. Test your installed yfinance library and internet rate limits in production before scheduling large batch jobs.

## Deployment

Extract the entire updated `main_back_end/` over the matching backend version, retaining your original `.env`, `data/`, and operational databases. Back up the database separately before switching to yfinance the first time. Run `python -m pip install -r requirements.txt` in the runtime used by the shell launcher. See `price_pipeline/README.md` for usage and migration restrictions.
