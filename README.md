# Film Club Tracker

A small self-hosted tracker for a weekly film club. Keep a running list of
suggestions, mark what you've watched, rate and discuss, and see who suggested
what. Built to run as a single Docker container on Unraid with a SQLite file on
a bind-mounted volume — backups are a file copy.

Picks are made ad hoc by group consensus. The app supports a lightweight
suggested → scheduled → watched workflow without enforcing a rotation.

## The one rule

> A film is only eligible to be picked if at least one member has **not** seen it.

The app enforces nothing, but it keeps the information visible:

- Every backlog card shows a simple seen tally — e.g. **"3/6 seen."**
- The movie detail page shows the full member breakdown and eligibility state.
- Members with no answer remain **unknown** (distinct from unseen) — an unanswered
  member is not evidence of eligibility.
- One-tap seen/unseen toggle on each card. It cycles: *unknown → seen → not
  seen → unknown*.
- Sort by unseen count; filter to eligible only.

## Features

- **Backlog** — poster grid of suggestions with a simple seen tally, suggester,
  one-tap seen toggle, and "mark watched."
- **Watched** — most-recent-first grid with average rating and rating coverage.
- **Movie detail** — full metadata including language and available Rotten
  Tomatoes critic/audience scores, a private rating input during the scheduled
  week, then all ratings grouped by first-watch vs rewatch after archival.
- **Stats** — watched-over-time, per-member rater profiles, a pairwise taste
  agreement matrix, divisiveness, first-watch-vs-rewatch deltas, suggester
  scorecards, genre/decade distribution, total runtime. All computed
  server-side with **small-n suppression** so thin data doesn't masquerade as
  signal.
- **TMDB** metadata, including original language, snapshotted locally on
  selection (no re-fetch per load).
- **Plex** Rotten Tomatoes critic and audience scores for matched library films.
- **Plex "In Library"** badges with a deep link, refreshed periodically.
- **Two-way Plex ratings** for future rating changes: Film Club's 0.5–5 score
  maps to Plex's 1–10 scale, and Plex rating webhooks update Film Club.
- **Seerr auto-request** (optional) — adding a film that isn't already on Plex
  submits a request to your Seerr (Overseerr/Jellyseerr) instance so it gets
  fetched automatically. Degrades safely: if Seerr is off or unreachable the
  film is still added.
- **Plex OAuth**, restricted to accounts with access to your Plex server.

## Stack

FastAPI + SQLite, a no-build vanilla-JS single-page frontend served by FastAPI,
hand-rolled SVG charts. No external database, no Node build step.

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example)
for the annotated list. Summary:

| Variable | Required | Purpose |
|---|:---:|---|
| `TMDB_API_KEY` | ✓ | Film metadata and search |
| `PLEX_URL` | ✓ | Library enrichment |
| `PLEX_TOKEN` | ✓ | Library enrichment |
| `PLEX_MACHINE_ID` | ✓ | Auth: the server-access check |
| `PLEX_CLIENT_ID` | ✓ | Stable UUID for the OAuth flow (auto-generated if blank) |
| `APP_URL` | ✓ | OAuth callback target; must match how you open the app |
| `SESSION_SECRET` | ✓ | Cookie signing |
| `PLEX_WEBHOOK_SECRET` | — | Secret URL component for inbound Plex rating webhooks |
| `DEV_BYPASS_USER` | — | Skip Plex OAuth during development |
| `PLEX_REFRESH_INTERVAL` | — | Seconds between library refreshes (default 3600) |
| `SEERR_URL` | — | Seerr base URL; enables auto-request when set with the key |
| `SEERR_API_KEY` | — | Seerr API key (Settings → General). Feature off if blank |
| `SEERR_TIMEOUT` | — | Per-request timeout in seconds (default 10) |

### Seerr auto-request

If `SEERR_URL` and `SEERR_API_KEY` are set, adding a suggestion that isn't
already on Plex will submit a movie request to Seerr (using the TMDB id the app
already has). Existence is checked cache-first against the periodically-refreshed
Plex library set; on a miss the app does one targeted **live** Plex lookup to
catch very recent additions before requesting. The request is fire-after-insert
and never blocks the add — if Seerr is unreachable the film is still added and
the card shows a "Request failed" badge. Seerr's own de-dup (already
requested/available) is respected, and the outcome is surfaced as a toast and a
badge (`Requested` / `On Seerr` / `Request failed`). Leave the vars unset to
disable entirely — behaviour is then identical to before.

### Authorization model

A valid Plex account is **not** the same as club membership — any Plex account
in the world authenticates. Access to *your* server is the real signal. On
login the app confirms your server's `PLEX_MACHINE_ID` appears in the user's
Plex resource list and rejects anyone else. The signed, HttpOnly session cookie
holds only the local member id and durable Plex uuid. To write a member's own
rating back to Plex, the OAuth token is encrypted at rest in SQLite using a key
derived from `SESSION_SECRET`; it is never returned by an API or placed in the
cookie. Rotating `SESSION_SECRET` requires members to sign in again.

### Plex rating sync

Rating sync applies only to changes made after this feature is enabled; it does
not reconcile old ratings. Film Club saves locally first, then attempts to rate
the same library item in Plex using the signed-in member's token. A Plex failure
does not discard the Film Club rating. Ordinary Plex logins now retain this token
automatically. Sessions created before rating sync was added are invalidated so
those members return through the normal Plex login once; their existing Film
Club profile, suggestions, and ratings stay attached to the same member row.
Each member can pause or resume future synchronization from the compact toggle
on their profile; pausing affects both Film Club → Plex and Plex → Film Club.

For Plex → Film Club updates, set `PLEX_WEBHOOK_SECRET` to a long random value
and add this URL under Plex Webhooks:

```text
https://your-filmclub.example.com/api/plex/webhook/<PLEX_WEBHOOK_SECRET>
```

Webhook updates are accepted only for this configured Plex server, a known
member, a TMDB/IMDb-matched movie, and a movie currently scheduled or watched.
Plex 1–10 ratings are divided by two. Echoes of an unchanged score are ignored.
Clearing a Plex rating is not currently treated as deleting a Film Club rating.
The server owner needs Plex Pass for webhooks, and each member should enable
Plex's “Sync My Watch State and Ratings” account setting.

## Running with Docker (recommended)

```bash
cp .env.example .env
# edit .env — fill in the required values
docker compose up -d --build
```

Then open `APP_URL` in a browser. First login auto-provisions your member row
from your Plex account (avatar included).

### On Unraid

- Point the `/data` volume at `/mnt/user/appdata/filmclub`.
- Map a host port to container port `8000`.
- Set `APP_URL` to the address you actually browse to (host IP + mapped port,
  or your reverse-proxy hostname). OAuth will redirect back there.
- Set the required environment variables in the template.

The SQLite database is `filmclub.db` inside the data volume. Back it up by
copying that file (WAL mode is enabled; copy the `.db`, `.db-wal`, `.db-shm`
together, or stop the container first for a clean single-file copy).

### One-command deployment from the Mac

`deploy-from-mac.sh` automates the existing LAN deployment pattern: it runs
the focused checks, rebuilds the sensitive source bundle, serves it from the
Mac, connects to Unraid over SSH, has Unraid pull the bundle, runs `deploy.sh`,
and waits for `/healthz`.

One-time setup: enable SSH on the Unraid LAN interface and authorize the Mac's
existing key. Once SSH is listening, run this once and enter the Unraid root
password when prompted:

```bash
./deploy-from-mac.sh --install-key
```

After that, deploy any future change from the project directory with:

```bash
./deploy-from-mac.sh
```

To verify SSH and local prerequisites without deploying or restarting the app:

```bash
./deploy-from-mac.sh --check
```

Defaults are the current installation (`root@192.168.1.152:2222`, remote
directory `/mnt/user/appdata/filmclub`, Mac port `8888`). They can be overridden
with `UNRAID_HOST`, `UNRAID_USER`, `UNRAID_SSH_PORT`, `FILMCLUB_REMOTE_DIR`,
`FILMCLUB_HTTP_PORT`, or `FILMCLUB_MAC_IP`. SSH should remain LAN-only; do not
forward its port publicly.

## Running locally (without Docker)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# Dev mode skips Plex OAuth. DATA_DIR keeps the db out of /data.
export DATA_DIR=./devdata SESSION_SECRET=dev DEV_BYPASS_USER=Alice TMDB_API_KEY=yourkey
./.venv/bin/python -m app.seed --force        # optional: load demo data
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

## Seed data

The seed script fabricates six members, ~20 films, randomized ratings, and a
realistic mix of prior-view / seen-before states — including the edge cases the
stats views need in order to be trustworthy:

- a film everyone has seen (ineligible),
- a film with unknowns (not just yes/no),
- a watched film everyone had seen before, and one nobody had (first/rewatch
  delta must suppress, not divide by zero),
- a film with only one or two ratings (small-n suppression must fire).

```bash
python -m app.seed          # refuses if data already exists
python -m app.seed --force  # wipe and reseed
```

The seeded members use `dev:<Name>` ids, so running with `DEV_BYPASS_USER=Alice`
logs you in *as* seeded member Alice rather than creating a stray seventh person.

## Data model notes

`prior_views` (editable, pre-watch eligibility) and `ratings.seen_before`
(frozen at watch time) are deliberately separate. When a film is marked watched,
each member's current `prior_views` state is snapshotted onto the movie; that
snapshot seeds the "had you seen this before?" default when they rate, so the
historical fact can't be rewritten by later prior-view edits.
