# News AI scoring prompt

The production prompt and strict JSON response schema are in `news_pipeline/scoring.py`.

The standalone `run_ai_news_analys` worker receives a **batch** of already-
linked main-news / entity pairs. The input includes each relation ID, the exact
SEC company or national government name, the title, summary or available article
text, the source, and the AI-extracted company-specific correlation reason (plus any legacy match evidence). The correlation reason is not an independently verified source fact; inspect it against the source text before using it for an impact assessment.

For each relation it must return these eight fields, plus the unchanged `relation_id`:

| Field | Required meaning |
|---|---|
| `ai_impact` | Integer −10..+10; zero is neutral |
| `ai_confidence` | Finite numeric 0..10 |
| `ai_reason` | Impact explanation specific to that entity |
| `ai_evidence` | Exact excerpt from supplied text or `Not stated` |
| `ai_monetary_effect` | Signed finite USD number (0 if unquantified, with an explanation) |
| `ai_monetary_effect_reason` | Explanation of amount, conversion assumptions, or unquantified 0 |
| `price_change_1w_pct` | Hypothetical 7-day signed percentage change; 0 allowed |
| `price_change_reason` | Assumptions, catalyst, sentiment, uncertainty and scenario disclaimer |

Strict numeric validation and required explanations apply. AI may not invent
quotes, money amounts or stock prices. The estimates are speculative scenarios,
not observed market returns or trading recommendations. Government entities
without a stock share price receive 0% and a not-applicable explanation.

The response must contain one object per relation ID. The entire batch is
rejected if any score is malformed, avoiding partial database updates.
