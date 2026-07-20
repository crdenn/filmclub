# Feature inventory

Statuses describe visible implementation. Focused automated coverage exists for the Plex rating-sync boundary; most product features still lack automated regression coverage.

## Sign in and membership

### Plex authentication and club authorization

- **Purpose:** Admit Plex users who can access the configured club Plex server.
- **Main files:** `app/main.py`, `app/plex.py`, `app/auth.py`, `app/config.py`, `static/app.js`.
- **Status:** Implemented.
- **Dependencies:** Plex cloud PIN, user, and resources APIs; `PLEX_MACHINE_ID`; signed cookies.
- **Limitations:** Plex/API availability is required for production login. Configuration errors warn at startup rather than preventing boot. OAuth was not exercised during this audit.
- **Follow-up:** Add authentication/authorization integration tests and production configuration diagnostics.

### Automatic member provisioning

- **Purpose:** Create or refresh a local member record from the Plex identity.
- **Main files:** `app/auth.py`, `app/db.py`, `app/colors.py`, `app/schema.sql`.
- **Status:** Implemented.
- **Dependencies:** Successful Plex identity lookup.
- **Limitations:** Members are authorized through server access rather than an explicit club roster. Plex thumbnails are stored but intentionally not displayed.
- **Follow-up:** Clarify whether an explicit membership approval layer will ever be needed for public distribution.

### Development bypass

- **Purpose:** Allow local work without Plex OAuth.
- **Main files:** `app/config.py`, `app/auth.py`, `README.md`.
- **Status:** Implemented.
- **Dependencies:** `DEV_BYPASS_USER`.
- **Limitations:** Bypasses all authentication for anyone reaching the server; unsafe in production.
- **Follow-up:** Keep isolated to local/tool testing and add an unmistakable UI/environment warning if needed.

## Member identity and activity

### Personal profile hub

- **Purpose:** Let a member manage their display name, review personal activity and totals, and control future Plex rating sync.
- **Main files:** `app/main.py`, `app/service.py`, `app/db.py`, `static/app.js`.
- **Status:** Implemented.
- **Dependencies:** Authenticated member row.
- **Limitations:** The display name has a maximum length of 40 characters. The compact sync control exposes connection/preference booleans only, never the saved Plex token.
- **Follow-up:** None apparent.

### Public member profile

- **Purpose:** Show a member's suggestions, ratings, notes, and summary metrics.
- **Main files:** `service.member_profile()`, `/api/members/{id}/profile`, `renderMemberProfile()`.
- **Status:** Implemented.
- **Dependencies:** Movies and ratings attributed to a member.
- **Limitations:** Profiles are visible to every authenticated member; there are no privacy controls. Direct profile URLs fall back to Stats for return navigation because there is no prior in-app route.
- **Navigation:** Member names and coverage avatars link to profiles from film cards, the current selection, movie details, rating rows, and statistics. In-app visits retain the originating hash route for the Back link.
- **Follow-up:** Consider pagination only if history grows substantially.

### Reminder badges

- **Purpose:** Highlight backlog movies lacking a prior-view answer and watched movies lacking the current member's rating.
- **Main files:** `service.todo_counts()`, `/api/me/todo`, `refreshTodo()`.
- **Status:** Implemented.
- **Dependencies:** Prior views, ratings, current member.
- **Limitations:** Scheduled movies are not counted as unrated in the watched badge.
- **Follow-up:** Decide whether the current selection should have its own reminder.

## Discover and suggest films

### TMDB search

- **Purpose:** Find movies by title, review full metadata in an expandable result, and explicitly confirm before adding one.
- **Main files:** `app/tmdb.py`, `/api/tmdb/search`, `/api/tmdb/movies/{tmdb_id}`, `openSearchModal()`.
- **Status:** Implemented.
- **Dependencies:** `TMDB_API_KEY` and TMDB availability.
- **Behavior:** Selecting a result fetches its full TMDB details without mutating the backlog. The result expands in place with facts, genres, synopsis, and an **Add to backlog** button; only that button creates the suggestion and closes the dialog.
- **Limitations:** Director lookup adds parallel requests; search returns at most six results and fails as a whole when the main TMDB request fails. Previewing makes one additional TMDB details request, and adding re-fetches the metadata before snapshotting it.
- **Follow-up:** Add caching or rate-limit handling only if operational evidence warrants it.

### Add suggestion and metadata snapshot

- **Purpose:** Add a selected film to the backlog with durable metadata, original language, and suggester attribution.
- **Main files:** `api_add_movie()`, `tmdb.details()`, `service.add_suggestion()`, `movies` table.
- **Status:** Implemented.
- **Dependencies:** TMDB details endpoint.
- **Limitations:** Duplicate detection is in application code by TMDB ID; the database has no unique constraint. Existing metadata is not refreshed.
- **Follow-up:** Add database-level duplicate protection if concurrent adds become possible.

### Rotten Tomatoes enrichment

- **Purpose:** Show critic and audience percentages on movie views other than the backlog when Plex supplies Rotten Tomatoes metadata.
- **Main files:** `app/plex.py`, `service._in_library()`, metadata helpers in `static/app.js`.
- **Status:** Implemented and exercised against the configured Plex server.
- **Dependencies:** A matched film in the Plex library and Rotten Tomatoes fields from the library's Plex metadata agent.
- **Limitations:** Scores are omitted for films outside Plex or when Plex does not supply them; no separate ratings service is queried.
- **Follow-up:** None apparent.

### Seerr auto-request

- **Purpose:** Request a newly suggested movie when it is absent from Plex.
- **Main files:** `_maybe_request_from_seerr()`, `app/seerr.py`, `app/plex.py`, UI status badges.
- **Status:** Implemented, optional.
- **Dependencies:** Both Seerr variables; TMDB ID; Plex cache/live lookup.
- **Limitations:** Integration was not exercised during the audit. Failed requests are stored but there is no retry button. A failed Plex lookup may defer deduplication to Seerr.
- **Follow-up:** Add admin retry/status diagnostics if failures occur in practice.

## Backlog triage and eligibility

### Prior-view answers

- **Purpose:** Record whether each member has already seen a backlog/current movie.
- **Main files:** `prior_views`, `service.set_prior_view()`, `/api/movies/{id}/prior_view`, seen controls in `static/app.js`.
- **Status:** Implemented.
- **Dependencies:** Authenticated member and movie.
- **Limitations:** The API allows updates for movies in any status, although archived detail uses snapshot/rating data instead of live prior views.
- **Follow-up:** Consider restricting irrelevant archived updates at the API boundary.

### Eligibility calculation

- **Purpose:** Surface whether at least one member has not seen a film.
- **Main files:** `service._coverage()`, backlog/detail renderers and CSS.
- **Status:** Implemented.
- **Dependencies:** Complete member list and prior-view rows.
- **Limitations:** Backlog cards intentionally show only an `X/Y seen` tally; the eligibility label and member-level breakdown are on movie detail. Every local member, including placeholders, affects totals and eligibility until merged or removed.
- **Follow-up:** Test zero-member and placeholder-member behavior explicitly.

### Backlog browsing, sorting, and filtering

- **Purpose:** Browse suggestions by poster/list view, coverage, suggester, year, runtime, title, date, unseen count, or seconds.
- **Main files:** `service.backlog()`, `service._sort_backlog()`, `renderBacklog()` and related client functions.
- **Status:** Implemented.
- **Dependencies:** Movie metadata, members, votes, prior views.
- **Limitations:** Server `eligible_only` includes unconfirmed movies. Several filters are client-side after one API load.
- **Follow-up:** Rename/define the eligible filter behavior and add tests for all sort modes.

### Seconding suggestions

- **Purpose:** Let members express support for another member's backlog suggestion.
- **Main files:** `votes`, `service.set_vote()`, `/api/movies/{id}/vote`, vote controls.
- **Status:** Implemented.
- **Dependencies:** Backlog movie and authenticated member.
- **Limitations:** Suggesters cannot second their own film. Member merge currently drops the source member's votes.
- **Follow-up:** Correct merge behavior and cover unique-collision cases.

### Delete a backlog suggestion

- **Purpose:** Let the suggester or an admin remove an erroneous backlog entry.
- **Main files:** `api_delete_movie()`, `service.delete_movie()`, backlog/detail UI.
- **Status:** Implemented.
- **Dependencies:** Ownership or admin status.
- **Limitations:** Ordinary endpoint is restricted to `suggested`; deletion cascades related data.
- **Follow-up:** Add audit/history only if deletion accountability becomes important.

## Weekly selection

### Schedule a movie

- **Purpose:** Promote a backlog movie to the current selection and assign the next Tuesday discussion date.
- **Main files:** `service._next_tuesday()`, `service.schedule_movie()`, `/api/movies/{id}/schedule`, detail UI.
- **Status:** Implemented.
- **Dependencies:** Suggested movie and prior-view state.
- **Limitations:** Any authenticated member may schedule. Eligibility is displayed but not enforced. Multiple movies can be scheduled.
- **Follow-up:** Confirm desired authorization, multiplicity, and whether eligibility should ever block scheduling.

### This week view

- **Purpose:** Present current selection metadata, discussion date, Plex link, coverage, and live rating tally.
- **Main files:** `service.this_week()`, `/api/thisweek`, `renderThisWeek()` and `thisWeekHero()`.
- **Status:** Implemented.
- **Dependencies:** One or more `scheduled` movies.
- **Limitations:** API intentionally returns a list; UX behavior with multiple items needs explicit verification.
- **Follow-up:** Test empty, single, and multiple-selection states.

### Edit discussion date

- **Purpose:** Move the meeting away from the automatically selected Tuesday.
- **Main files:** `service.set_discuss_date()`, `/api/movies/{id}/discuss_date`, date input in the current view.
- **Status:** Implemented.
- **Dependencies:** Scheduled movie and browser date input.
- **Limitations:** Any authenticated member may edit; date has format validation but no past/future business rule.
- **Follow-up:** Confirm intended permissions and date constraints.

### Archive after discussion

- **Purpose:** Move the current selection into watched history while retaining its date, snapshot, and ratings.
- **Main files:** `service.archive_movie()`, `/api/movies/{id}/watch`, current/detail UI.
- **Status:** Implemented.
- **Dependencies:** Scheduled status.
- **Limitations:** Does not require all members to rate or watch. Any authenticated member may archive.
- **Follow-up:** Confirm whether this trust model is desired.

### Reverse scheduling or archive

- **Purpose:** Return a scheduled/watched movie to the backlog, or let an admin reopen a watched movie directly as This Week.
- **Main files:** `service.unschedule_movie()`, `service.unmark_watched()`, `service.return_to_this_week()`, corresponding endpoints and confirmation UI.
- **Status:** Implemented.
- **Dependencies:** Matching source status.
- **Behavior:** Reversals change only `movies.status`; ratings, notes, discussion date, prior-view snapshot, prior views, votes, and metadata are retained. The direct `watched -> scheduled` correction is admin-only; backlog reversals remain available to authenticated members.
- **Limitations:** Re-picking a backlog film recalculates its discussion date and prior-view snapshot, while retaining existing ratings.
- **Follow-up:** None apparent.

## Ratings and watched history

### Rate scheduled or watched movies

- **Purpose:** Record one member rating, first-watch/rewatch context, and optional note.
- **Main files:** `ratings`, `RatingIn`, `_validate_score()`, `service.upsert_rating()`, rating UI.
- **Status:** Implemented.
- **Dependencies:** Scheduled/watched movie and authenticated member.
- **Behavior:** A scheduled-film rating is visible only to the member who gave it. Scores, notes, aggregates, public-profile activity, and statistics are revealed to the club only after the movie moves to `watched`.
- **Limitations:** No note length limit. Updating a rating preserves the original `created_at` because the conflict update does not modify it.
- **Follow-up:** Decide whether update time or note limits are needed.

### Two-way Plex rating sync

- **Purpose:** Keep future Film Club and Plex rating changes aligned for the same member and movie.
- **Main files:** `app/plex_ratings.py`, `app/token_crypto.py`, the rating route, Plex webhook route, and rating UI toast handling.
- **Status:** Implemented and covered by focused unit tests; production integration not yet exercised.
- **Dependencies:** Normal Plex login with a retained per-member token, movie present in Plex for outbound writes, `PLEX_WEBHOOK_SECRET` and Plex Pass webhooks for inbound updates, and the member's Plex rating-sync account setting.
- **Behavior:** Film Club scores multiply by two for Plex's 1–10 API; inbound scores divide by two. Local saves survive Plex failures. Unchanged webhook echoes are ignored. A per-member profile toggle pauses or resumes future changes in both directions without deleting the encrypted connection.
- **Limitations:** Future changes only; no historical reconciliation, no deletion sync, and inbound updates apply only to scheduled/watched movies matched by TMDB/IMDb identity. Legacy identity-only sessions are invalidated once so the normal login can retain the required token.
- **Follow-up:** Verify real per-user writes and webhook payload matching on the production Plex server after deployment.

### Frozen seen-before default

- **Purpose:** Pre-fill historical viewing context from the state captured when the movie was scheduled.
- **Main files:** `movies.seen_before_snapshot`, `schedule_movie()`, `default_seen_before()`.
- **Status:** Implemented.
- **Dependencies:** Valid snapshot JSON or live prior views fallback.
- **Limitations:** Member merging does not rewrite IDs inside snapshots.
- **Follow-up:** Define snapshot migration during member merge.

### Watched archive

- **Purpose:** Browse watched movies most-recent-first with rating average and personal completion state.
- **Main files:** `service.watched()`, `/api/watched`, `renderWatched()`.
- **Status:** Implemented.
- **Dependencies:** Watched movies and ratings.
- **Limitations:** No pagination or text search.
- **Follow-up:** Add only when archive size makes it necessary.

### Movie detail

- **Purpose:** Show metadata, eligibility/history, seconding, library state, actions, and rating groups.
- **Main files:** `service.movie_detail()`, `/api/movies/{id}`, `renderDetail()`.
- **Status:** Implemented.
- **Dependencies:** Movie, members, ratings, prior views/snapshot, Plex cache.
- **Behavior:** Scheduled details return only the requesting member's rating and no rating aggregates. Watched details return the complete group ratings.
- **Limitations:** Behavior and actions vary by status within one large render function.
- **Follow-up:** Add end-to-end coverage for each status rather than refactoring preemptively.

## Statistics

### Group totals

- **Purpose:** Summarize members, movies, ratings, group mean, and runtime. Headline insights stay visible while deeper tables are grouped into expandable sections.
- **Main files:** `stats.compute()`, Stats UI.
- **Status:** Implemented.
- **Dependencies:** Watched films, runtimes, ratings.
- **Limitations:** Missing metadata is omitted naturally.
- **Follow-up:** None apparent.

### Rater profiles and agreement matrix

- **Purpose:** Compare rating tendencies and pairwise taste correlation.
- **Main files:** `_rater_profiles()`, `_agreement_matrix()`, matrix UI.
- **Status:** Implemented.
- **Dependencies:** Member rating overlap.
- **Limitations:** Confidence floors are fixed constants; Pearson correlation is suppressed below five overlaps or with zero variance.
- **Follow-up:** Unit-test numeric edge cases.

### Divisiveness and first-watch/rewatch analysis

- **Purpose:** Identify disagreement and whether repeat viewing explains rating differences.
- **Main files:** `_divisiveness()`, `_split_explains()`, `_first_vs_rewatch_delta()`, `_member_rewatch_bias()`.
- **Status:** Implemented.
- **Dependencies:** Scores and accurate `seen_before` values.
- **Limitations:** The split explanation is explicitly a heuristic. Several outputs remain low-confidence with small groups.
- **Follow-up:** Preserve disclosure if formulas change; add deterministic unit tests.

### Suggester, genre, decade, and runtime summaries

- **Purpose:** Show how members' picks perform and describe the watched catalog.
- **Main files:** `_suggestions_per_member()`, `_suggester_scorecard()`, `_genre_decade_runtime()`.
- **Status:** Implemented.
- **Dependencies:** Suggester attribution and stored TMDB metadata.
- **Limitations:** Placeholder membership and missing metadata affect summaries.
- **Follow-up:** Recalculate expected outcomes after placeholder merges.

## Administration

### Admin and owner roles

- **Purpose:** Combine durable database admins with environment-defined owner accounts.
- **Main files:** `app/config.py`, `app/auth.py`, `service.set_member_admin()`, admin UI.
- **Status:** Implemented.
- **Dependencies:** `ADMIN_PLEX_IDS` for owners.
- **Limitations:** Admin truth has two sources; changing environment configuration is outside the UI.
- **Follow-up:** Design precedence carefully if configuration moves into the application.

### Placeholder member merge

- **Purpose:** Fold import/dev/seed identities into real Plex accounts.
- **Main files:** `service.is_placeholder()`, `admin_members()`, `merge_members()`, admin UI.
- **Status:** Partial.
- **Dependencies:** Existing source and target members.
- **Limitations:** Target rows win rating/prior-view collisions; votes are dropped; snapshots are not rewritten; operation is not reversible.
- **Follow-up:** Fix vote and snapshot handling and wrap the whole merge in an explicit transaction test.

### Administrative movie deletion

- **Purpose:** Remove any movie regardless of status or suggester.
- **Main files:** `/api/admin/movies/{id}`, `service.delete_movie()`, detail/admin UI paths.
- **Status:** Implemented.
- **Dependencies:** Effective admin.
- **Limitations:** Cascading, irreversible without backup.
- **Follow-up:** Consider audit/soft-delete only if operational need appears.

### Manual Plex library refresh

- **Purpose:** Update library badges without waiting for the periodic loop.
- **Main files:** `/api/admin/refresh_library`, `plex.refresh_library()`, admin UI.
- **Status:** Implemented.
- **Dependencies:** Plex server URL/token.
- **Limitations:** Failure is logged and cache marked unavailable, but endpoint still returns success because refresh never raises.
- **Follow-up:** Return refresh status/details without exposing secrets.

### Portable backup and restore

- **Purpose:** Let an admin download all durable application data as one file and recover it on the same or a fresh installation.
- **Main files:** `app/backups.py`, `/api/admin/backup`, `/api/admin/backup/restore`, and the Backup & restore section in `static/app.js`.
- **Status:** Implemented with automated round-trip and corruption tests.
- **Dependencies:** Effective admin access and writable persistent `/data` storage.
- **Safety behavior:** Uses an online SQLite snapshot; bundles the data key; validates checksums, archive shape, size, database integrity, foreign keys, and schema compatibility; creates a pre-restore server-side safety copy; atomically replaces the database; re-keys encrypted values; revokes all sessions.
- **Limitations:** The portable archive contains sensitive key material and must be stored securely. It is not password-encrypted. Server-side pre-restore and migration copies remain database-only, same-install rollback files.

## Operations

### Browser setup and admin-managed integrations

- **Purpose:** Configure a fresh installation and later rotate integration settings without editing deployment files.
- **Main files:** `app/settings.py`, migration v2, `/api/setup`, `/api/admin/settings`, `/api/admin/settings/test`, and setup/admin forms in `static/app.js`.
- **Status:** Implemented, including a service-grouped Admin UI with per-connection results.
- **Dependencies:** Persistent `/data`, access to the one-time container-log setup code, and reachable TMDB plus any configured Plex/Seerr services for validation.
- **Behavior:** **Test connections** checks the current unsaved form values without persisting them. It validates the Film Club URL format, authenticates to TMDB, verifies the Plex token and machine identifier, and authenticates to Seerr. Blank saved-secret fields reuse the encrypted current value for both testing and saving.
- **Limitations:** Explicit environment overrides remain read-only in the UI; the generated `master.key` must be backed up with the database; there is no master-key rotation flow.
- **Follow-up:** Publish a prebuilt container image and add browser-level setup tests.

### Mac-to-Unraid container deployment

- **Purpose:** Build and replace the production container with persistent SQLite storage.
- **Main files:** `deploy-from-mac.sh`, `Dockerfile`, `deploy.sh`, `.env.example`.
- **Status:** Implemented and readiness-checked against the current installation on SSH port 22.
- **Dependencies:** Mac SSH client/Python, LAN reachability, Unraid SSH key access, Docker, Unraid path layout, populated `.env`.
- **Limitations:** Defaults contain installation-specific addresses and the Unraid-side script replaces the running container directly.
- **Follow-up:** Keep this as maintainer-only automation; public installs use Docker Compose and the browser wizard.

### Local/tool testing

- **Purpose:** Run with `DEV_BYPASS_USER` and a local data directory.
- **Main files:** `README.md`, `docker-compose.yml`, `app/seed.py`, ignored `devdata/`.
- **Status:** Implemented as a convenience, not an authoritative deployment.
- **Dependencies:** Python virtualenv or Compose.
- **Limitations:** Rating sync has isolated `unittest` coverage, but the broader app has no automated browser/API harness; seed `--force` is destructive.
- **Follow-up:** Turn core seed scenarios into isolated automated fixtures and broaden endpoint coverage.

### Live updates

- **Purpose:** Refresh other clients after successful mutations without flashing or replacing the whole viewport.
- **Main files:** `app/events.py`, `BroadcastMiddleware`, `connectEvents()`.
- **Status:** Implemented.
- **Dependencies:** One process and browser `EventSource`.
- **Limitations:** No cross-worker bus; events are coarse invalidations and slow subscribers drop messages. Refreshes wait while a form control or modal is active.
- **Follow-up:** Retain one-worker deployment until shared infrastructure is intentionally added.
