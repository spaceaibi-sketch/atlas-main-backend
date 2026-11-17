# Atlas — Phase 1 SEC identity-only matching and historical transition

This is a **backend-only overlay**. Extract the archive in `/opt/atlas` to replace
only its included files; no schema migration or Android update is needed.

## Why change

Phase 1 AI receives the full `Official SEC Company Name|Ticker` catalog and the
news articles. It must identify directly and indirectly affected companies,
and return the exact catalog name whenever the company is uniquely identifiable.
For example an article saying `Amazon` should lead to `AMAZON COM INC` if that
is the unique matching catalog identity. The prompt now makes this mandatory.
Company names genuinely absent from the SEC catalog continue to be preserved
in `news.companies` without creating an SEC relationship.

Deterministic linking **no longer applies a Python proper-name, CEO, or exposure
evidence gate**. After AI extraction, a unique SEC identity match is enough to
create a **candidate** relationship. The Python matcher still uses exact,
jurisdiction-suffix-normalized and unique word-boundary bidirectional-contains
matching. Ambiguous or unsafe partial names never create a link. The original
AI names remain in `news.companies`, whether matched or not.

Phase 2 AI remains responsible for evaluating relevance, evidence, impact and
confidence. A candidate link does **not** establish that the article affects
the company.

## CEO clarification

The supplied catalog is **only company names and tickers**, not CEOs. Atlas
also supports a separate, optional `data/company_ceos.json` file. If it exists,
its entries may be passed as extra context to Phase 1 AI, but CEO name and
source URL are **not independently verified** by Python, and this file no
longer has any role in allowing or blocking deterministic identity links.
The historical transition never requires or consults a CEO or exposure file.
Older `--ceos-file` and `--exposures-file` transition flags remain accepted for
CLI compatibility but are ignored.

## Transition / recorrelation

The historical script recalculates relationships from saved AI names in
`news.companies` and does not make OpenAI calls. It is safe to rerun, never
removes existing links, does not overwrite prior Phase 2 scores, and does not
change already-skipped News.

1. Stop the News collector and other database writers. Create your own database
   backup before deploying the patch.
2. From `/opt/atlas`, unzip the patch.
3. In `/opt/atlas/main_back_end`, preview and inspect the JSON/CSV reports:

   ```bash
   python3 -m news_pipeline.recalculate_correlations
   ```

4. Apply if the candidates look correct:

   ```bash
   python3 -m news_pipeline.recalculate_correlations --apply
   ```

`--apply` makes another automatic timestamped backup next to the database. The
report defaults to `/opt/atlas/data/reports/news_recorrelation/`.

A report outcome of `would_insert` means a **uniquely identified company** is
eligible for a candidate link, not that supporting exposure was verified.
No `insufficient_evidence` outcome is emitted by the new script. `already_linked`,
`unmatched`, `unsafe_partial`, `ambiguous`, and `duplicate_alias` remain.

**Important:** If a previously `analysed` News record receives a new candidate,
its News status changes temporarily to `connected` to make the new relation
eligible for the existing Phase 2 scorer. Previously analysed relation rows and
scores are preserved. Once all its candidate relations are scored, the normal
Phase 2 code restores the article status to `analysed`.

Run Phase 2 with your usual process. To verify the new pending count:

```bash
python3 -m news_pipeline.scoring_cli --dry-run
```

## Test coverage

```bash
cd /opt/atlas/main_back_end
python3 -m pytest -q tests
```

Tests exercise Phase 1 and historical matching, private/unknown company
preservation, ambiguous names, direct and indirect matches without CEO or
exposure records, transition idempotence, and already-analysed records.
