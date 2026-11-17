# Atlas .env configuration

Runtime credentials and adjustable parameters live in **main_back_end/.env**.
Copy `.env.example` to `.env` once, edit real credentials, and set `chmod 600 .env`.
The package includes only `.env.example` (no API keys, DB or user-specific `.env`).

## Choose the AI API in `.env`

Both SEC Item AI and News Phase 1/Phase 2 AI use the selected provider. The
backend continues to use the OpenAI Python SDK as a compatible transport for
both providers' Responses API endpoints. Requests, strict JSON schemas,
validation, database behavior, batch sizes and cron jobs are unchanged.

For OpenRouter, add or update the following lines in your private `.env`:

```dotenv
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=YOUR_REAL_OPENROUTER_KEY
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openai/gpt-5-mini
NEWS_OPENROUTER_MODEL=openai/gpt-5-mini
AI_TIMEOUT_SECONDS=300
```

For direct OpenAI instead, use:

```dotenv
AI_PROVIDER=openai
OPENAI_API_KEY=YOUR_REAL_OPENAI_KEY
OPENAI_MODEL=gpt-5-mini
NEWS_OPENAI_MODEL=gpt-5-mini
AI_TIMEOUT_SECONDS=300
```

* `AI_PROVIDER` defaults to `openai` when absent, preserving existing installations.
  Other values are rejected; a missing key never causes fallback to another provider.
* `OPENROUTER_MODEL` is used for SEC Item scoring and questions;
  `NEWS_OPENROUTER_MODEL` overrides it for News Phase 1 and Phase 2, when set.
  Use a full OpenRouter model ID (for example `openai/gpt-5-mini`) that supports
  the project's Responses and JSON-schema requirements. Check model support
  before choosing a different provider/model: not all models offer the same
  structured-output guarantees.
* `OPENAI_MODEL` and `NEWS_OPENAI_MODEL` retain their previous roles for direct
  OpenAI usage. Per-command `--model` arguments continue to override defaults.
* `AI_TIMEOUT_SECONDS` applies to SEC Item and News Phase 2 requests;
  `NEWS_PHASE1_TIMEOUT_SECONDS` separately controls News Phase 1 requests.
  If `AI_TIMEOUT_SECONDS` is absent or blank, the existing
  `OPENAI_TIMEOUT_SECONDS` setting is honored for compatibility.
* `.env` is excluded from version control and must remain private. The package
  supplies `.env.example` only, so existing private `.env` files are never
  overwritten by the update.

If your previous OpenAI credential is in `assets/key.txt`, **move that secret into**
`OPENAI_API_KEY=...` in `.env` before selecting direct OpenAI. The legacy
`assets/key.txt` is not read. Once you've confirmed credentials work,
securely remove the old key file. Do not paste API keys into chat or logs.

All shell entry points call Python CLI modules that load `.env` safely. No
arbitrary `.env` content is executed as shell code. `install_cron.sh --defaults`
reads the five `ATLAS_CRON_*_HOURS` settings from `.env`; zero disables that job.

Both scoring launchers defer to their Python defaults:
* `SEC_AI_BATCH_SIZE=20` (1–25)
* `NEWS_SCORING_BATCH_SIZE=20` (2–100)

Command-line overrides (e.g. `--batch-size 5`) continue to work for a single run.
An explicitly exported environment variable takes precedence over `.env`.

The `.env` settings cover OpenAI/OpenRouter and Alpaca keys/models/timeouts, SEC user agent,
SQLite path, dashboard binding/token, SEC extraction and HTTP settings, News
sources/ingestion/Phase 1/Phase 2 settings, price feed and network settings,
logging, and per-job cron intervals. Database schema, response-validation
requirements, business invariants, and SEC safety minimum intervals remain
in source code rather than being configurable.

## Validation

Run these commands after deployment:

```bash
cd /opt/atlas/main_back_end
chmod 600 .env
./run_ai_sec_scoring.sh --help
./run_ai_news_scoring.sh --help
python3 -m shared.config_cron
bash install_cron.sh --defaults
```

## SEC directory raw-archive collision fix

Earlier versions wrote JSON payloads and metadata sidecars to the same
`<stamp>_<sha>_<uuid>.json` path. JSON metadata is now written as
`<stamp>_<sha>_<uuid>.meta.json`. Existing archived files are not changed and
filing ingestion is unaffected.
