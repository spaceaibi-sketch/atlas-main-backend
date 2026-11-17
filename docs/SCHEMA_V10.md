> **Atlas v11 additive news fields:** `news.company_reasons_json TEXT NOT NULL DEFAULT '[]'` and `news_company_relations.correlation_reason TEXT NOT NULL DEFAULT ''`. New backups, AI-first routing and sibling `atlas/data` folder are documented in `AI_FIRST_CORRELATION_AND_WORKFLOW_AUDIT.md`. The historical material below describes the v10 SEC-key migration.

# SEC EDGAR Collector v10 — integer keys and raw HTTP provenance

## Migration summary

The old five-table v9 schema is updated in place by `sec_edgar.database.connect()` or explicitly by `python -m sec_edgar.migrate_v10 --db PATH`. The upgrade creates an SQLite online backup **before** DDL and commits its table-copy changes together. Do not run writers concurrently during an upgrade. The original source snapshot should be retained independently.

- `companies`: add `id INTEGER PRIMARY KEY AUTOINCREMENT`; keep `cik TEXT UNIQUE NOT NULL`; remove `source`.
- `forms`: add `id INTEGER PRIMARY KEY AUTOINCREMENT` and `company_id REFERENCES companies(id)`; remove `accession`, `cik`, `filing_ts_utc`, `time_source`, `summary`. `index_url` is uniquely constrained and also contains the SEC accession identifier. Keep only `filed_date` as a filing-related datetime column. Audit timestamps `created_at` and `updated_at` remain.
- `items`: add `id INTEGER PRIMARY KEY AUTOINCREMENT`, `form_id REFERENCES forms(id)`; remove `item_guid`, `accession` and `part`. Introduce `item_occurrence INTEGER NOT NULL DEFAULT 1` and `UNIQUE(form_id,item_type,item_occurrence)` to preserve 14 pairs of Item codes from different Parts in the provided sample database. The human-readable Part prefix is retained in `item_name` for prior records and newly parsed records.
- `news_company_relations`: rename `company_cik` to `company_id INTEGER REFERENCES companies(id)` so all company associations use the same numeric key. News rows, existing UUID news/relation IDs, and all analysis output values are retained.

## Filing date semantics

`forms.filed_date` is now an ISO-8601 **UTC datetime string**, e.g. `2026-10-16T21:30:58Z`. Existing rows take the former `filing_ts_utc` field, whose source is the Atom feed's `updated` timestamp; the field is **not necessarily the exact official SEC acceptance timestamp**. This is a deliberate compromise to retain UTC time information when eliminating the second filing-time column. The original SEC filing date from a filing summary is not independently retained in the new database. Source Atom XML should be consulted for authoritative field distinctions. `created_at`/`updated_at` remain audit timestamps, not filing datetimes.

## Raw page retention

All newly downloaded successful SEC Atom XML feeds and SEC filing detail/primary pages are archived **before parsing**. SEC complete-submission `.txt` headers used as a fallback are also saved. RSS XML feeds are archived per news publisher. News-article HTML is saved when `--fetch-articles` is enabled and a fetch occurs. Archiving runs for new fetches; when an existing UUID report is reused for parsing, its stored bytes are also copied into the corresponding subject archive without another HTTP request. Previously fetched index pages are not available in the supplied source package/database and cannot be retroactively reconstructed offline.

Every archive payload is stored as original binary bytes, with a sibling JSON manifest containing source URL, SHA-256, byte count, UTC receive time, subject and kind. Each retrieval makes a new file, including repeat URL downloads. Storage will grow unless retention is managed explicitly.

Example tree (relative to DB location):

```text
raw_pages/
  sec_atom/
    8-K/offset_0/<timestamp>_<sha>_<nonce>.xml
    10-K/offset_100/<timestamp>_<sha>_<nonce>.xml
  sec_filings/
    CIK_0000900075__ACCESSION_000119312526424052/
      index/<timestamp>_<sha>_<nonce>.htm
      primary/<timestamp>_<sha>_<nonce>.htm
      submission/<timestamp>_<sha>_<nonce>.txt  # only when fallback needed
  news_feeds/
    A_Sample_Feed/rss_xml/<timestamp>_<sha>_<nonce>.xml
  news_articles/
    <news_id>/html/<timestamp>_<sha>_<nonce>.html  # only on opted-in fetch
```

SEC inline-XBRL wrappers such as `https://www.sec.gov/ix?doc=...` are unwrapped to the validated SEC primary document URL before fetching. The saved payload is the original HTML/XML downloaded from that primary URL, and the source URL is recorded in its manifest. Existing UUID report copies under `reports/` continue to serve the dashboard.

## Additional columns that *could* be removed (not changed automatically)

| Column | Recommendation | Reason / prerequisite |
|---|---|---|
| `forms.form_name` | Candidate | Derivable from `form_type`; adjust dashboard and queries first. |
| `items.file_guid`, `items.file_name` | Strong candidate for a later migration | Redundant with `items.form_id → forms.file_guid/file_name`; `forms_cli` currently checks `items.file_guid` to confirm extraction consistency, so rewrite that test before removal. |
| `companies.ticker`, `companies.exchange` | Keep | Valuable external company attributes, not derivable from CIK offline. |
| `forms.index_url` | **Keep** | Without `accession`, this is the filing's unique durable external reference, and it drives source archiving. |
| `forms.primary_url`, `forms.file_guid`, `forms.file_name` | Keep for now | Document tracing and serving are still dependent on them. Could later use a normalized documents table. |
| `items.item_name`, `items.item_occurrence`, `items.item_text` | **Keep** | The identifier may repeat across Parts; labels and source text are needed for evidence and identity. |
| `news.title_key`, `news.story_key` | Optional derived/cache fields | Recomputable from normalized headlines, but currently used for deduplication and indexes. |
| `news.region`, `news.category` | Keep | Source-level filters/classification may be useful. |
| `news.country`, `news.industry`, `news.government_name`, `news.scope` | Review after classification redesign | Very sparse in supplied snapshot, but live dashboard/scoring still references some. |
| `news_company_relations.entity_key`, `entity_type`, `entity_name` | **Keep** | Needed for non-company government/industry links. |
| `created_at`, `updated_at` | **Keep** | Local processing audit timestamps; separate from SEC filing time. |

## Execution

From `atlas/main_back_end/`:

```bash
python -m sec_edgar.migrate_v10 --db /path/to/edgar.sqlite3
# Or point the standard commands at a database via SEC_DB_PATH.
# export SEC_DB_PATH=/path/to/edgar.sqlite3
python -m sec_edgar.cli --max-atom-pages 100
python -m sec_edgar.forms_cli --limit 500
python -m news_pipeline.cli --fetch-articles
```

For online SEC requests set a real `SEC_USER_AGENT` including your contact email. RSS article HTML is retrieved only for eligible articles when explicitly requested by `--fetch-articles`. Do not expect existing historic network responses to appear until corresponding content is fetched anew.

## Verification and limitations

Against the supplied database snapshot: 8,072 companies, 374 forms, 331 Items, 684 news records, 105 news–entity relations retained; `PRAGMA integrity_check` returns `ok` and foreign-key check returns no rows. New v10-specific tests cover migration, duplicate Item identity preservation, SEC URL handling and archive bytes, and news linking/archive. Pre-v10 tests that construct the old `accession`/`item_guid` schema need updating; the suite has not been fully ported. Dash app import was not verified in this environment because the `dash` Python package is unavailable. The code was bytecode-compiled, and SQL news/dashboard queries and Item analysis dry-run were smoke-tested against the migrated copy. No actual SEC HTTP fetch or OpenAI invocation was performed.
