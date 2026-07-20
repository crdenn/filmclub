# Contributing to Film Club Tracker

Thanks for your interest! This is a small, self-hosted app for a weekly film
club: members suggest films, record whether they've seen backlog films, pick a
weekly movie, rate it, and view group stats. Please read
[`AGENTS.md`](AGENTS.md) — it is the authoritative description of the
architecture, conventions, and domain rules, and this guide is a short
companion to it.

## Ground rules

- **No build step.** The frontend is intentionally vanilla JavaScript with
  hand-written CSS. Do not add a framework, bundler, or package manager.
- **Preserve the domain rules.** In particular: a film is eligible only when at
  least one member has explicitly recorded *not seen*; a missing prior-view is
  *unknown*, never *unseen*. Don't casually change eligibility, scheduling
  snapshots, rating semantics, lifecycle reversals, or admin rules.
- **Never commit secrets.** `.env`, `.claude/`, deployment bundles, databases,
  and logs are git-ignored. Use placeholders in code, tests, and docs.
- **Escape untrusted strings** with the existing `esc()` helper before putting
  them into template strings, and use **parameterized SQL** everywhere.

## Development setup

Requires Python 3.12 and Node (only for `node --check`).

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Run locally with auth bypassed and an isolated database.
# (TMDB search/add needs a real key; other pages work without one.)
DATA_DIR=./devdata SESSION_SECRET=dev DEV_BYPASS_USER=Alice TMDB_API_KEY=placeholder \
  ./.venv/bin/uvicorn app.main:app --reload --port 8000
```

Seed demo data into an isolated database:

```bash
DATA_DIR=./devdata ./.venv/bin/python -m app.seed --force   # destructive on that DB
```

Never run `--force` seeding against a database whose contents you care about,
and never enable `DEV_BYPASS_USER` in a deployment.

## Before opening a pull request

Run the checks that apply to your change:

```bash
# Python syntax
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c \
  "import ast,pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path('app').glob('*.py')]"

# Frontend syntax
node --check static/app.js

# Tests
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover -s tests -v
```

Please also exercise the affected screens in a browser against a throwaway
database when your change is user-visible.

## Pull request guidelines

- Branch from `main`; keep each PR focused on one change.
- Add or update tests for behavior changes where practical.
- Update `README.md` and the relevant file under `docs/` when product,
  schema, authorization, integration, or deployment behavior changes.
- Bump the `?v=` query string in `static/index.html` when you change
  `static/app.js` or `static/styles.css`.
- Describe what you changed and how you verified it.

## Licensing of contributions

This project is licensed under the **GNU AGPL-3.0** (see [`LICENSE`](LICENSE)).
By submitting a contribution, you agree that it is licensed under the same
terms.
