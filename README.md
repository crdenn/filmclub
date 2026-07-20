# Film Club Tracker

A small, self-hosted app for a weekly film club. Suggest films, record what
everyone has seen, second the ones you want to watch, pick a movie for the week,
rate and discuss it, and browse group stats — with an optional Plex integration
for library availability, deep links, and rating sync.

Runs as a single Docker container with a SQLite database on a bind-mounted
`/data` volume. Dark, poster-forward, no-build vanilla-JS frontend.

> **Status:** pre-1.0, self-hosted. Licensed under **AGPL-3.0-or-later**. Plex is
> currently required; making it optional (with invite-only local accounts) and
> shipping a prebuilt image are in progress — see `CHANGELOG.md`.

## The one rule

> A film is only eligible to be picked if at least one member has **explicitly
> recorded that they have not seen it.**

The app never blocks a pick, but it keeps the signal honest:

- A member with **no answer** is **unknown**, not unseen — an unanswered member is
  never treated as evidence a film is pickable.
- "Everyone's seen it" is shown plainly and the card is dimmed.
- Each backlog card carries a two-button **Seen it / Not seen** control for the
  current user and a condensed summary of who still hasn't seen it; the full
  per-member breakdown lives on the movie page.

## Features

**This week**
- A full-bleed hero for the current pick: backdrop, poster, synopsis, discussion
  date (editable), who's watched it, and your own seen control.

**Backlog**
- Poster grid or list of suggestions with condensed coverage, eligibility state,
  suggester, and your seen control.
- Search by title, filter by suggester (or "your suggestions"), and sort by
  most-seconded, unseen count, date, title, year, or runtime.
- **Second** (`+1`) films you'd also like to watch (you can't second your own).
- Live updates: another member's change appears without a manual refresh.

**Watched**
- Most-recent-first grid showing the club average, **your** rating, and a calm
  "Rate" prompt for anything you haven't scored yet.

**Movie detail**
- Metadata header (poster/backdrop, director, runtime, discussion date, genres,
  synopsis, suggester, Plex "Watch" link when available).
- A rating input during the scheduled week (0.5–5 in half-steps, an optional
  note, and a "had you seen this before?" toggle), then all ratings grouped by
  **first watch vs rewatch** with per-group averages.
- Coverage/eligibility presented alongside ratings; lifecycle actions
  (pick / archive / send back) with destructive actions kept in an overflow menu.

**Profiles & reminders**
- A personal profile hub (display name, Plex rating-sync status, activity summary)
  and public per-member profiles linked throughout the app.
- Amber reminder badges count films you still need to mark seen/unseen or rate.

**Statistics** (all server-side, with small-sample suppression so thin data never
masquerades as signal): group totals and runtime, rater profiles, a pairwise
taste-agreement matrix, divisiveness, first-watch-vs-rewatch deltas, suggester
scorecards, and genre/decade distributions.

**Integrations**
- **TMDB** — search and a local metadata snapshot on selection (no re-fetch per
  load); original language captured.
- **Plex** — OAuth login restricted to accounts with access to *your* server;
  "In Library" badges with deep links; Rotten Tomatoes critic/audience scores for
  matched films; and optional **two-way rating sync** (future changes only).
- **Seerr** *(optional)* — adding a film that isn't already on Plex submits a
  request to Overseerr/Jellyseerr. Degrades safely when off or unreachable.

**Admin** — member list, placeholder-member merge, admin grants, movie deletion,
manual Plex refresh, and a diagnostics endpoint (version, schema, DB health,
backups, integration status).

## Stack

FastAPI + SQLite (standard-library `sqlite3`, WAL mode), a no-build vanilla-JS
single-page frontend served by FastAPI, hand-rolled inline-SVG charts. `httpx`
for outbound calls, `itsdangerous` for signed cookies, `cryptography` for
encrypted per-member Plex tokens. No external database and no Node build step.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env — at minimum set the required values below
docker compose up -d --build
```

Open `APP_URL` in a browser. The first Plex login auto-provisions your member
row. (An admin is anyone whose Plex UUID is listed in `ADMIN_PLEX_IDS`.)

### Configuration

All configuration is via environment variables; see [`.env.example`](.env.example)
for the annotated list.

| Variable | Required | Purpose |
|---|:---:|---|
| `TMDB_API_KEY` | ✓ | Film search and metadata |
| `PLEX_URL` | ✓ | Plex server base URL for enrichment |
| `PLEX_TOKEN` | ✓ | Plex server token for enrichment |
| `PLEX_MACHINE_ID` | ✓ | Authorization: the server-access check |
| `APP_URL` | ✓ | OAuth callback target; must match how you open the app |
| `SESSION_SECRET` | ✓ | Cookie signing + per-member token encryption key |
| `PLEX_CLIENT_ID` | — | Stable OAuth client UUID (auto-generated into `/data` if unset) |
| `ADMIN_PLEX_IDS` | — | Comma-separated Plex UUIDs granted owner/admin |
| `PLEX_WEBHOOK_SECRET` | — | Enables inbound Plex→Film Club rating webhooks |
| `PLEX_REFRESH_INTERVAL` | — | Seconds between library refreshes (default 3600) |
| `SEERR_URL`, `SEERR_API_KEY` | — | Enable Seerr auto-request when both are set |
| `SEERR_TIMEOUT` | — | Per-request Seerr timeout (default 10s) |
| `FILMCLUB_VERSION` | — | Version string shown in `/readyz`, diagnostics, labels |
| `DEV_BYPASS_USER` | — | **Development only** — skips Plex auth for everyone |

`DATA_DIR` (default `/data`) and `PORT` (default `8000`) are operational. Never
enable `DEV_BYPASS_USER` in a deployment.

### Authorization model

Any Plex account in the world can authenticate — that alone is **not** club
membership. Access to *your* server is the real signal: on login the app confirms
your `PLEX_MACHINE_ID` appears in that account's Plex resources and rejects anyone
else. The signed, HttpOnly session cookie holds only the local member id and Plex
UUID. To write a member's own rating back to Plex, their token is **encrypted at
rest** with a key derived from `SESSION_SECRET`; it is never returned by an API or
placed in a cookie. Rotating `SESSION_SECRET` invalidates stored tokens and
requires members to sign in again.

### Plex rating sync (optional)

Sync applies only to changes made after it's enabled; it never reconciles old
ratings. Film Club saves locally first, then best-effort writes to Plex; a Plex
failure never discards the Film Club rating. Each member can pause/resume sync
from their profile. For Plex → Film Club updates, set `PLEX_WEBHOOK_SECRET` and
add this URL under Plex Webhooks (owner needs Plex Pass):

```text
https://your-app.example.com/api/plex/webhook/<PLEX_WEBHOOK_SECRET>
```

Webhook updates are accepted only for the configured server, a known member, a
TMDB/IMDb-matched movie, and a scheduled/watched film; unchanged echoes are
ignored, and clearing a Plex rating is not treated as a deletion.

## Data, migrations & backups

The database is `filmclub.db` in the `/data` volume. On startup the app applies
ordered, transactional migrations recorded in a `schema_migrations` table. Before
any migration it writes a **timestamped backup** under `/data/backups/` (never
overwriting an existing one).

- **Rollback:** stop the container, restore the matching `/data/backups/…` file
  over `filmclub.db` (remove any stale `-wal`/`-shm` sidecars), and start the
  previous image/tag. There are no down-migrations by design.
- **Manual backup:** copy `filmclub.db` (with its `-wal`/`-shm`, or stop the
  container first for a clean single-file copy).
- **Health:** `/healthz` is a liveness check; `/readyz` reports readiness
  (database reachable, schema current, durable secret) without exposing secrets.

## Running locally (without Docker)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# Dev mode skips Plex OAuth; keep the db out of /data.
export DATA_DIR=./devdata SESSION_SECRET=dev DEV_BYPASS_USER=Alice TMDB_API_KEY=yourkey
./.venv/bin/python -m app.seed --force        # optional: load demo data
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for tests
and conventions, and [`AGENTS.md`](AGENTS.md) for the authoritative architecture.

### Seed data

The seeder fabricates six members, ~20 films, randomized ratings, and the edge
cases the stats views need to be trustworthy (a film everyone has seen; films
with unknowns; watched films everyone/nobody had seen before; a small-sample
film). Seeded members use `dev:<Name>` ids, so `DEV_BYPASS_USER=Alice` logs you
in *as* seeded member Alice.

```bash
python -m app.seed          # refuses if data already exists
python -m app.seed --force  # wipe and reseed
```

## Deployment notes

- **Unraid / Compose:** point a `/data` volume at persistent storage (e.g.
  `/mnt/user/appdata/film-club-tracker:/data`), map a host port to container port
  `8000`, and set `APP_URL` to the address you actually browse to (OAuth redirects
  back there). Serve over HTTPS behind a reverse proxy or tunnel; don't expose a
  plain-HTTP instance publicly.
- **Maintainer workflow:** `deploy-from-mac.sh` packages the source and deploys it
  to an Unraid host over SSH, then health-checks. Its host/port/paths default to
  the maintainer's setup and are overridable via `UNRAID_HOST`, `UNRAID_USER`,
  `UNRAID_SSH_PORT`, `FILMCLUB_REMOTE_DIR`, `FILMCLUB_HTTP_PORT`, and
  `FILMCLUB_MAC_IP`. Run `./deploy-from-mac.sh --check` to validate prerequisites
  without deploying. Keep SSH LAN-only.

## Data model notes

`prior_views` (editable pre-watch eligibility) and `ratings.seen_before` (frozen
at watch time) are deliberately separate. When a film is scheduled, each member's
current `prior_views` state is snapshotted onto the movie; that snapshot seeds the
"had you seen this before?" default at rating time, so the historical fact can't
be rewritten by later prior-view edits.

## Contributing & security

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, tests, and conventions.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately.
- [`CHANGELOG.md`](CHANGELOG.md) — notable changes.

## License

[GNU AGPL-3.0-or-later](LICENSE). If you run a modified version as a network
service, you must offer your users the corresponding source.
