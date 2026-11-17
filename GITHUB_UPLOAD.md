# Upload Atlas main backend to GitHub

This folder is ready to use as the **root of a Git repository** (for example,
`atlas-main-backend`). It is the backend source project, **not** the `/opt/atlas`
runtime data directory, and not the separate Android application or API.

## Before publishing

- **Use a private GitHub repository initially.** The source contains backend and
  database-architecture details. Review the repository before making it public.
- This package contains `.env.example` (placeholders), but **not** `.env`,
  `assets/key.txt`, or any production SQLite database. Never commit real API keys,
  bearer tokens, logs, raw SEC reports, local archives, or production data.
- Review all documentation and examples for information you do not want public.
- The packaged source and run scripts are unchanged. There are no deployment,
  database migration, or live-service actions in this package.
- A GitHub archive and `.gitignore` do **not** remove a secret from Git history
  if it has already been committed. Rotate any exposed credentials.

## Upload with Git (Linux/macOS/Windows Git Bash)

1. Create an **empty** private repository on GitHub, without an initial README,
   `.gitignore`, or license; for example `atlas-main-backend`.
2. Extract this ZIP and open a terminal **inside** the `atlas-main-backend`
   directory that contains `README.md` and `app.py`.
3. Run the commands below, substituting your actual GitHub username or organization.

```bash
git init -b main
git add .
git status --short
git diff --cached --check
git commit -m "Initial Atlas main backend source"
git remote add origin https://github.com/YOUR_USERNAME/atlas-main-backend.git
git push -u origin main
```

You can instead upload the **extracted folder contents** via GitHub's web
interface: repository > `Add file` > `Upload files`. Git CLI is recommended
because it preserves shell executable permissions and handles updates better.

## Before every subsequent push

Check exactly which files will be published:

```bash
git status --short
git diff --cached --name-only
git ls-files | grep -Ei '(^|/)(\.env($|\.)|key\.txt$|data/|.*\.(db|sqlite3?)$)' || true
```

The `grep` above is a quick file-name check, not a comprehensive secret scanner.
Use GitHub secret scanning or another dedicated secret scanner before exposing
source publicly.

## Validate the source without touching production data

```bash
python -m compileall -q .
# To run the complete tests, first install your app's requirements:
python -m pip install -r requirements.txt -r requirements-massive-ws.txt pytest
python -m pytest -q tests
```

The Python commands above are for a local copy, not a live production upgrade.
They may require external dependency downloads. Tests should be reviewed before
running on a machine configured with live API credentials.

## Production layout is unchanged

```text
/opt/atlas/
  main_back_end/  <-- clone or deploy the source here (carefully)
  data/           <-- private runtime database, logs and report files
  tmp/            <-- temporary operations
```

Never replace your running `/opt/atlas` installation with an unreviewed
checkout. Keep your private `/opt/atlas/main_back_end/.env` and `/opt/atlas/data`
unchanged. Deployment and migrations are out of scope for this GitHub package.

No license was added. A public GitHub repository is **not automatically
open-source licensed**; add a LICENSE file only if you choose to grant reuse
rights.
