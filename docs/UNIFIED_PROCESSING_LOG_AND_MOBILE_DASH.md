# Unified processing log and Android-inspired Dash workspace

## Log location and format

All pipeline events append to the **same** file by default:

```
atlas/
  data/
    edgar.sqlite3
    atlas_processing.log    # single structured processing event stream
  main_back_end/
```

`atlas_processing.log` is UTF-8, **one JSON object per line** (JSONL). Each entry includes `timestamp_utc`, `stage`, `action`, `pid`, and identifiers or values relevant to that action. Log writers are implemented centrally in `shared/processing_log.py`, invoked by the individual functions that perform the work. Existing terminal/cron output may still be present; the JSONL file is the single unified *structured processing* log.

To use a different shared file for every pipeline, set `ATLAS_LOG_PATH` to the same absolute path in their environments, for example:

```bash
export ATLAS_LOG_PATH=/srv/atlas/data/atlas_processing.log
```

The log grows as processing runs; it is **not automatically rotated or truncated** because all events must remain in one common file. Monitor free disk space. Use external backup/archive policy when needed. Treat this file as sensitive application telemetry; limit filesystem permissions and never serve it publicly.

### Event families

| `stage` prefix | Recorded information |
| --- | --- |
| `sec.atom` | Feed requests, pages, individual filing candidates, filtering and acceptance, errors, run summary |
| `sec.http`, `sec.document`, `archive` | SEC request status, primary report downloads, document GUID links, immutable archive file and SHA-256 |
| `sec.form`, `sec.filing`, `sec.item`, `sec.company` | Form state, per-Item type, occurrence, database ID, text length, extraction and storage, failure stage, company updates |
| `news.http`, `news.feed`, `news.ingest` | Source requests, individual headline/time freshness decisions, inserted/duplicate/invalid news records |
| `news.ai`, `news.dedup` | Batch input IDs, references, main/duplicate grouping, counts, errors and successful batch commits |
| `news.extraction`, `news.identity`, `news.validation`, `news.correlation`, `news.linking` | Company name proposals with AI reasons, catalog matching, evidence gate accept/reject reasons, saved company correlation and IDs |
| `ai.item`, `news.scoring` | Per-Item and per-relation AI request IDs, model metadata, **all validated metric values and explanation fields**, persistence outcomes, invalid/stale/error decisions |
| `price.collect`, `price.http`, `price.bar` | Per-company time windows, provider page requests, individual accepted/rejected candles (timestamp, OHLC, volume, count, VWAP, feed), exact batch insert count |
| `company.analytics` | Changed company ID and 7-day weighted impact, monetary and confidence aggregates |

**Important distinction:** individual `price.bar` acceptance is not proof of a new insert, because duplicates are intentionally ignored by the storage layer. `price.bar/batch_completed` records the exact saved and duplicate/existing totals. Similarly, `news.linking/relation_proposed` and `news.correlation/main_proposed` are logged before the enclosing batch transaction commits; `news.ai/batch_saved` confirms batch commit.

These events cover **new runs from installation onward**. They do not retrospectively create per-step history for previously processed rows. Original source text belongs in archived response files, saved reports or database Item/News rows, not in log records. No API secrets, credentials, HTTP request headers, full AI prompt bodies, or full report/article text are written to the structured log. In the price collector, provider JSON must already be parsed record-by-record because it is heterogeneous/untrusted; row-level logging is intentionally done at that same validation boundary.

### Inspect entries

```bash
# Watch processing in real time
tail -f ../data/atlas_processing.log

# Extract just item assessment results
python - <<'PY'
import json
from pathlib import Path
for line in Path('../data/atlas_processing.log').open(encoding='utf-8'):
    event = json.loads(line)
    if event['stage'] == 'ai.item' and event['action'] in ('saved','manual_saved'):
        print(event)
PY
```

## Dash company workspace

Run the existing `dash.sh` or `dash.bat`, then open:

`http://127.0.0.1:8050/mobile`

The **Atlas app** tab in the main navigation opens this page. It is modeled on the supplied Android UI, using server-side SQLite queries rather than relying on the Android app's separate `/api/v1` server:

- Searchable, sortable company directory with pagination and SEC ticker/CIK identity.
- Browser-local watchlist/favorite toggle. Stored only in that browser; it does not synchronize with the Android app's device-local favorites.
- Browser-local light/dark toggle, responsive layouts.
- Company workspace tabs for **Overview, News, SEC Items, SEC Forms** with working drill-down and source links.
- Seven-day stored company aggregates, scored record counts, AI assessment values and their full reasoning, exact news correlation explanation.
- Real **stored** 15-minute close-price chart and seven-day news/SEC Item impact markers on a **separate** Y axis, with Plotly zoom/range slider. No guessed candle data.
- News and Item lists ordered by time, absolute impact or absolute monetary estimate, paged at 40 records.
- Original extracted SEC Item text and saved news article/summary in detail views.

**Differences from Android:** The supplied backend database lacks the Android API's company description field, so this UI explicitly shows an unavailable notice rather than inventing one. Local browser favorites are not shared with Android. The Android account/subscription screens, API authentication, language-selector settings and native offline cache are outside this SQLite-backed Dash implementation. Existing Dash global explorers and explicit Item AI actions remain available in their prior pages.

**Data Quality is unchanged.** The `dashboard/data_quality.py` source and existing `app.py` page section and quality callbacks are preserved; CSS for the new screen is appended and namespaced under `.atlas-mobile`.

## Validation

Use:

```bash
python -m pytest -q tests
```

The uploaded baseline already contains one stale structural test (`test_dashboard_lineage.py::test_dashboard_scope_and_sections_wired`) expecting an older `lineage-window` element absent from its existing `app.py`. No change to Data Quality was made merely to satisfy this unrelated expectation. The new logging / mobile tests are in `tests/test_mobile_and_processing_log.py`. Browser-based Dash verification requires a local Dash installation and a populated database.
