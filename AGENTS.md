# Film Club Tracker agent instructions

## Project purpose

Film Club Tracker is a small, self-hosted application for a weekly film club. Members authenticate through Plex, suggest films from TMDB, record whether they have seen backlog films, second suggestions, select the current film, rate scheduled or watched films, and view group statistics. Plex supplies library-availability enrichment; Seerr integration can request newly suggested films that are not already available.

The current product includes the `suggested -> scheduled -> watched` workflow and the **This week** view. Older README wording that says there is no scheduling system is obsolete.

The central rule is implemented in `app/service.py`: a film is eligible only when at least one member has explicitly recorded `seen = false`. Missing `prior_views` rows mean unknown, not unseen. Preserve this distinction.

## Technology stack

- Python 3.12, FastAPI, Uvicorn, Pydantic
- SQLite through the standard-library `sqlite3` module
- `httpx` for Plex, TMDB, and Seerr calls
- `itsdangerous` for signed session and OAuth-flow cookies
- `cryptography` for encrypted per-member Plex token storage
- Vanilla JavaScript single-page application; no Node build or frontend framework
- Handwritten CSS and inline SVG visualizations
- Docker image and an Unraid-oriented deployment script

Dependency versions are pinned in `requirements.txt`. Focused rating-sync tests use the standard-library `unittest` runner. There is no formatter configuration, linter configuration, or CI pipeline.

## Repository structure

- `app/main.py`: FastAPI app, request models, middleware, authentication flow, API routes, static serving, and startup tasks.
- `app/service.py`: domain queries and mutations: coverage, backlog, current selection, archive, ratings, member profiles, and admin operations.
- `app/db.py`: SQLite connection helpers, response serializers, schema initialization, and guarded additive migrations.
- `app/schema.sql`: canonical schema for new databases.
- `app/auth.py`: signed sessions, member provisioning, development bypass, and admin dependency.
- `app/config.py`: environment-variable loading and derived paths/settings.
- `app/plex.py`: Plex library cache, deep links, PIN authentication, identity lookup, and server-access authorization.
- `app/plex_ratings.py`: failure-tolerant outbound rating writes and inbound webhook matching.
- `app/token_crypto.py`: encryption/decryption boundary for persisted member Plex tokens.
- `app/tmdb.py`: TMDB search and metadata snapshots.
- `app/seerr.py`: optional, failure-tolerant Seerr request integration.
- `app/events.py`: in-process Server-Sent Events fan-out.
- `app/stats.py`: server-side aggregate/statistical calculations and small-sample handling.
- `app/colors.py`: deterministic member colors.
- `app/seed.py`: destructive-on-`--force` demo-data generator.
- `static/index.html`: SPA shell and static asset version references.
- `static/app.js`: client state, hash router, API wrapper, rendering, and event handlers.
- `static/styles.css`: design tokens, component styling, and responsive rules.
- `Dockerfile`: production container definition.
- `deploy.sh`: current Unraid deployment workflow.
- `deploy-from-mac.sh`: authoritative Mac-side packaging, SSH orchestration, and post-deploy health check.
- `docker-compose.yml`: secondary local/development convenience, not the authoritative deployment path.
- `devdata/`: ignored local database, logs, and generated client identifier. Do not treat it as source or production data without confirmation.
- `filmclub-deploy.tar.gz`: generated deployment bundle containing sensitive environment data. Do not inspect, publish, or regenerate it casually.
- `docs/`: architecture, decisions, feature inventory, and current-state handoff.

## Architecture and data flow

The browser loads `static/index.html` and `static/app.js`. The hash router renders screens and calls `/api/*`. FastAPI authenticates requests with `auth.current_member`, opens a SQLite connection per route, and delegates most behavior to `service.py` or `stats.py`. Mutating API responses are observed by `BroadcastMiddleware`, which sends a lightweight event to authenticated SSE subscribers. Other clients then refresh their current view.

TMDB is queried during search and selection; selected metadata is stored in `movies`, so normal reads do not depend on TMDB. Plex library identifiers live in a process-local cache. Seerr is consulted after a new movie has already been inserted, and integration failure must never roll back the suggestion.

Keep route-specific HTTP validation and authorization in `main.py`; keep reusable domain behavior and SQL assembly in `service.py`; keep external API behavior in its integration module.

## Database conventions

- Use `db.connect()` so rows are `sqlite3.Row` and foreign keys are enabled.
- Use parameterized SQL. Never interpolate user-controlled values into SQL.
- Existing helpers commit each mutation immediately. Preserve their transaction behavior unless a requested change explicitly redesigns it.
- Keep `schema.sql` correct for fresh databases. Schema changes for existing databases go in `app/migrations.py` as a new ordered `(version, name, up)` migration — appended with the next integer, never renumbered. `db.init_db()` runs `schema.sql` (fresh tables) then `migrations.run()`, which applies pending migrations transactionally, records them in `schema_migrations`, and writes a timestamped backup under `<data dir>/backups/` before any migration.
- Schema changes must be additive and safe for an existing bind-mounted `filmclub.db`; there is no downgrade framework.
- Preserve foreign-key deletion semantics and unique constraints.
- `prior_views.seen` is mutable eligibility input. `ratings.seen_before` is a historical fact supplied at rating time.
- `movies.seen_before_snapshot` is captured when a film is scheduled and seeds rating defaults. Do not replace it with live prior-view data.
- Movie statuses currently used by the application are `suggested`, `scheduled`, and `watched`.
- `watched_at` doubles as the scheduled discussion date and the retained archive date. This overloaded meaning is established behavior, although it should be documented if redesigned.

## Authentication and authorization

- Production authentication uses Plex PIN OAuth.
- A Plex identity is authorized only if `PLEX_MACHINE_ID` appears in that account's Plex resources.
- Plex access tokens are encrypted at rest for per-user rating writes and are never returned to the browser or logged.
- The signed session stores the local member ID and durable Plex UUID in an HttpOnly cookie.
- Token encryption is derived from `SESSION_SECRET`; rotating it invalidates stored tokens and requires members to sign in again.
- `DEV_BYPASS_USER` bypasses authentication for anyone who can reach the app. Never enable it in production instructions or deployments.
- Admin status is true when `members.is_admin` is set or the member's Plex UUID appears in `ADMIN_PLEX_IDS`.
- Owner UUIDs from `ADMIN_PLEX_IDS` cannot be demoted or merged away.
- Admin endpoints use `auth.require_admin`. Ordinary authenticated members currently may schedule, unschedule, archive, rate, second, and update their own prior-view state. Do not silently tighten or broaden these rules.
- The direct watched-to-scheduled correction is admin-only. Unscheduling or unwatching to the backlog changes only status and must retain ratings, notes, dates, snapshots, prior views, votes, and metadata.
- A backlog movie may be deleted by its suggester or an admin. The separate admin endpoint may delete any movie.

## Python conventions

- Use module docstrings and type hints in the style of the existing modules.
- Prefer small functions with explicit return shapes.
- Use `None` when the domain distinguishes missing/unknown from false or zero.
- Convert database rows through `db.member_public()` and `db.movie_base()` rather than exposing raw rows.
- External enrichment should degrade predictably where the existing workflow promises it. Log useful context without logging credentials or tokens.
- Broad exception handling is used only at optional integration boundaries where failure must not break the core workflow. Do not spread broad catches into domain code.
- Preserve the established logging namespace pattern (`filmclub`, `filmclub.auth`, and integration-specific children).

## Frontend and UX conventions

- The frontend is intentionally build-free. Do not add a JavaScript framework, bundler, or package manager unless explicitly requested.
- Keep rendering and routing in `static/app.js`; routes use `#/thisweek`, `#/backlog`, `#/watched`, `#/stats`, `#/admin`, `#/profile`, `#/member/:id`, and `#/movie/:id`.
- Escape untrusted strings with `esc()` before inserting them into template strings.
- Use the shared `api()` wrapper so JSON handling, client IDs, and 401 behavior remain consistent.
- Mutating requests carry `X-Client-Id`; the SSE stream uses it to suppress a client's own echo.
- Preserve the poster-forward design, responsive breakpoints, and grid/list preference stored in `localStorage`.
- The app ships dark and light themes. **Never write a colour literal outside the
  `THEME:BEGIN`/`THEME:END` block in `static/styles.css`** — every rule must use a token, or a
  `color-mix()` against one, so a theme stays a flat list of variable overrides. The same applies
  to `static/app.js`: colours there come from `var(--token)` or the server-assigned member colour.
  Use the `-text` variants (`--accent-text` etc.) when a hue is text on the page rather than a fill,
  and the `--on-*` tokens for ink on a saturated chip. Verify with the audit commands below.
- Visual mode is per member: `members.theme` (`system`/`dark`/`light`), exposed on the current-user
  payload via `auth.with_connection_status()` and mirrored to `localStorage` only so the inline
  `<head>` script can paint before first render. It is deliberately absent from `db.member_public()`,
  which also serves other members' public payloads.
- Use existing UI primitives for avatars, posters, buttons, eligibility labels, toasts, skeletons, and cards.
- Member avatars intentionally use deterministic colored initials rather than Plex thumbnails.
- Asset cache-busting is automatic: the index route rewrites the `?v=` markers in
  `static/index.html` from a content hash and serves the shell `no-cache`. Do not hand-bump them.
- Eligibility copy must not represent unknown members as unseen.
- Statistics UI must continue to disclose low confidence and suppression rather than presenting thin samples as reliable.

## External-service conventions

- TMDB searches are debounced by the client and capped server-side. Full metadata is snapshotted on selection.
- Plex enrichment is cached in memory and assumes one Uvicorn worker. Multiple workers require a shared cache/event bus design first.
- A failed Plex enrichment refresh hides availability; it should not make ordinary pages fail.
- Rating writes are local-first and failure-tolerant. Inbound rating webhooks require `PLEX_WEBHOOK_SECRET`, match only this Plex server and scheduled/watched movies, and ignore unchanged echoes.
- Seerr is enabled only when both its URL and API key are present. A request happens after the suggestion insert and must remain non-blocking with respect to the core add flow.
- Do not expose API keys, Plex tokens, session secrets, client identifiers, private hostnames, or actual environment values in code, logs, documentation, fixtures, archives, or examples.

## Environment and configuration

Use `.env.example` as the public variable inventory. Required production variables currently include `TMDB_API_KEY`, `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MACHINE_ID`, `APP_URL`, and `SESSION_SECRET`; `PLEX_CLIENT_ID` may be supplied or generated once in the data volume. Optional variables include `ADMIN_PLEX_IDS`, `PLEX_REFRESH_INTERVAL`, `PLEX_PRODUCT`, `SESSION_MAX_AGE`, `PLEX_WEBHOOK_SECRET` (required for inbound rating sync), `SEERR_URL`, `SEERR_API_KEY`, and `SEERR_TIMEOUT`.

Configuration currently lives in environment variables. Moving operational settings into an admin UI is a future product direction, not current behavior. Any such change needs an explicit design for secret encryption/storage, authorization, validation, restart requirements, and environment-variable precedence.

The repository currently contains sensitive values in local/generated files, including `.claude/launch.json` and `filmclub-deploy.tar.gz`. Do not reproduce those values. Credential cleanup and rotation should be handled as a separate security task.

## Run, build, and validation commands

Production/Unraid deployment is currently driven by:

```bash
./deploy-from-mac.sh
```

This script rebuilds the sensitive bundle, uses LAN HTTP plus SSH to invoke the existing Unraid-side `deploy.sh`, operates on `/mnt/user/appdata/filmclub/data`, replaces the existing `filmclub` container, and requires a populated `.env` plus SSH key access. Treat it as a real deployment mutation, not a routine validation command. `deploy.sh` remains the Unraid-side build/run implementation.

Build the image without deploying:

```bash
docker build -t filmclub:latest .
```

Local testing convenience:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
DATA_DIR=./devdata SESSION_SECRET=dev DEV_BYPASS_USER=Alice TMDB_API_KEY=placeholder \
  ./.venv/bin/uvicorn app.main:app --reload --port 8000
```

The local server can exercise non-TMDB pages with an existing seeded database; real search/add flows require a valid TMDB key. Never commit test secrets.

Available non-mutating syntax checks:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c \
  "import ast,pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path('app').glob('*.py')]"
node --check static/app.js

# Theme audits — both must print nothing. A hit means a colour escaped the token
# system and will not follow the active theme. (The sed strips `white-space`,
# which would otherwise match on `white`.)
awk '/THEME:BEGIN/{s=1} !s{printf "%d:%s\n", NR, $0} /THEME:END/{s=0}' static/styles.css \
  | sed 's/white-space//g' \
  | grep -E '#[0-9a-fA-F]{3,8}\b|rgba?\(|\bwhite\b|\bblack\b|brightness\('
grep -nE '#[0-9a-fA-F]{3,8}\b|rgba?\(' static/app.js | grep -vE '\$\("#|querySelector'
```

A typo'd token (`var(--acccent-soft)`) fails silently in CSS. To catch one, load the
app and run this in the console — it should return an empty array:

```js
[...new Set([...document.styleSheets].flatMap(s=>[...s.cssRules]).map(r=>r.cssText).join('').match(/var\(--[\w-]+/g))]
  .map(t=>t.slice(4)).filter(t=>!getComputedStyle(document.documentElement).getPropertyValue(t).trim())
```

Focused automated tests:

```bash
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover -s tests -v
```

There are no established lint, format, or CI commands. **Recommendation:** expand focused service/API tests before major domain or schema changes.

`python -m app.seed --force` deletes application rows before reseeding. Never run it against a database whose status is uncertain.

## Change discipline

- Inspect the relevant route, service function, schema, and frontend consumer before changing behavior.
- Preserve existing behavior unless the request explicitly requires a change.
- Avoid unrelated refactoring, dependency upgrades, formatting churn, or file moves.
- Do not modify `devdata/`, `.env`, deployment archives, databases, generated client IDs, or logs as part of ordinary source changes.
- Do not casually change eligibility, snapshots, member merging, authentication, admin rules, lifecycle reversal flows, statistics thresholds, or deployment volume paths.
- Verify both fresh-schema and existing-database implications for persistence changes.
- Validate changes in proportion to risk. At minimum, run Python AST parsing and `node --check` when their respective files change; exercise relevant endpoints or workflows when possible.
- State explicitly when integration behavior could not be exercised because credentials or services were unavailable.
- Update `README.md` and the relevant file in `docs/` after meaningful product, architectural, schema, authorization, integration, or deployment changes.
- Recommendations in this file are labeled as such; unlabeled rules describe current repository behavior or required change discipline.
