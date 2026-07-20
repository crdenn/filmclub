# Architectural and product decisions

This file records decisions visible in the current repository. “Confirmed” means the owner or explicit product documentation established the decision. “Inferred” means the implementation strongly suggests it; the historical reasoning is not known.

## Use a single-container monolith with SQLite

- **Decision:** Serve the API and frontend from one FastAPI process and persist to a bind-mounted SQLite file.
- **Evidence:** `app/main.py` serves `/static` and `/`; `app/db.py` uses `sqlite3`; `Dockerfile` declares `/data`; `deploy.sh` binds the Unraid appdata directory.
- **Likely reasoning:** The club is small, traffic is low, deployment should remain simple, and backup should be a file copy.
- **Consequences:** Very low operational overhead and no separate database service. Write scalability, horizontal replicas, online schema management, and shared background state are limited.
- **Status:** Confirmed.

## Invite-only local accounts with an identity model

- **Decision:** Decouple accounts from Plex with an `identities` table mapping `plex`/`local` login methods to member rows. Local accounts are gated by single-use, expiring, admin-issued invites (no open registration, no SMTP). Passwords use stdlib `hashlib.scrypt`, not Argon2id.
- **Evidence:** `app/accounts.py`, `app/passwords.py`, migration `4` (`identities`, `invites`), and the `/auth/local/*` + `/api/admin/invites` endpoints. A local account is a member row with a synthetic `local:<random>` plex_id plus a `local` identity holding the scrypt hash.
- **Likely reasoning:** The roadmap specified Argon2id, but the owner chose scrypt to keep the deliberately minimal dependency set and the no-build, multi-arch Docker image (scrypt is stdlib and memory-hard, sufficient for a small private club). The synthetic plex_id avoids rebuilding the live `members` table while satisfying its `NOT NULL UNIQUE` constraint.
- **Consequences:** Members can log in without Plex; existing Plex members are grandfathered (their ids and data preserved). Only an invite code's SHA-256 hash is stored. Plex-account linking, password reset, and the account UI are follow-up work.
- **Status:** Confirmed by the owner (scrypt over Argon2id).

## Include a weekly scheduling lifecycle

- **Decision:** Movies move from backlog to a current scheduled selection and then to the watched archive.
- **Evidence:** `service.schedule_movie()`, `archive_movie()`, `unschedule_movie()`, `unmark_watched()`, and `return_to_this_week()`; `/api/thisweek`; the `#/thisweek` UI.
- **Likely reasoning:** The club needs a clear current selection and discussion date alongside the long-term backlog.
- **Consequences:** The model has three statuses and non-destructive reversal behavior. `watched_at` represents both scheduled discussion and archive date. A direct watched-to-scheduled correction is admin-only; backlog reversals retain history. The README's older “no scheduling” statement is obsolete.
- **Status:** Confirmed by the owner.

## Treat unknown prior-view state separately from unseen

- **Decision:** A missing answer does not establish eligibility.
- **Evidence:** `service._coverage()` places missing rows in `unknown_ids`; it returns `eligible` only when `not_seen` is nonempty.
- **Likely reasoning:** The club's rule requires positive evidence that someone has not seen the movie.
- **Consequences:** UI and filters require three states, and code must not coerce missing rows to false.
- **Status:** Confirmed.

## Separate mutable eligibility answers from historical watch context

- **Decision:** Store editable `prior_views.seen` separately from `ratings.seen_before`, and snapshot prior views when scheduling.
- **Evidence:** Separate schema tables/columns, `movies.seen_before_snapshot`, `schedule_movie()`, and `default_seen_before()`.
- **Likely reasoning:** Members may watch backlog films independently later; those edits must not rewrite whether a club viewing was their first watch.
- **Consequences:** More persistence complexity and merge edge cases, but historical ratings retain meaning.
- **Status:** Confirmed in README and code.

## Snapshot TMDB metadata locally

- **Decision:** Use TMDB for discovery and selection, then store the full movie metadata in SQLite.
- **Evidence:** `tmdb.search()`, `tmdb.details()`, and `service.add_suggestion()`.
- **Likely reasoning:** Fast, stable page loads and reduced dependency on TMDB availability/rate limits.
- **Consequences:** Existing movies continue to render if TMDB is unavailable, but metadata becomes stale unless an explicit refresh feature is added.
- **Status:** Confirmed.

## Authorize by access to one Plex server

- **Decision:** Successful Plex authentication is insufficient; the account must have access to the configured server.
- **Evidence:** `plex.has_server_access()` and the mandatory check in `auth_callback()`.
- **Likely reasoning:** Plex server sharing is the existing membership boundary.
- **Consequences:** No separate invite/club-membership system is needed. Plex configuration or resource API failure blocks production login.
- **Status:** Confirmed.

## Encrypt Plex user tokens for per-member rating writes

- **Decision:** Persist each OAuth token encrypted in SQLite while keeping the signed cookie limited to member identity.
- **Evidence:** `members.plex_token_encrypted`, `app/token_crypto.py`, and the callback's member upsert.
- **Likely reasoning:** Plex rating writes must be performed as the member, while raw tokens should not be exposed to the browser or stored as plaintext.
- **Consequences:** Ciphertext is keyed on the durable data key (`config.DATA_KEY` — the `SESSION_SECRET` env value if set, otherwise the generated `/data/master.key`), which is deliberately separate from the cookie-signing secret (`/data/session.key`). Rotating the signing secret only forces a re-login; rotating the data key makes existing ciphertext unreadable and members must sign in again. The database still contains sensitive encrypted material and requires normal backup/access protection.
- **Status:** Confirmed by owner direction and implementation.

## Make rating synchronization future-only and local-first

- **Decision:** Sync new rating changes in both directions without reconciling historical ratings. Commit Film Club changes before attempting Plex, and let each member pause both directions from their profile.
- **Evidence:** `app/plex_ratings.py`, the rating API response, and the `media.rate` webhook route.
- **Likely reasoning:** Avoid an ambiguous one-time conflict merge and ensure an optional integration cannot lose the club's local record.
- **Consequences:** Existing scores remain untouched. Ordinary logins retain the token automatically; sessions created before this feature are invalidated so those members pass through normal login once and populate the missing token without losing their Film Club data. The preference defaults on and can be paused without deleting the encrypted token. Plex webhook delivery and account rating-sync settings are operational prerequisites. Rating deletion is not synchronized.
- **Status:** Confirmed by owner direction.

## Combine declarative owners with database admins

- **Decision:** Effective admin status is the database flag OR membership in `ADMIN_PLEX_IDS`; allowlisted owners cannot be demoted or merged away.
- **Evidence:** `auth._with_effective_admin()`, `service.set_member_admin()`, and `service.merge_members()`.
- **Likely reasoning:** Preserve an emergency owner account through database resets while allowing in-app delegation.
- **Consequences:** Admin state has two sources and environment changes can override what the database appears to say.
- **Status:** Confirmed by code; historical reasoning inferred.

## Allow ordinary members to manage the weekly lifecycle

- **Decision:** Scheduling, date editing, archiving, and backlog reversals require authentication but not admin status. The direct watched-to-scheduled correction is admin-only.
- **Evidence:** Ordinary lifecycle routes depend on `auth.current_member`; `/return-to-this-week` depends on `auth.require_admin`.
- **Likely reasoning:** The group operates by trust and consensus, while the corrective shortcut is reserved for administration/testing.
- **Consequences:** Any member can perform group-wide transitions, but those reversals retain movie history. Only an admin can reopen an archived movie directly as the current selection.
- **Status:** Confirmed by the owner.

## Permit more than one scheduled movie at the data layer

- **Decision:** No unique constraint or service check limits the current selection to one movie.
- **Evidence:** `schedule_movie()` updates only the requested movie, while `this_week()` returns a list.
- **Likely reasoning:** Possibly flexibility for skipped weeks/double features, or simply an unenforced invariant. Historical intent is unknown.
- **Consequences:** Multiple current selections can coexist and appear in the API/UI.
- **Status:** Inferred and uncertain.

## Use a build-free vanilla JavaScript frontend

- **Decision:** Render a hash-routed SPA from one JavaScript file and one CSS file without a compilation step.
- **Evidence:** `static/app.js`, `static/styles.css`, `static/index.html`, and absence of a Node manifest.
- **Likely reasoning:** Minimize dependencies and Docker/development complexity.
- **Consequences:** Simple delivery and debugging, but a large manually organized file, no component tooling, and manual cache versioning.
- **Status:** Confirmed.

## Compute statistics on the server with explicit small-n rules

- **Decision:** Return completed statistics and confidence metadata from `app/stats.py` instead of calculating them in the browser.
- **Evidence:** `stats.compute()` and thresholds `MIN_FILMS_PER_MEMBER`, `MIN_RATERS_PER_FILM`, and `MIN_OVERLAP`.
- **Likely reasoning:** Keep statistical definitions consistent and prevent plausible-looking thin data from being overinterpreted.
- **Consequences:** The API response is specialized to the current Stats UI; threshold changes are backend decisions.
- **Status:** Confirmed.

## Use broad SSE invalidation instead of entity-level synchronization

- **Decision:** Broadcast that a mutation occurred and have other clients re-fetch.
- **Evidence:** `BroadcastMiddleware`, `app/events.py`, and `scheduleRemoteRefresh()` in `static/app.js`.
- **Likely reasoning:** The dataset is small, so correctness and simplicity matter more than minimizing read volume.
- **Consequences:** Straightforward convergence, but extra queries and no cross-worker propagation.
- **Status:** Inferred.

## Keep Plex library state in process memory

- **Decision:** Periodically load Plex GUIDs into module-level sets and maps.
- **Evidence:** `_library` and `refresh_loop()` in `app/plex.py`.
- **Likely reasoning:** A small catalog and single process do not justify another persistent cache.
- **Consequences:** Fast lookups and no new service; cold starts, stale intervals, and multi-worker inconsistency remain.
- **Status:** Inferred.

## Make Seerr automation optional and subordinate to adding a suggestion

- **Decision:** Insert the movie first; if configured and not on Plex, request it from Seerr without allowing failure to break the add.
- **Evidence:** `_maybe_request_from_seerr()` and exception/status behavior in `app/seerr.py`.
- **Likely reasoning:** Tracking the club's choice is core; media automation is convenience.
- **Consequences:** Suggestions may remain with `failed` request status and require manual action. Degraded external checks can still cause a redundant request, relying on Seerr deduplication.
- **Status:** Confirmed.

## Use inline additive schema migrations

- **Decision:** Create the current schema idempotently, then guard individual `ALTER TABLE ADD COLUMN` steps.
- **Evidence:** `db.init_db()` and `db._migrate()`.
- **Likely reasoning:** The schema is small and has only needed additive changes so far.
- **Consequences:** Minimal tooling, but no version history, rollback, migration ordering record, or robust path for complex transformations.
- **Status:** Inferred.

## Use Unraid as the authoritative deployment

- **Decision:** Treat the Mac project as source of truth; package and orchestrate from `deploy-from-mac.sh`, then build and replace a named Docker container on Unraid with persistent data under appdata.
- **Evidence:** `deploy-from-mac.sh`, `deploy.sh`; owner confirmation and successful SSH readiness check.
- **Likely reasoning:** This matches the current host environment.
- **Consequences:** Routine deployment is one Mac command with no Unraid-terminal copy/paste. LAN SSH access and the Mac HTTP endpoint are required; current host/port/path defaults remain installation-specific but are environment-overridable.
- **Status:** Confirmed.

## Store normal configuration through the application UI

- **Decision:** Fresh installations use a setup-code-protected browser wizard; admins subsequently manage integrations in the Admin screen. Sensitive values are encrypted in SQLite using a durable key generated under `/data`. Explicit environment variables remain higher-precedence automation overrides.
- **Evidence:** `app/settings.py`, migration v2, setup/admin routes in `app/main.py`, and the setup/admin forms in `static/app.js`.
- **Likely reasoning:** Operators should be able to install and configure the product without editing files while existing automated deployments remain compatible.
- **Consequences:** The complete `/data` directory, including `master.key`, must be backed up. The first authorized Plex login after setup becomes the non-demotable owner. Environment-overridden fields are read-only in the UI.
- **Status:** Confirmed and implemented.
