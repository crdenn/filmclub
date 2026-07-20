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

### Changed
- Hardened `.gitignore` to exclude all credential-bearing and local-only
  artifacts (`.env`, `.claude/`, deployment bundles, databases, logs,
  generated IDs, caches) ahead of publishing source history.
- Documented the rollback model: restore the matching `<data dir>/backups/`
  snapshot and run the previous image/source tag.

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
