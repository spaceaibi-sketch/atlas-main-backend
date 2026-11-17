# Atlas — AI-first company correlation / workflow audit (17 Oct 2026)

## Project layout

```text
atlas/
├── main_back_end/       application, launchers, packages, assets, documentation, tests
└── data/                durable SQLite, reports, raw_pages, cron state, optional evidence registers
```

The default `SEC_DB_PATH` now resolves to `atlas/data/edgar.sqlite3`. All generated reports and raw downloads are placed beside this database. Linux cron locks, stamps and logs also use `atlas/data`. Historical data was **not** present in the source ZIP; this release does not invent, copy or delete real production records.

## Correlation contract

1. AI batch-deduplicates events (existing workflow).
2. AI extracts affected company pairs only from the surviving main articles. The second batch AI request must produce one plain-text `ID|Company|Reason` line per company or `ID|NONE`; `ID` is **batch-local** and mapped internally to the SQLite UUID.
3. Extraction considers directly named companies/verified executives **and** demonstrably affected companies with documented existing contracts, operations, or related relationships. Only candidate names and their reasons are proposed by AI.
4. Deterministic matching is **AFTER** AI: exact unique full corporate identities only; no whole-article scan against the catalog, no substring/keyword, no ticker/alias fanout. A second gate checks for actual proper names, verified full CEO name records, or a contract/exposure record. An unmatched or ungrounded AI candidate is rejected and is not stored as a news/company correlation.
5. Save only validated `Company|Reason` strings in `news.company_reasons_json`; save the corresponding string reason in `news_company_relations.correlation_reason`. Missing candidates use `[]`. Do not add DIRECT or INDIRECT identifiers. Subsequent separate AI impact scoring receives the company-specific correlation reason.
6. Both AI calls and deterministic validation finish **before** approving/committing the event batch. A malformed/partial company output aborts the batch; SQLite writes are transactional. A second AI request per event batch therefore adds model/API cost compared with the old implementation.
7. Existing provisional unscored keyword links can be explicitly rebuilt using `python -m news_pipeline.review_existing --limit 100` (dry-run), then `... --apply` (billable). Already-analyzed legacy relations are skipped rather than deleting historic scores without review.

### Verified external context

Optional `atlas/data/company_exposures.json` supports contracts, operations, concessions, active investments, with `company`, `country`, `sector`, `business_connection`, `evidence`, `source_url` and optional `valid_until`. Optional `company_ceos.json` supports CEO-only identification. `.example.json` files are templates, **not verified records**. The application checks structural completeness and staleness, but **does not verify the URL contents or current corporate exposure itself**. Without supported evidence, a nonmentioned company must not be linked. Requests are capped at 80 exposure and 80 CEO records and 700 kB AI input; larger registries need a targeted evidence retrieval/index step instead of silent truncation.

## Source-level efficiency / reliability findings

| Workflow | Status | Observation / recommended work |
|---|---|---|
| Scan every article against every SEC company, ticker and alias | **Fixed in active path** | Old algorithm was approximately O(number of articles × catalog size × pattern checks), with ambiguous `news` and `paid` matches. Now only AI-proposed identities are looked up once per extraction batch. |
| One SELECT per resolved company to fetch CIK | **Fixed** | CIK is read in the same catalog scan as the unique ID, then reused in bulk insert. |
| Old provisional keyword links never invalidated | **Partly fixed** | Explicit opt-in `review_existing` clears and rebuilds unscored relationships. Historic `analysed` links are not silently changed; they need deliberate review. |
| Missing company-specific correlation reason | **Fixed** | Stored on both news and relationship. New column additions receive an online SQLite backup before migration. |
| AI grouping success before company AI failure | **Fixed** | No event approval is saved until both calls validate, then one SQLite transaction. |
| Duplicate AI relation-score work | **Retained** | Scoring remains a separate batch after correlation. It now receives the correlation reason; no one-company-at-a-time requests are needed. |
| Unbounded extraction inputs | **Fixed** | Adaptive text compaction enforces a 700-kB serialized request cap and 500 articles maximum. |
| News fetches only inside a rolling 3-hour window | **Outstanding** | Outage/API error can permanently skip queued news when it ages out. Add replay cursors and a persisted retry queue with an explicit backfill policy. |
| Optional article downloads made sequentially | **Outstanding** | `--fetch-articles` attempts one network fetch per pending story in a Python loop. Bound concurrency with per-domain rate limits and a shared session, then verify provider terms. |
| Every raw fetched payload archived as a new file | **Outstanding** | A repeated feed fetch creates another immutable payload even if SHA-256 is unchanged; implement a keyed content-addressed archive index and retention/GC policy without losing provenance. |
| Indirect-contract source verification | **Partly supported** | Curated registry records can be supplied, but sources are not fetched/checked or periodically revalidated. Implement primary-source verification and time-validity tracking to support rigorous macro→contract routing at scale. |
| Large model input and model spend | **Tradeoff** | Two batch requests (event grouping and company routing) are now intentional, safer than per-company calls but more costly than the old single batch call. Measure token counts and cached-context behavior in production. |
| Legacy tests against v9 data schema | **Isolated (not ported)** | The v10 archive shipped older tests using `accession`, `company_cik` and obsolete APIs. Those files now live in `tests_legacy_pre_v10/`; the maintained v10+/v11 tests live in `tests/`. Historical tests are preserved, not marked as passing. |
| Dashboard missing the company-specific reason | **Fixed** | The correlation detail shows the stored reason, not misleading keyword-hit counts; an expandable list shows extracted pairs. |

## Data migration and deployment

1. Stop collector, dashboard and scheduled tasks. Back up the entire **old** data directory, `.env` and `assets/key.txt`.
2. Extract `atlas/` so `atlas/main_back_end/` and `atlas/data/` are adjacent. Move the real old database, reports, raw-pages, cron state, and locks into `atlas/data/` (the shipped data folder only contains examples/documentation).
3. Update any pre-existing `SEC_DB_PATH` in `.env`, environment variables, systemd/cron, or Windows Task Scheduler to point to the relocated SQLite file. Windows launchers run from `atlas/main_back_end`.
4. From `atlas/main_back_end`, run setup and the dashboard / pipelines normally. `news_pipeline.store.ensure_schema()` adds the two columns and saves a `*.before_ai_context_v11.bak` online snapshot before the additive news/relation migration.
5. Preview legacy unscored news correlations with `python -m news_pipeline.review_existing --limit 100`; add `--apply` to replace them with AI-first associations. Use `--exposures-file` / `--ceos-file` for a prepared evidence registry.
6. Do **not** automatically purge previously scored `analysed` links; verify and selectively reanalyze them from independent evidence.

## Verification

- New `tests/test_ai_first_correlation.py` covers formatted extraction, missing ID rejection, false `NEWS CORP` / `PAID INC` candidates, indirect context, JSON/relation persistence, schema columns, layout and transactional rollback.
- `compileall` and `bash -n` verify Python/shell syntax. No production OpenAI call or live SEC/RSS request was made.
- The supplied v10 archive contained no production SQLite database or documents. Migration was tested on synthetic data and cannot guarantee live records until run on a backup of the actual file.
