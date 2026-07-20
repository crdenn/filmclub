# Changelog

All notable changes to Film Club Tracker are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- AGPL-3.0 `LICENSE`, `SECURITY.md`, and `CONTRIBUTING.md`.
- This changelog.
- Versioned, transactional schema migrations (`app/migrations.py`,
  `schema_migrations` table). Pending migrations apply in order, each in its own
  transaction recorded atomically with its version; a timestamped online backup
  is written under `<data dir>/backups/` before any migration. Migration `1` is
  the baseline (the former `db._migrate()` additive columns), so existing
  databases upgrade with data preserved and no behaviour change. Covered by
  `tests/test_migrations.py`.
- `/readyz` readiness endpoint (database reachable + schema at latest + durable
  signing secret; returns 503 until ready, never exposes secret values), and an
  admin-only `/api/admin/diagnostics` endpoint (app/schema version, database
  integrity, row counts, backup status, integration on/off flags).
- Application version (`config.APP_VERSION`, overridable via `FILMCLUB_VERSION`)
  surfaced in `/readyz`, diagnostics, and OCI container labels.
- Encrypted, database-backed application settings (`app/settings.py`,
  `app_settings` table via migration `2`): a typed registry where secrets are
  Fernet-encrypted at rest and never returned to the browser, values apply at
  runtime, and explicit environment variables take precedence. A durable master
  key self-provisions to `<data dir>/master.key` (so `SESSION_SECRET` no longer
  needs to be supplied). Admin `GET`/`PUT /api/admin/settings` manage it, with
  server-side Plex/TMDB connection tests. Covered by `tests/test_settings.py`.
- First-run setup: an unconfigured instance logs a one-time setup code and routes
  the SPA to `#/setup`; `/api/setup` validates the code and connections, saves
  encrypted settings, and completes. An env-configured (existing) install
  auto-completes on startup and promotes its existing admin to the single locked
  **owner** (`is_owner`, DB-enforced unique), so upgrades don't require the wizard.

### Changed
- Split the single durable secret into two independent keys: a data-at-rest
  encryption key (`config.DATA_KEY` — the `SESSION_SECRET` env value if set,
  otherwise the generated `/data/master.key`, used for app-settings secrets and
  per-member Plex tokens) and a separate cookie-signing secret self-provisioned
  to `/data/session.key`. Rotating the signing secret now only forces a re-login
  and never risks encrypted data; existing installs keep all encrypted data
  readable (the data key still resolves to the same value) and members re-login
  once as signing moves to the new key. Foundation for safe credential rotation.
- Hardened `.gitignore` to exclude all credential-bearing and local-only
  artifacts (`.env`, `.claude/`, deployment bundles, databases, logs,
  generated IDs, caches) ahead of publishing source history.
- Documented the rollback model: restore the matching `<data dir>/backups/`
  snapshot and run the previous image/source tag.
- Rewrote `README.md` for the current feature set (This Week, seconding,
  profiles, reminder badges, live updates, redesigned backlog/watched/detail,
  migrations/backups/`/readyz`) and genericized deployment references (no
  site-specific hosts or IPs).

### Security
- Redact query-string secrets (TMDB `api_key`, Plex/access tokens) and the Plex
  webhook path from all log output via a root logging filter. Closes a leak
  where a failed TMDB request logged the real API key in its URL. Covered by
  `tests/test_log_redaction.py`.
- Hardened the first-run setup code: it is now printed once and stored only as a
  salted SHA-256 hash at rest (never in plaintext), expires 30 minutes after
  issuance, and is rate-limited (5 failed attempts per rolling 5-minute window,
  answered with HTTP 429). A code that is missing or expired is reissued on the
  next startup. Covered by `tests/test_settings.py`.

## [0.9.0] - 2026-07-19

Baseline snapshot of the known-good private Unraid deployment, captured as the
rollback point before the public self-hosting work begins.

### Included in the baseline
- Plex PIN login, server-access authorization, member provisioning, and signed
  sessions, with a development bypass for local work.
- TMDB search and metadata snapshots when adding a suggestion.
- Backlog with grid/list views, sorting, client-side filters, condensed
  coverage, and seen / not-seen / unknown tracking; eligibility keeps *unknown*
  distinct from *not seen*.
- Suggestion seconding, weekly selection, discussion-date editing, in-week
  rating, and the watched archive with first-watch/rewatch averages.
- Reminder badges, server-side statistics with small-sample suppression, and
  live cross-client refresh via Server-Sent Events.
- Optional Plex library enrichment/deep links, optional Seerr requests, and
  future-only Plex rating sync with encrypted per-member tokens.
- Admin member management, public member profiles, and a self profile.
- Docker image and a Mac-to-Unraid deployment workflow.

[Unreleased]: https://example.com/compare/v0.9.0...HEAD
[0.9.0]: https://example.com/releases/v0.9.0
