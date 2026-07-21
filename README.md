# Film Club Tracker

A small, self-hosted app for a weekly film club. Suggest films, record what
everyone has seen, second the ones you want to watch, pick a movie for the week,
rate and discuss it, and browse group stats — with an optional Plex integration
for library availability, deep links, and rating sync.

Runs as a single Docker container with a SQLite database on a bind-mounted
`/data` volume. Dark, poster-forward, no-build vanilla-JS frontend.

> **Status:** pre-1.0, self-hosted. Licensed under **AGPL-3.0-or-later**. Plex is
> optional; a published prebuilt image is still in progress — see `CHANGELOG.md`.

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
- Metadata header (poster/backdrop, director, U.S. content rating, runtime,
  discussion date for scheduled/watched films, genres, synopsis, suggester,
  and a Plex "Watch" link when available).
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
  load); original language and U.S. movie certification captured.
- **Plex** — OAuth login restricted to accounts with access to *your* server;
  "In Library" badges with deep links; Rotten Tomatoes critic/audience scores for
  matched films; and optional **two-way rating sync** (future changes only).
- **Seerr** *(optional)* — adding a film that isn't already on Plex submits a
  request to Overseerr/Jellyseerr. Degrades safely when off or unreachable.

**Admin** — member list, local-account invites and password resets,
admin grants, movie deletion, manual Plex refresh, and diagnostics
(version, schema, DB health, backups, integration status).

## Stack

FastAPI + SQLite (standard-library `sqlite3`, WAL mode), a no-build vanilla-JS
single-page frontend served by FastAPI, hand-rolled inline-SVG charts. `httpx`
for outbound calls, `itsdangerous` for signed cookies, `cryptography` for
encrypted per-member Plex tokens. No external database and no Node build step.

## Install with Docker Compose

### Prerequisites

Before starting, you need:

- A host with Docker Engine and Docker Compose v2 (`docker compose`), with Git
  installed. Linux, Unraid, and Docker Desktop are all suitable.
- A free TMDB API key for film search and metadata.
- Optionally, a Plex server and owner token reachable from the container.
- A persistent directory for the SQLite database. The included Compose file uses
  `./data`; replace that bind mount with an absolute host path if preferred.

Verify both Docker and the Compose plugin before cloning:

```bash
docker --version
docker compose version
```

Docker Desktop for Mac includes Compose. If you use Homebrew's Docker CLI with
Colima, install Compose separately and expose it in Docker's standard plugin
directory:

```bash
brew install docker-compose
mkdir -p ~/.docker/cli-plugins
ln -sf "$(brew --prefix)/lib/docker/cli-plugins/docker-compose" ~/.docker/cli-plugins/docker-compose
docker compose version
```

If `docker compose version` still fails, resolve that Docker installation issue
before continuing; `docker compose up` cannot fall back to the base Docker CLI.

### 1. Download and start Film Club Tracker

```bash
git clone https://github.com/crdenn/filmclub.git
cd filmclub
docker compose up -d --build
```

If host port 8000 is already occupied, choose another one for the install:

```bash
FILMCLUB_HTTP_PORT=8001 docker compose up -d --build
```

Use that same port in the browser and in the setup wizard's Film Club URL.

No configuration file is required. On first startup, the app generates a
durable master key and a one-time setup code. The code is printed once to the
container log; only a salted hash of it is stored in `/data`, it expires after
30 minutes, and repeated wrong guesses are rate-limited. If it expires before
you use it, restart the container to issue a fresh one. Read it from the log:

```bash
docker compose logs filmclub
```

Open <http://localhost:8000> (or the host address you mapped), enter the setup
code, create the first local owner, and complete the guided form. It validates
TMDB and, when configured, Plex and Seerr before saving. The Admin page also
has a **Test connections** action that checks the current unsaved form values,
including the Film Club URL format, without changing the saved configuration.
API keys and tokens are encrypted in SQLite; secret values are never returned
to the browser after saving.

The local owner is locked against demotion and can manage integrations, invite
members, issue password-reset links, and grant additional admins.

### 2. Verify the installation

```bash
docker compose ps
curl http://localhost:8000/readyz
```

Change the `curl` address if you mapped a different host port. Local accounts are
invite-only. If Plex login is enabled, only accounts with access to the
configured server are admitted.

If startup fails, inspect `docker compose logs filmclub`. The most common causes
are a Plex URL that the container cannot reach or a Film Club URL that differs
from the address used in the browser.

### Unraid and other Docker hosts

The included Compose file is portable. On Unraid, Docker Compose Manager, or a
manually created container, use these equivalent settings:

- Build from this repository's `Dockerfile`.
- Publish the desired host port to container port `8000`.
- Bind a persistent host directory such as
  `/mnt/user/appdata/filmclub/data` to `/data`.
- Use the container health endpoint `/healthz` and restart policy
  `unless-stopped`.

There is not yet a published prebuilt image, so the current release must be
built from the cloned source.

### Configuration

The setup wizard and Admin screen manage normal application configuration:

| Variable | Required | Purpose |
|---|:---:|---|
| `TMDB_API_KEY` | ✓ | Film search and metadata |
| `PLEX_URL` | — | Plex server base URL; set with token and machine ID |
| `PLEX_TOKEN` | — | Plex server token for enrichment |
| `PLEX_MACHINE_ID` | — | Plex login authorization server |
| `APP_URL` | ✓ | OAuth callback target; must match how you open the app |
| `PLEX_WEBHOOK_SECRET` | — | Enables inbound Plex→Film Club rating webhooks |
| `PLEX_REFRESH_INTERVAL` | — | Seconds between library refreshes (default 3600) |
| `SEERR_URL`, `SEERR_API_KEY` | — | Enable Seerr auto-request when both are set |
| `SEERR_TIMEOUT` | — | Per-request Seerr timeout (default 10s) |

For automated or legacy deployments, environment variables override values
saved through the UI. [`.env.example`](.env.example) documents those advanced
overrides, including `SESSION_SECRET`, `PLEX_CLIENT_ID`, `ADMIN_PLEX_IDS`,
`DATA_DIR`, `PORT`, `FILMCLUB_VERSION`, and development-only
`DEV_BYPASS_USER`. UI fields backed by environment variables are shown as
locked. Never enable `DEV_BYPASS_USER` in production.

### Authorization model

Local membership is invite-only. Admins create expiring, single-use invite and
password-reset links; only SHA-256 token hashes are retained. A local member can
optionally link Plex from their profile, and future Plex logins resolve to the
same member and history. For Plex login, authentication alone is not membership:
the app confirms your `PLEX_MACHINE_ID` appears in that account's resources.
Sessions are **revocable and server-side**: the HttpOnly cookie carries an
opaque random token, and the database stores only its SHA-256 hash and an expiry,
so logout and admin-wide invalidation kill a session immediately (a stolen cookie
can be revoked). To write a member's own rating back to Plex, their token is **encrypted at
rest** with a key derived from the durable data key (`SESSION_SECRET` if set,
otherwise the generated `/data/master.key`); it is never returned by an API or
placed in a cookie. Cookie/session signing uses a *separate*, self-provisioned
`/data/session.key`, so rotating the signing secret only forces a re-login and
never touches encrypted data. Rotating the data key does invalidate stored tokens
and requires members to sign in again.

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

The database is `filmclub.db` in the `/data` volume. The same volume contains
`master.key`, which is required to decrypt saved integration secrets and member
Plex tokens. On startup the app applies
ordered, transactional migrations recorded in a `schema_migrations` table. Before
any migration it writes a **timestamped backup** under `/data/backups/` (never
overwriting an existing one).

- **Admin backup:** open **Admin → Backup & restore** and select **Download
  backup**. The resulting `.filmclub-backup` file contains a consistent online
  database snapshot and its data-encryption key, so it can be restored on a new
  installation. Treat the file as sensitive: it contains account data, password
  hashes, the effective application settings (including environment-backed
  settings), and the key protecting saved service credentials. Environment
  values configured on the destination still take precedence after restore.
- **Admin restore:** choose **Restore from file**, select a Film Club backup, and
  type `RESTORE`. The app checks the archive checksum, database integrity,
  foreign keys, and schema compatibility before changing anything. It saves the
  current database under `/data/backups/`, restores the uploaded data, re-encrypts
  secrets for this installation, and signs everyone out.
- **Rollback:** stop the container, restore the matching `/data/backups/…` file
  over `filmclub.db` (remove any stale `-wal`/`-shm` sidecars), and start the
  previous image/tag. These server-side safety copies contain only the database
  and are intended for same-install rollback. There are no down-migrations.
- **Filesystem backup:** if backing up outside the Admin panel, copy the complete
  `/data` directory. Stop the container first, or capture `filmclub.db` together
  with its `-wal`/`-shm` files.
- **Health:** `/healthz` is a liveness check; `/readyz` reports readiness
  (database reachable, schema current, durable secret) without exposing secrets.

### Updating

Take a consistent backup as described above, then update and rebuild from the
repository:

```bash
docker compose down
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Startup applies any pending database migrations and creates a pre-migration
backup. Pin a release tag instead of tracking `main` if you prefer controlled
upgrades.

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

## Internet exposure

For access outside your trusted network, put the app behind an HTTPS reverse
proxy or private tunnel and set `APP_URL` to that public HTTPS origin. Do not
publish the plain HTTP container port directly to the internet. The
`deploy-from-mac.sh` and `deploy.sh` files are installation-specific maintainer
automation; they are not required for a normal install or upgrade.

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
