-- Film Club Tracker schema. SQLite.
-- Kept deliberately small: six users, a few hundred rows. A file copy is a backup.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS members (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_id      TEXT UNIQUE NOT NULL,
    plex_account_id TEXT,            -- numeric Plex account id used by webhooks
    plex_token_encrypted TEXT,       -- per-user token; Fernet-encrypted at rest
    plex_rating_sync_enabled INTEGER NOT NULL DEFAULT 1, -- member opt-out
    username     TEXT NOT NULL,      -- the Plex username, refreshed on each login
    display_name TEXT,               -- user-chosen name shown in place of username
    email        TEXT,
    thumb        TEXT,               -- Plex avatar URL
    color        TEXT NOT NULL,      -- deterministic hex, derived from plex_id
    is_admin     INTEGER NOT NULL DEFAULT 0,  -- can access the admin panel
    is_owner     INTEGER NOT NULL DEFAULT 0,  -- first setup owner; cannot be demoted
    -- Can create and manage their own curated collections without admin
    -- access. Deliberately narrower than is_admin: this grants nothing
    -- outside collections, and only over collections this member created
    -- (collections.created_by) — an admin's authority is unrestricted by
    -- comparison and does not depend on this flag.
    can_curate_collections INTEGER NOT NULL DEFAULT 0,
    theme        TEXT NOT NULL DEFAULT 'system', -- 'system' | 'dark' | 'light'
    discord_user_id TEXT,             -- admin-entered, for @mentions in reminder digests
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS movies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id       INTEGER,
    title         TEXT NOT NULL,
    year          INTEGER,
    poster_url    TEXT,
    backdrop_url  TEXT,
    runtime       INTEGER,          -- minutes
    director      TEXT,
    language      TEXT,             -- human-readable original language from TMDB
    content_rating TEXT,            -- US movie certification from TMDB (G, PG, etc.)
    overview      TEXT,
    genres        TEXT,             -- JSON array of strings
    suggested_by  INTEGER REFERENCES members(id) ON DELETE SET NULL,
    suggested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- Suggester's optional "elevator pitch" for why the club should watch it.
    -- Shown only on the film's detail page, never on the backlog grid.
    pitch         TEXT,
    status        TEXT NOT NULL DEFAULT 'suggested',  -- 'suggested' | 'scheduled' | 'watched'
    watched_at    TEXT,
    imdb_id       TEXT,             -- external id for Plex in-library matching
    -- Frozen at the watch transition: JSON map of member_id -> bool of each
    -- member's prior_views.seen at that moment. Seeds ratings.seen_before so the
    -- historical fact can't be mutated by later prior_views edits.
    seen_before_snapshot TEXT,
    -- Outcome of the Seerr auto-request at add time, if that feature is enabled.
    -- One of: 'in_library' | 'available' | 'requested' | 'failed'. NULL = the
    -- feature was off (or the film predates it).
    seerr_status  TEXT
);

CREATE TABLE IF NOT EXISTS ratings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id     INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    member_id    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    score        REAL NOT NULL,     -- 0.5 .. 5.0 in 0.5 steps
    seen_before  INTEGER NOT NULL DEFAULT 0,  -- historical fact at club-watch time
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (movie_id, member_id)
);

-- "Seconding" a backlog suggestion: a lightweight +1 that a member also wants to
-- watch a film. The suggester is implied and cannot vote for their own film
-- (enforced in the service layer). Absence of a row = not seconded by that member.
CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id     INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    member_id    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (movie_id, member_id)
);

-- Pick-eligibility state, editable before a film is watched. Distinct from
-- ratings.seen_before on purpose: this answer can change over time as a member
-- watches things independently; seen_before is frozen at watch time.
CREATE TABLE IF NOT EXISTS prior_views (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id     INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    member_id    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    seen         INTEGER NOT NULL,  -- 1 seen, 0 not seen. Absence of row = unknown.
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (movie_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_movies_status ON movies(status);
CREATE INDEX IF NOT EXISTS idx_ratings_movie ON ratings(movie_id);
CREATE INDEX IF NOT EXISTS idx_prior_movie ON prior_views(movie_id);
CREATE INDEX IF NOT EXISTS idx_votes_movie ON votes(movie_id);
-- idx_members_single_owner is created by migration 2 after older members
-- tables have received the is_owner column. Creating it here would make
-- db.init_db() fail before migrations can upgrade a pre-v2 database.

-- Curated collections: an authored essay-style page of films, each with a
-- blurb. Separate from the club's suggest/schedule/watch lifecycle on purpose —
-- a collection film need not ever have been a suggestion, and adding one must
-- not put it on the backlog.
CREATE TABLE IF NOT EXISTS collections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT UNIQUE NOT NULL,   -- URL key: #/collections/<slug>
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'picked',  -- 'picked' | 'director'
    intro         TEXT,                   -- author's markdown, shown above the rows
    -- 'director' collections only: the subject, and the TMDB person id used to
    -- pull the factual scaffolding (portrait, dates, full filmography).
    director_name    TEXT,
    director_tmdb_id INTEGER,
    director_intro   TEXT,                -- markdown prose about the director
    -- Snapshotted TMDB scaffolding, so a public page load never hits TMDB. The
    -- filmography is not stored: it is only needed by the admin coverage view.
    director_portrait_url TEXT,
    director_born    TEXT,                -- ISO date
    director_died    TEXT,
    published     INTEGER NOT NULL DEFAULT 0,  -- 0 = admin-only/curator-only draft
    -- Who wrote this. 'authored' collections are a person's own writing and
    -- carry the full editing surface, gated to created_by (see below) unless
    -- the viewer is an admin; 'generated' ones were assembled by the app and
    -- are read-only in the UI regardless of who's viewing, so there is no
    -- editing furniture on a page nobody is meant to edit. Deliberately a
    -- stored fact rather than inferred, since nothing about the content
    -- itself distinguishes the two.
    origin        TEXT NOT NULL DEFAULT 'authored',  -- 'authored' | 'generated'
    -- The member who created this collection. NULL for every 'generated' one
    -- (nobody personally owns those) and for an 'authored' one predating this
    -- column (migration backfills those to the site owner). ON DELETE SET
    -- NULL rather than CASCADE, matching movies.suggested_by: removing a
    -- member must not delete their writing.
    created_by    INTEGER REFERENCES members(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One film within a collection.
--
-- Keyed on tmdb_id (the durable external GUID), never on a Plex ratingKey:
-- ratingKeys are server-local and change when an item is deleted and re-added
-- or the library is rebuilt, which would silently detach the author's writing
-- from the film. Resolution to a live Plex item happens at render time.
--
-- The title/year/runtime/director/still columns are a deliberate snapshot, for
-- the same reason `movies` snapshots TMDB: a collection page must render
-- without calling TMDB, and without the film existing in `movies` at all.
CREATE TABLE IF NOT EXISTS collection_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    tmdb_id       INTEGER NOT NULL,
    imdb_id       TEXT,                   -- secondary key for Plex matching
    title         TEXT NOT NULL,
    year          INTEGER,
    runtime       INTEGER,                -- minutes
    director      TEXT,
    still_url     TEXT,                   -- TMDB backdrop/still for the row image
    blurb         TEXT,                   -- author's markdown; empty = not written yet
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (collection_id, tmdb_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_entries_collection
    ON collection_entries(collection_id, position);

-- Runtime configuration entered through first-run setup or Admin settings.
-- Sensitive values are Fernet-encrypted with the durable key in /data.
CREATE TABLE IF NOT EXISTS app_settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    encrypted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Revocable server-side sessions. The cookie carries an opaque random token; we
-- store only its SHA-256 hash, so a database read never yields a usable session.
-- Deleting a row revokes it immediately (logout, admin-wide invalidation).
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    member_id    INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_member ON sessions(member_id);

-- Login methods mapped to member rows. A member may have both a 'plex' and a
-- 'local' identity (account linking). provider_uid is the Plex uuid or the
-- lowercased local username; password_hash is set for local identities only.
CREATE TABLE IF NOT EXISTS identities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id     INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,          -- 'plex' | 'local'
    provider_uid  TEXT NOT NULL,          -- plex uuid, or lowercased username
    password_hash TEXT,                   -- local identities only (scrypt)
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, provider_uid)
);
CREATE INDEX IF NOT EXISTS idx_identities_member ON identities(member_id);

-- Single-use, expiring invitations. Only the SHA-256 hash of the code is stored.
CREATE TABLE IF NOT EXISTS invites (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash          TEXT NOT NULL UNIQUE,
    created_by         INTEGER REFERENCES members(id) ON DELETE SET NULL,
    email              TEXT,
    expires_at         TEXT NOT NULL,
    redeemed_at        TEXT,              -- NULL = unused (single-use)
    redeemed_member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Admin-issued, single-use password reset links for local identities. The raw
-- token is shown once; only its SHA-256 hash is retained here.
CREATE TABLE IF NOT EXISTS password_resets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    created_by INTEGER REFERENCES members(id) ON DELETE SET NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_password_resets_member ON password_resets(member_id);
