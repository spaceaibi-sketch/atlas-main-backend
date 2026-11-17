# Standalone Atlas Massive WebSocket → 15-minute price bars

**Completely separate service**: no modifications to Atlas REST/Java API,
Android, `backend.sh`, crontab, existing configuration templates or private
`.env`, SEC schema, existing price extraction, or deployment scripts.

## Exact feed and timing

This service connects to Massive's **stock minute aggregate** WebSocket feed
and subscribes to `AM.*` (all stock tickers, no watchlist). It consumes *all
Massive-published, eligible-trade one-minute OHLC aggregates*, **not raw
individual trades or quotes**. This is deliberate: raw trade conditions would
require exchange eligibility processing to match Massive's official bars.

It groups each ticker's received minute aggregates into UTC-aligned 15-minute
windows (`00, 15, 30, 45`). For each window:

- **open** = first available minute's open;
- **high** = max minute high;
- **low** = min minute low;
- **close** = last available minute's close;
- **volume** = sum of reported minute volumes;
- **VWAP** = volume-weighted average of reported minute VWAPs if available;
- **trade_count** = NULL (AM minute feed does not provide the trade count).

This never invents missing no-trade minute prices. WebSocket interruptions
invalidate affected intervals instead of pretending a partial bar is complete.
The first, partially observed interval after a connection is dropped. There is
**no REST backfill** by this service, and WebSocket delivery is not guaranteed
during connection loss. Retain the historical REST extractor if you need
long-term gap repair, but the first inserted bar wins on the existing unique
`(company_id, bar_time_utc)` key. A later REST bar will not overwrite the WS bar.

The default WebSocket is `wss://delayed.massive.com/stocks`, with a 15-minute
provider delay and an additional configurable 90-second closing grace. To use
the real-time feed on an entitled plan, set `MASSIVE_WS_FEED=realtime` (or pass
`--feed realtime`). The service will finalize live 15-minute windows after the
closing grace. It requires Massive **WebSocket minute aggregate** entitlement;
paid plan access differs by plan. The Basic free tier has no WebSocket access.

Price bars are stored **only for symbols matched against existing**
`companies.ticker` rows. Unknown market symbols are counted and skipped, not
used to create companies or modify schema.

## Files and persistent state

- `price_pipeline/massive_ws_service.py` — WebSocket client, auth, reconnect,
  subscription, graceful shutdown and scheduling.
- `price_pipeline/massive_ws_store.py` — private durable spool and insert-only
  SQLite writer.
- `start_massive_ws.sh` — new dedicated launcher.
- `requirements-massive-ws.txt` — additional WebSocket Python dependency only.
- `systemd/atlas-massive-ws.service` — **new service unit**; not installed
  automatically.
- `tests/test_massive_ws_service.py` — offline tests.

Spool default: `/opt/atlas/data/edgar.sqlite3.massive_ws_spool.sqlite3`.
Only this *separate spool SQLite* gets its own WAL; the existing
`/opt/atlas/data/edgar.sqlite3` has no journal-mode changes or schema changes.
Live inserts use `ON CONFLICT DO NOTHING`, so your existing triggers run only
for new bars. In the presence of SQLite locks, pending bars remain on disk
and are retried. A crash between target DB commit and spool ACK is also safe:
retries skip existing keys. Because inputs are buffered for up to one second,
a process crash can still lose some not-yet-spooled updates; on reconnection
the affected 15-minute window is invalidated.

## Install only this new service (Ubuntu)

Extract the update-only ZIP over `/opt/atlas` from its parent; it adds the
new files under `main_back_end/`. Do not extract or run any backend installer.

The live backend package directory is used by `scripts/run_python.sh`.
Install only this additional package into that existing package directory:

```bash
cd /opt/atlas/main_back_end
sudo python3 -m pip install --target \
  /opt/atlas/runtime/build/backend/python-packages \
  -r requirements-massive-ws.txt
```

Make sure the **existing private** `/opt/atlas/main_back_end/.env` contains
`MASSIVE_API_KEY=<your existing key>`; do not upload or replace that file.
The service reads the existing `SEC_DB_PATH` from that same `.env` and defaults
to `/opt/atlas/data/edgar.sqlite3`. Optional entries (append manually only if
needed):

```dotenv
MASSIVE_WS_FEED=delayed
MASSIVE_WS_GRACE_SECONDS=90
MASSIVE_WS_SPOOL_PATH=/opt/atlas/data/edgar.sqlite3.massive_ws_spool.sqlite3
```

Install and start **only this unit**:

```bash
sudo install -m 0644 \
  /opt/atlas/main_back_end/systemd/atlas-massive-ws.service \
  /etc/systemd/system/atlas-massive-ws.service
sudo systemctl daemon-reload
sudo systemctl enable --now atlas-massive-ws.service
```

Check it:

```bash
sudo systemctl status atlas-massive-ws.service --no-pager
sudo journalctl -u atlas-massive-ws.service -n 60 --no-pager
```

Look for `subscribed AM.* feed=delayed`, heartbeat lines, and
`committed ... inserted=...` once windows mature during exchange activity.
Do not expect full bars immediately after the service starts or while the
market is closed. To stop only the new service:

```bash
sudo systemctl stop atlas-massive-ws.service
```

To verify stored 15-minute bars without changing data:

```bash
sqlite3 -readonly /opt/atlas/data/edgar.sqlite3 \
  "SELECT bar_time_utc, COUNT(*) n FROM price_bars \
   WHERE source='massive' GROUP BY bar_time_utc ORDER BY bar_time_utc DESC LIMIT 8;"
```

## Testing

```bash
cd /opt/atlas/main_back_end
python3 -m pytest -q tests/test_massive_ws_service.py
```

The tests cover the handshake sequence, minute validation, 15-minute
open/high/low/close, VWAP, repeated updates, insert triggers, duplicate
protection, reconnection invalidation, durable restart and DB lock recovery.
No live subscription/test was performed. Test the actual feed under your
Massive plan before treating the service as production-confirmed.
