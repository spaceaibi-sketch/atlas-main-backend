# Atlas Dash Database Explorer

The dashboard has a read-only **DB Explorer** tab at `/database`.

## Enable access

Because it can display raw database content, the explorer is **disabled unless an administrator provides a secret**. Add the following to `main_back_end/.env` (use a unique long random value, not the example):

```text
ATLAS_DB_EXPLORER_TOKEN=REPLACE_WITH_A_LONG_RANDOM_SECRET
```

Launch the existing dashboard with `./dash.sh` on Linux or `dash.bat` on Windows. Those launchers load `.env`. Restart the dashboard after changing the token. Open `/database`, enter the secret in the **Admin access token** field, and choose a table.

**Security:** Keep the dashboard bound to `127.0.0.1` by default, or restrict external access through a private VPN/reverse proxy with HTTPS and authentication. Do not publish an unprotected Dash instance to the internet. The token is sent in the dashboard callback request body, never in a query-string URL. Do not share it or store it in source control.

## Features

- Inspect all application tables in the database set by `SEC_DB_PATH` (defaults to `atlas/data/edgar.sqlite3`).
- View column names, SQLite data types, primary-key and nullability information.
- Sort by a selected column in ascending or descending order.
- Filter a selected column with contains, equals, greater than, less than, IS NULL or IS NOT NULL. Contains interprets `%` and `_` literally.
- Browse **40 rows per page** using Previous, Next and Page controls. Open long text fields to preview up to 16,000 characters. BLOB columns show sizes instead of raw bytes.
- Refresh a table manually to see newly committed rows. Sorting/filtering/page parameters are validated server-side.

The query connection uses SQLite `mode=ro`, hard limits page sizes and execution duration, and never accepts arbitrary SQL. The explorer does **not** migrate, change or delete data. Filtering/sorting very large unindexed columns may time out; choose indexed keys for large price tables.


## News companies and responsive records

When browsing `news`, the explorer adds the read-only **Linked companies** field
from `news_company_relations` + `companies`. This field reflects verified
company IDs, while the physical `news.companies` column shows AI-extracted
candidates (`Name One|Name Two`), including names that have not passed matching.
Company links are read in one batched query per displayed page. Every original
column appears in a wrapping record card, without any horizontal scrollbar.
Long text values may be expanded individually.
