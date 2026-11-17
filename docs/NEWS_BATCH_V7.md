> **Historical v7 document.** For the active AI-first company extraction and sibling data layout see `AI_FIRST_CORRELATION_AND_WORKFLOW_AUDIT.md`. Legacy deterministic keyword correlation described below is RETIRED.

# News batch-only semantic deduplication (v7)

## Command

```powershell
cd C:\Dev\sec_edgar_collector
.\extract_news.bat --batch-size 500
```

The latest 3-hour RSS news is saved first. One OpenAI Responses call then
compares up to **500 total articles per batch** (new articles + a small
reference window of recently reviewed main articles). AI returns only compact
integer-ID groups. The first ID in each group is the **main**. Remaining IDs
are duplicates, marked `skipped`; no main-to-duplicate relationship is persisted
foreign key. Unlisted new articles are also considered main events.

For `N` news stories, the log summarizes **batches**, **main**, **duplicates**,
**groups**, **deferred**, and **errors**, rather than emitting one AI message
per article. Example:

```text
AI BATCH [1] input=118 (new=118 reference=0) main=87 duplicate=31 groups=24
```

The numbers above are only an example. Deterministic matching for SEC companies
and country governments can create provisional relations for mains without
calling AI again; detailed AI impact scoring is intentionally **disabled**.
Existing completed AI scores are not deleted or overwritten.

### Important safeguards

- Ingestion deduplicates canonical URLs only. Different publisher URLs remain
  stored so the AI can select a better main article.
- Previously selected mains can be comparison references; the AI cannot
  demote them. The comparison window is limited to recent 3-hour mains.
- IDs are checked before any group is saved. Unknown IDs, cycles/overlap, or
  attempts to demote a previous main reject the whole batch.
- One isolated story is deferred, not submitted as a one-article API request.
- Temporary API errors leave a whole batch retryable. No partial group saves.
- Duplicate stories remain in SQLite for provenance and are visible from the
  main article's detail page in Dash.
- Future detailed filtration, sentiment and price impact analysis will need a
  separate batch stage; this release does not perform them.
- Automatic backup: `data/edgar.sqlite3.before_news_batch_v7.bak`.

### Options

```powershell
.\extract_news.bat --ai-only --batch-size 500
.\extract_news.bat --dry-run
.\extract_news.bat --max-feeds 5 --max-new 40 --batch-size 500
```

`--ai-only` reviews pending news without fetching the feeds again. `--dry-run`
reports news counts without AI calls. Previously reviewed main and duplicate
articles are excluded from repeat API requests.
