# Project state

Generated: 2026-07-19

Git branch: unavailable; this workspace has no `.git` metadata.

Git commit: unavailable for the same reason.

## Purpose and current scope

Film Club Tracker makes a small film club's shared backlog and weekly selection visible. It tracks eligibility based on members' prior-view answers, supports ad hoc seconding and selection, retains a watched archive with ratings, and calculates group statistics. It is currently deployed as a self-hosted Docker container on Unraid.

The scheduling workflow in the code and README is authoritative.

## Implementation status terminology

- **Implemented** means the backend and corresponding UI/code path exist.
- **Exercised locally** means runtime logs or the local database show the app has been run, not that every workflow was verified.
- **Tested** is reserved for automated coverage. Focused Plex rating-sync tests are present; most features are not covered.
- **Inferred** marks conclusions based on code or artifacts that the owner has not confirmed.

## Features that appear fully implemented

- Plex PIN login, server-access authorization, member auto-provisioning, signed sessions, and logout.
- Development authentication bypass for local work.
- User display-name editing and public member activity profiles, with member links across film, coverage, rating, and statistics views.
- TMDB search and full metadata snapshot, including original language, when adding a suggestion; existing rows are backfilled once in the background.
- Backlog grid/list views, sorting, client filters, coverage display, and seen/unseen/unknown cycling.
- Eligibility classification with unknown kept separate from unseen.
- Suggestion seconding with suggester self-votes blocked.
- “This week” selection, discussion-date editing, private rating during the week, and automatic group reveal at the archive transition.
- Watched archive and movie detail with average, first-watch, and rewatch ratings.
- Non-destructive lifecycle reversals, including an admin-only Watched-to-This-Week correction.
- Reminder badges for unanswered backlog items and unrated archived films.
- Server-side statistics with explicit small-sample disclosure/suppression.
- Admin member list, placeholder merging, admin grants, movie deletion, and manual Plex refresh.
- Plex library badges, deep links, and available Rotten Tomatoes critic/audience scores backed by a periodic in-memory cache.
- Optional Seerr request flow with Plex cache/live checks and failure-tolerant behavior.
- Future-only, two-way Plex rating sync with encrypted per-member tokens and local-first failure handling.
- One-command Mac-to-Unraid deployment over LAN HTTP and SSH, with preflight checks and a post-deploy health check.
- Live cross-client refresh notifications through SSE.
- Responsive, build-free frontend and Docker image health check.

These are implemented code paths, not claims that each works in the current production environment.

## Features or work that are partial

- **Portable distribution:** Docker packaging and one-command Mac-to-Unraid deployment exist, but defaults remain site-specific and there is no end-user setup/admin configuration flow.
- **Admin-managed configuration:** desired as a future capability, but all settings and secrets are currently environment variables.
- **Database migrations:** guarded additive column changes exist, but there is no versioned migration system.
- **Health monitoring:** `/healthz` confirms only that FastAPI can respond.
- **Runtime validation:** local logs and a populated local database show development execution, but current Plex, TMDB, Seerr, OAuth, and Unraid production behavior were not exercised during documentation work.
- **Plex rating sync:** focused integration-boundary tests pass, but real Plex user-token writes and webhook delivery require production verification. Legacy identity-only sessions are automatically invalidated; the next normal Plex login retains the required token.

## Stubbed or unclear areas

- The schema comment lists `scheduled`, and the implementation uses it, but there is no database CHECK constraint for status values.
- `MEMBER_COUNT_HINT` exists in configuration but is not an enforced club size.
- The current code permits multiple scheduled films. It is unclear whether this is intentional flexibility or an unenforced invariant.
- Any plan for secure database-backed settings, secret encryption, or environment overrides is not yet designed.

## Suspicious behavior and likely bugs

- `service.merge_members()` transfers suggestions, ratings, and prior views but not `votes`. Foreign-key cascade removes the source member's seconds.
- The server's `eligible_only` filter excludes only films classified `ineligible`; it includes `unconfirmed` films despite the parameter name.
- Multiple movies can be `scheduled` concurrently.
- Member merging does not rewrite member IDs embedded in existing `seen_before_snapshot` JSON. For a placeholder member merged after a movie was scheduled, the historical fallback can retain the deleted ID.
- The login error page interpolates internally supplied strings without HTML escaping. Present callers use fixed strings, so there is no current user-controlled injection path.
- FastAPI's `on_event("startup")` hook is deprecated in favor of lifespan handlers.
- Asset version numbers are manually maintained.

## Technical debt and incomplete areas

- Focused rating-sync tests exist; there is no broad suite, CI, static typing command, linting, or formatting enforcement.
- Route responses have no declared response models or API versioning.
- SQL and response assembly in `service.py` are becoming large and tightly coupled.
- Each small mutation commits independently, including multi-step lifecycle operations; failures between statements could leave partial state.
- Inline migrations have no transaction across all steps and no recorded schema version.
- Background Plex refresh and SSE fan-out are single-process only.
- External integration retry/backoff is minimal.
- No database-integrity or integration-aware health/readiness checks.
- Runtime and generated artifacts exist at the repository root.
- The completed one-time importer remains coupled to current source and contains hardcoded historical rows.

## Current architectural assumptions

- One small club, low write concurrency, and a few hundred movies.
- One Uvicorn worker and one container instance.
- SQLite on a persistent Unraid bind mount is the source of truth.
- Backups are file-level copies performed safely with WAL considerations.
- Members trust one another with ordinary weekly lifecycle actions.
- Plex server access is equivalent to club access.
- TMDB IDs are sufficiently unique for duplicate detection, although the schema does not enforce uniqueness.
- A selected movie's scheduled discussion date is also its retained watched date.
- Optional Plex enrichment and Seerr automation may fail without breaking core tracking.

## Important product and UX behavior

- Unknown prior-view state must never count as evidence that a movie is eligible.
- A movie everyone has seen is visually dimmed and marked ineligible.
- Ratings use 0.5 increments from 0.5 to 5.0.
- Ratings distinguish first watches from rewatches and accept optional notes.
- Rating sync is future-only, maps Film Club half-stars to Plex's ten-step scale, and does not sync rating deletion.
- Prior-view state is snapshotted on scheduling, not archiving.
- Members may rate the current selection before it is archived.
- The suggester cannot second their own movie.
- Remote changes refresh only the current page body, preserve the app shell and scroll position, and wait while a member is editing or has a modal open.
- Avatars use deterministic colored initials, even when Plex thumbnails exist.
- Stats deliberately mark/suppress thin samples.

## External dependencies and integrations

- **TMDB:** required for search and adding new movies; reads use stored metadata afterward.
- **Plex cloud API:** production login, user identity, and resource authorization.
- **Plex server:** periodic movie GUID scan, live existence check, and deep links.
- **Plex webhooks:** optional inbound `media.rate` delivery for per-member rating updates.
- **Seerr:** optional request automation and deduplication.
- **Browser EventSource:** live update channel.
- **SQLite:** persistent application data.
- **Docker/Unraid:** current deployment environment.

## Testing coverage and evidence

`tests/test_plex_ratings.py` uses standard-library `unittest` with isolated temporary databases and mocked Plex HTTP calls.

The focused suite verifies token encryption, safe connection-status exposure, per-member watched-state shaping, outbound writes and score conversion, no-token degradation, inbound matching/update behavior, preservation of Film Club-only rating context, and unchanged-echo suppression.

During the read-only audit:

- all 16 current Python and test files parsed successfully with `ast.parse`;
- `static/app.js` passed `node --check`;
- the local SQLite file opened read-only;
- its schema contained the five expected domain tables;
- `PRAGMA foreign_key_check` returned no violations;
- it contained six members, twenty movies, eighty ratings, zero votes, and twenty-four prior-view rows at inspection time;
- local logs showed development-bypass startup and ordinary HTTP traffic.

After the UI polish, an isolated headless Chrome session exercised Backlog, movie detail, the personal profile hub, progressive Stats sections, and 390-pixel profile/backlog layouts against a temporary database. A two-client check also verified that a remote vote repainted the second client's page body without a loading state, app-bar replacement, or horizontal overflow. These are browser assertions run during implementation, not a committed regression suite.

This evidence does not verify OAuth, external integrations, lifecycle edge cases, admin authorization, Docker builds, or Unraid deployment.

## Recommended next steps

### Immediate

1. Rotate any credentials present in `.claude/launch.json`, `.env`, or `filmclub-deploy.tar.gz`; remove secrets from generated/distributable artifacts in a separately authorized security cleanup.
2. Confirm whether multiple simultaneous scheduled films are intended.
3. Expand regression tests to eligibility, scheduling snapshots, member merge collisions, and authorization.

### Near-term

1. Fix member merge to handle votes and snapshot identities deliberately.
2. Define whether “eligible only” should include or exclude unconfirmed films, then align API naming and UI copy.
3. Add versioned, transactional migrations and a schema version.
4. Add linting, formatting, CI, and broader API/browser coverage around the documented test command.
5. Make the Unraid deployment script portable by removing site-specific hints and documenting volume/backup requirements.
6. Add database-aware readiness checks and graceful background-task shutdown.

### Later

1. Package and publish a reusable container and installation guide.
2. Design an admin configuration system with encrypted secret storage, environment-variable precedence, validation, and safe credential rotation.
3. Split `service.py` by domain if growth warrants it; avoid doing so as an unrelated refactor.
4. Move SSE/cache state to shared infrastructure only if multiple workers or replicas become necessary.
5. Consider explicit audit/history records for destructive or club-wide actions.

## Facts requiring owner confirmation

- `devdata/` is treated here as ignored local runtime/test material because it contains a local database, logs, a generated client ID, and is excluded by `.gitignore`/`.dockerignore`. The owner was unsure of its provenance.
- `filmclub-deploy.tar.gz` is treated as a generated deployment artifact, not source. It contains an `.env` with real values and should be handled as sensitive. Its intended retention/distribution policy remains unconfirmed.
- Whether the app should permit multiple current selections remains unconfirmed.
- Whether all members should retain lifecycle controls or those controls should be admin-only remains unconfirmed.

## Deliberately unchanged

The July 19 rating-sync work modified application code, schema/configuration examples, dependencies, frontend behavior, tests, and documentation. It did not modify production data, regenerate the deployment archive, remove the obsolete importer, or rotate existing credentials.
