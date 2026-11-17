# Atlas v12.1 web dashboard

Navigation: Overview · Companies · SEC filings · Items · News · Data quality.

- Only the navigation header persists across pages. The compact statistics
  summary appears **only on Data quality**, not on Overview, Companies,
  SEC filings, Items, or News. All four data collections have independently
  searchable, SQL-paginated tables (24 rows per page), with filters and detail
  panels. The overview shows six recent rows per collection.
- News detail presents article text (or RSS summary), ALL linked companies and
  `correlation_reason`, and saved AI assessments where completed.
- SEC filing detail includes the original SEC index, primary report, locally
  saved report and extracted Item links. Item detail has its source text, AI
  impact fields, and explicit Analyze / Ask AI controls.
- The Data quality summary shows six concise groups: Companies; News Total; News 3h;
  Items Total; Items 3h; Latest UTC (News published, News analysed, Item
  extracted, Item analysed). It occupies no more than 10dvh; on narrow screens
  it scrolls horizontally rather than shrinking text until unreadable.
  Counts and quality checks load only when that page is open; other pages
  do not execute the summary queries.
- News 3h uses `news.published_utc`. Item 3h and last extracted time use
  `items.created_at`, the first saved extraction datetime. AI update time is
  stored separately as `items.updated_at` when analysed. Re-extraction of an
  existing record cannot be independently dated without a dedicated timestamp.
- The dashboard does not start data pipelines and cannot by itself make an
  out-of-date filing inventory fresh. Run the SEC collection command separately.

Code: `app.py`, `dashboard/explorer_queries.py`, `dashboard/compact_summary.py`,
`assets/style.css`. Database schema and collection/scoring workflows are not
changed in this web redesign.

## Data quality page

- Five status ledgers: filings (`created`, `extracted`, `parsed`, `error`),
  Items (`created`, `processed`, `analysed`, `rejected`, `error`),
  News (`created`, `skipped`, `connected`, `analysed`),
  event grouping (`pending`, `main`, `duplicate`), and
  news/company relationships (`connected`, `analysed`, other statuses if present).
- Each status shows **total records**, **records from the last 3 hours**,
  and the most recent `updated_at` timestamp (for event grouping,
  `batch_reviewed_at` when available). Rows include zeros.
- News and event 3-hour counts use the original publication time; filing,
  Item and relationship 3-hour counts use the first-saved timestamp.
  These columns describe the **current status of records in the window**,
  not the number of transitions that occurred within the window.
- Latest activity reports company profile updates, filing discovery/parsing,
  initial Item extraction and AI updates, news ingestion/publication/AI updates,
  and company relation creation/AI updates.
- Checks show failed or rejected records, parsed forms without Items, empty
  Items, news lacking full text, main stories without links, relations without
  reasons, invalid duplicate references, future-dated news, and incomplete
  analysed news. These are inspection signals, not automatic repairs.
- The page never writes the SQLite database and does not create a separate
  audit database or require a schema migration.
