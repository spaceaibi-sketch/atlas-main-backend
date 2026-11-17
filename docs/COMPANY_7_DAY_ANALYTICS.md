# Seven-day company analytics — Atlas v12.2

## Storage

Three persisted **materialized indicators** in `companies`:

| Column (exact name) | Unit | Meaning |
|---|---|---|
| `acumm_impact` | age- and confidence-weighted impact points | Sum of scored component impacts, weighted by confidence and age. **Not** a bounded -10..10 score. |
| `acumm_monetary` | USD | Sum of age-weighted `ai_monetary_effect` fields. Not actual cash flow and not a deduplicated financial total. |
| `accum_confdent` | 0..10 | Age-weighted mean of reported AI confidences, not a calibrated success probability. |

The nonstandard spellings are intentional: they match the requested column names.
The additive migration is idempotent and creates `edgar.sqlite3.before_company_analytics_v12_2.bak` once, **before** altering an existing `companies` table. No old company records, Items, news, or AI results are deleted.

## Eligibility and timing

- Only `items.status='analysed'` from `forms.status='parsed'`, dated by SEC `forms.filed_date`.
- Only `news_company_relations.status='analysed'`, where `entity_type='company'`, `news.batch_status='main'`, and `news.status='analysed'`, dated by `news.published_utc`.
- Events with a timestamp strictly beyond the current UTC instant are excluded. Events older than 7×24 hours are excluded.
- `age_weight = 1 − age_in_seconds / 604800`, hence 1 for a just-published event and 0 exactly seven days later.

For all eligible components `i`:

```
acumm_impact     = round(SUM(impact_i * confidence_i / 10 * age_weight_i), 4)
acumm_monetary   = round(SUM(monetary_i * age_weight_i), 2)
accum_confdent   = round(SUM(confidence_i * age_weight_i) / SUM(age_weight_i), 4)
```

If there are no eligible components or all eligible weights sum to zero, the columns are zero. Values shown on a selected company's detail page include the age and weight of each contributing news relationship and Item, its AI impact, confidence, monetary field, 7-day scenario, rationale, evidence, and source document link.

No company is added or inferred by this aggregation. Records must already have a stored, resolved company ID. Existing unanalysed placeholder zeroes do not count. Some **analysed** monetary zeroes can mean unknown/unquantifiable; inspect each component's monetary reason. Duplicate economic exposures or separate Items repeating the same amount can cause overcounting. This is a transparent sum of saved AI fields, not a measured economic balance or a share-price forecast.

## Refresh / operational impact

The website recomputes these columns on every Companies page refresh (every ~60 seconds while open), before listing and opening company detail. The Item and News AI workers refresh them after successful completion. The original `companies.updated_at` field is **not** changed by aggregate recomputation, so it continues to mean SEC company profile changes.

For a manual refresh independent of the website:

```
cd atlas/main_back_end
python -m dashboard.company_analytics
```

This runs set-based SQL over all qualified assessments; no company-by-company Python matching, external API calls, or paid AI requests are made.

## Status display

The Data Quality page and the Items, Filings, News filters now display raw states that appear in application code/database (`created`, `extracted`, `parsed`, `processed`, `analysed`, `rejected`, `skipped`, `connected`, `error`, `pending`, `main`, `duplicate`) appropriate to each table, without substituting invented labels such as “discovered” or “ready for AI”.
