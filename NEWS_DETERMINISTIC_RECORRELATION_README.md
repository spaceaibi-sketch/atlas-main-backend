# Superseded: see NEWS_PHASE1_IDENTITY_ONLY_README.md

The previous transition required a Python proper-name/CEO/exposure evidence
gate. The new backend patch removes that gate from Phase 1 deterministic identity
matching and from historical recalculation. Unique SEC matches are candidate
links for downstream Phase 2 AI assessment, not verified evidence of impact.
Run `python3 -m news_pipeline.recalculate_correlations` to preview and
`python3 -m news_pipeline.recalculate_correlations --apply` to backfill.
