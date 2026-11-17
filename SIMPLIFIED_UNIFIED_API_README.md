# Atlas Java API — simplified unified table support

Overlay `src/main/java/space/aibi/atlas/Api.java` and `Db.java` onto your existing `android_back_and_java` source. Install this update **after** successfully applying `main_back_end/scripts/migrate_simplified_unified_model.py --apply` to `/opt/atlas/data/edgar.sqlite3`.

The API reads `raw_items.status` as the canonical source state, uses the presence of `relationships.ai_impact` to determine if an individual company assessment is present, and does not query deleted columns from `relationships`. Its timeline exposes stored `accumulated_impact` and `accumulated_monetary`. SEC item form type is read from `raw_items.source_name`; filing form type stays in filing source metadata.

From the existing Java backend source, run `python3 tests/test-unified-api-without-legacy.py`, then `sudo bash scripts/install-service.sh` and `sudo systemctl restart atlas-mobile-api-java`.

Legacy main-backend writer tables remain present until the corresponding production writers and dashboards have been converted. No subscription or Android application code changes are included.
