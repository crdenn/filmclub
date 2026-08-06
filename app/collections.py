"""Curated collections: authored, essay-style pages of films.

Separate from the club's suggest/schedule/watch lifecycle. A collection film is
not a suggestion, carries no coverage or eligibility, and never lands on the
backlog. The only thing a collection shares with the rest of the app is how it
finds a film on Plex.

Entries are keyed on ``tmdb_id`` — the durable external GUID — and resolved to a
live Plex item at render time through the existing library cache. Resolution is
deliberately tri-state (see ``resolve_entry``): a film that is genuinely absent
from the server and a server we simply cannot reach right now are different
situations and must not be rendered the same way.
"""
import re
import sqlite3

from . import db, plex

# An entry's Plex resolution state.
RESOLVED = "resolved"   # matched a library item; deep link available
MISSING = "missing"     # library is known-good and does not contain this film
UNKNOWN = "unknown"     # no usable library snapshot; resolution not attempted


def _entry_base(row: sqlite3.Row | dict) -> dict:
    """Shape an entry row for the API, mirroring db.movie_base's role."""
    d = dict(row)
    d["blurb"] = d.get("blurb") or ""
    d["has_blurb"] = bool(d["blurb"].strip())
    return d


def collection_base(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["published"] = bool(d.get("published"))
    d["intro"] = d.get("intro") or ""
    d["director_intro"] = d.get("director_intro") or ""
    # Anything without an explicit origin predates the column and is the owner's.
    d["origin"] = d.get("origin") or "authored"
    d["editable"] = d["origin"] == "authored"
    return d


def attach_club_state(conn: sqlite3.Connection, entries: list[dict]) -> None:
    """Mark which entries are also films the club is tracking, in place.

    A collection entry is deliberately independent of ``movies`` — a film can be
    written about without ever being suggested. But when the same film *is* on
    the club's list, the page should link to its normal detail page and say
    where it stands, rather than pretending the two are unrelated.

    A watched film also gets the club's real average rating, so a row can say
    "seen by the club, 4.2 avg" instead of just linking off — the average
    already exists for the movie detail page; this reuses the same fact.

    One query for the whole page rather than one per row.
    """
    ids = [e["tmdb_id"] for e in entries if e.get("tmdb_id")]
    found: dict[int, dict] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        rows = db.query_all(
            conn,
            f"""SELECT m.id, m.tmdb_id, m.status,
                       (SELECT AVG(r.score) FROM ratings r WHERE r.movie_id = m.id) AS avg_rating
                  FROM movies m WHERE m.tmdb_id IN ({marks})""",
            tuple(ids),
        )
        found = {
            r["tmdb_id"]: {
                "movie_id": r["id"],
                "movie_status": r["status"],
                "club_avg_rating": round(r["avg_rating"], 1) if r["avg_rating"] is not None else None,
            }
            for r in rows
        }
    for entry in entries:
        state = found.get(entry.get("tmdb_id"))
        entry["movie_id"] = state["movie_id"] if state else None
        entry["movie_status"] = state["movie_status"] if state else None
        entry["club_avg_rating"] = state["club_avg_rating"] if state else None


def resolve_entry(entry: dict) -> dict:
    """Attach Plex resolution state and a deep link to one entry.

    Three outcomes, because collapsing them loses the distinction that decides
    whether an entry is hidden:

    * ``resolved`` — the film is on the server; ``plex_link`` is a deep link.
    * ``missing``  — the library snapshot is good and this film is not in it.
                     The author's writing has been detached from the film
                     (removed file, rebuilt library); hide it publicly and list
                     it for the author.
    * ``unknown``  — Plex is unconfigured or the last refresh failed. We cannot
                     say anything about this film, so we must not call it
                     missing: show the entry, minus the watch link.
    """
    resolved = dict(entry)
    if not plex.library_ready():
        resolved["plex_state"] = UNKNOWN
        resolved["plex_link"] = None
        return resolved

    match = plex.library_match(entry.get("tmdb_id"), entry.get("imdb_id"))
    if match:
        resolved["plex_state"] = RESOLVED
        resolved["plex_link"] = match.get("deep_link")
    else:
        resolved["plex_state"] = MISSING
        resolved["plex_link"] = None
    return resolved


# The index's running order, shared by every query that needs to agree with it.
# Hand-placed collections first, in the order they were placed; everything
# unplaced falls in behind, newest first. Keeping this in one constant is what
# stops `slug_position` from disagreeing with the list it claims to index.
INDEX_ORDER = "(sort_order IS NULL), sort_order, created_at DESC, id DESC"


def list_collections(conn: sqlite3.Connection, *, include_unpublished: bool = False) -> list[dict]:
    """All collections in index order. Drafts are admin-only."""
    sql = "SELECT * FROM collections"
    if not include_unpublished:
        sql += " WHERE published = 1"
    sql += f" ORDER BY {INDEX_ORDER}"
    return [collection_base(r) for r in db.query_all(conn, sql)]


def set_index_order(conn: sqlite3.Connection, slugs: list[str]) -> list[str]:
    """Arrange the index by hand. Returns the slugs that matched a collection.

    Every slug named is placed in the order given. Anything not named is reset
    to unplaced rather than left holding a stale number, so the arrangement is
    exactly what was asked for and the remainder keeps its newest-first
    default — no invisible ordering left over from a previous call.
    """
    placed = []
    for position, slug in enumerate(slugs):
        cur = db.execute(
            conn,
            "UPDATE collections SET sort_order = ?, updated_at = datetime('now')"
            " WHERE slug = ?",
            (position, slug),
        )
        if cur.rowcount:
            placed.append(slug)
    if placed:
        marks = ",".join("?" * len(placed))
        db.execute(
            conn,
            f"UPDATE collections SET sort_order = NULL WHERE slug NOT IN ({marks})",
            placed,
        )
    return placed


def owner_name(conn: sqlite3.Connection) -> str | None:
    """The site's single owner, for attributing an authored collection to a
    person rather than a database column. Schema guarantees at most one
    (``idx_members_single_owner``), so this is unambiguous."""
    row = db.query_one(
        conn, "SELECT display_name, username FROM members WHERE is_owner = 1 LIMIT 1"
    )
    if not row:
        return None
    return (row["display_name"] or "").strip() or row["username"]


def _stats(entries: list[dict], *, blurb_gated: bool) -> dict:
    """Aggregate facts about a collection's stored entries, for a listing a
    reader is deciding whether to open rather than reading in full.

    Computed over every stored entry, not the gated subset a reader would
    actually see — the index is a table of contents, not a claim about
    current visibility. Where the two diverge, ``on_plex``/``missing`` and
    ``written`` say so explicitly instead of the total silently overstating
    what is readable right now.
    """
    resolved = [resolve_entry(e) for e in entries]
    plex_ok = plex.library_ready()
    on_plex = sum(1 for e in resolved if e["plex_state"] == RESOLVED) if plex_ok else None
    missing = sum(1 for e in resolved if e["plex_state"] == MISSING) if plex_ok else None
    years = [e["year"] for e in entries if e.get("year")]
    written = sum(1 for e in entries if str(e.get("blurb") or "").strip())
    # A contact sheet: a handful of real stills so a reader judges a collection
    # by what's actually in it. Drawn from every stored entry, same as the
    # numbers above — a still is just a picture, not a spoiler of unwritten
    # prose, so there is nothing here for the gated view to protect.
    with_stills = [e for e in entries if e.get("still_url")]
    return {
        "film_count": len(entries),
        "runtime_minutes": sum(e.get("runtime") or 0 for e in entries),
        "year_from": min(years) if years else None,
        "year_to": max(years) if years else None,
        "on_plex": on_plex,
        "missing": missing,
        "written": written if blurb_gated else None,
        "stills": [{"still_url": e["still_url"], "title": e["title"]}
                  for e in with_stills[:6]],
        "stills_overflow": max(0, len(with_stills) - 6),
    }


def _last_changed(collection: dict, entries: list[dict]) -> str:
    """The most recent edit to a collection or any of its entries.

    ``collections.updated_at`` alone misses an edited blurb (only
    ``collection_entries.updated_at`` moves) or an added/removed film, so this
    takes the latest of everything. SQLite's ``datetime('now')`` format sorts
    correctly as a plain string, so no parsing is needed.
    """
    stamps = [collection.get("updated_at") or ""]
    stamps += [e.get("updated_at") or "" for e in entries]
    return max(stamps)


def slug_position(conn: sqlite3.Connection, slug: str, *, is_admin: bool,
                  preview: bool) -> int | None:
    """1-based position of a collection in the index order: every authored
    collection first, then generated, each in ``INDEX_ORDER`` — matching
    ``index_payload``'s own mine-then-generated split. Purely a display
    numeral; returns None if the slug is not in the visible set at all."""
    include_unpublished = is_admin and not preview
    sql = "SELECT slug FROM collections"
    if not include_unpublished:
        sql += " WHERE published = 1"
    sql += f" ORDER BY (origin != 'authored'), {INDEX_ORDER}"
    slugs = [r["slug"] for r in db.query_all(conn, sql)]
    return slugs.index(slug) + 1 if slug in slugs else None


def index_payload(conn: sqlite3.Connection, *, is_admin: bool, preview: bool) -> dict:
    """Assemble the whole collections index in one call.

    Split into ``mine`` (the owner's own writing) and ``generated`` (assembled
    for them), matching the product's own distinction rather than an editorial
    one invented for the page. ``mine`` collections additionally carry a
    ``rows`` listing of individual films — reusing ``collection_detail``'s
    existing gating rather than a second implementation of it, so a listing
    can never show a reader a title withheld from them (an unpublished draft,
    an unwritten director entry).
    """
    include_unpublished = is_admin and not preview
    collections = list_collections(conn, include_unpublished=include_unpublished)

    mine, generated = [], []
    for c in collections:
        entries = entries_for(conn, c["id"])
        c.update(_stats(entries, blurb_gated=c["kind"] == "director"))
        if c["origin"] == "authored":
            full = collection_detail(conn, c["slug"], is_admin=is_admin, preview=preview)
            c["rows"] = full["entries"] if full else []
            c["last_changed"] = _last_changed(c, entries)
            mine.append(c)
        else:
            generated.append(c)

    return {
        "mine": mine,
        "generated": generated,
        "owner_name": owner_name(conn) if mine else None,
        "total_films": sum(c["film_count"] for c in collections),
    }


def get_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = db.query_one(conn, "SELECT * FROM collections WHERE slug = ?", (slug,))
    return collection_base(row) if row else None


def entries_for(conn: sqlite3.Connection, collection_id: int) -> list[dict]:
    """Every stored entry, in author order, before any resolution or gating."""
    rows = db.query_all(
        conn,
        """SELECT * FROM collection_entries
               WHERE collection_id = ?
               ORDER BY position, id""",
        (collection_id,),
    )
    return [_entry_base(r) for r in rows]


def collection_detail(conn: sqlite3.Connection, slug: str, *, is_admin: bool,
                      preview: bool = False) -> dict | None:
    """Assemble one collection page.

    The public view shows only entries that both resolve on Plex (or cannot be
    checked) and, for director collections, carry a blurb — so the page grows as
    the author writes and never looks half-finished. The admin view keeps
    everything and reports what was withheld and why.

    ``preview`` lets an admin see exactly the public view. It gates the content
    but not the access: an unpublished draft is still viewable, because the
    author previewing their own draft is the entire point. Deriving the preview
    here rather than hiding things in the client means what is shown is the same
    payload a reader would actually receive.
    """
    collection = get_by_slug(conn, slug)
    if collection is None:
        return None
    if not collection["published"] and not is_admin:
        return None
    is_admin = is_admin and not preview

    resolved = [resolve_entry(e) for e in entries_for(conn, collection["id"])]
    attach_club_state(conn, resolved)

    # A director collection's public membership is gated on the blurb; a
    # hand-picked one is not — the author chose those films explicitly, so an
    # unwritten blurb is a gap in the page, not a reason to hide the film.
    blurb_gated = collection["kind"] == "director"

    public, unresolved, unwritten = [], [], []
    for entry in resolved:
        if entry["plex_state"] == MISSING:
            unresolved.append(entry)
            continue
        if blurb_gated and not entry["has_blurb"]:
            unwritten.append(entry)
            continue
        public.append(entry)

    collection["entries"] = resolved if is_admin else public
    collection["entry_count"] = len(public)
    if is_admin:
        # The author's to-do lists: writing detached from a film, and films
        # still awaiting a blurb.
        collection["unresolved"] = unresolved
        collection["unwritten"] = unwritten
        collection["plex_ready"] = plex.library_ready()
    return collection


def upsert_entry(conn: sqlite3.Connection, collection_id: int, meta: dict,
                 *, blurb: str | None = None, position: int | None = None) -> int:
    """Insert or update one entry, keyed on (collection, tmdb_id).

    ``meta`` is a TMDB-shaped dict (as returned by ``tmdb.details``). Re-running
    with fresh metadata refreshes the snapshot without disturbing the author's
    blurb unless one is supplied.
    """
    if position is None:
        # An entry that already exists keeps where the author put it: re-adding
        # a film to refresh its metadata must not silently reorder the page.
        existing = db.query_one(
            conn,
            "SELECT position FROM collection_entries WHERE collection_id = ? AND tmdb_id = ?",
            (collection_id, meta["tmdb_id"]),
        )
        if existing:
            position = existing["position"]
        else:
            row = db.query_one(
                conn,
                "SELECT COALESCE(MAX(position), -1) + 1 AS next"
                "  FROM collection_entries WHERE collection_id = ?",
                (collection_id,),
            )
            position = row["next"] if row else 0

    cur = db.execute(
        conn,
        """INSERT INTO collection_entries
               (collection_id, tmdb_id, imdb_id, title, year, runtime, director,
                still_url, blurb, position)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (collection_id, tmdb_id) DO UPDATE SET
               imdb_id    = excluded.imdb_id,
               title      = excluded.title,
               year       = excluded.year,
               runtime    = excluded.runtime,
               director   = excluded.director,
               still_url  = excluded.still_url,
               blurb      = COALESCE(excluded.blurb, collection_entries.blurb),
               position   = excluded.position,
               updated_at = datetime('now')""",
        (
            collection_id,
            meta["tmdb_id"],
            meta.get("imdb_id"),
            meta.get("title") or "Untitled",
            meta.get("year"),
            meta.get("runtime"),
            meta.get("director"),
            meta.get("still_url") or meta.get("backdrop_url"),
            blurb,
            position,
        ),
    )
    return cur.lastrowid


def set_director_scaffold(conn: sqlite3.Connection, slug: str, person: dict) -> bool:
    """Snapshot a director's TMDB scaffolding onto the collection."""
    cur = db.execute(
        conn,
        """UPDATE collections
              SET director_tmdb_id = ?, director_name = COALESCE(?, director_name),
                  director_portrait_url = ?, director_born = ?, director_died = ?,
                  updated_at = datetime('now')
            WHERE slug = ?""",
        (person.get("tmdb_id"), person.get("name"), person.get("portrait_url"),
         person.get("born"), person.get("died"), slug),
    )
    return cur.rowcount > 0


def coverage(entries: list[dict], filmography: list[dict]) -> dict:
    """Merge a director's full filmography with what the author has written.

    This is the to-do list: every film TMDB credits them with, marked as
    written about, added but still blank, or not yet touched. Films on the Plex
    server are flagged so the author can tell what is actually watchable now
    from what would need finding first.

    Entries the author added that TMDB does not attribute to this director
    (a segment, a co-directed film, a disputed credit) are kept in `extra`
    rather than dropped — the author put them there deliberately.
    """
    by_tmdb = {e["tmdb_id"]: e for e in entries}
    plex_ok = plex.library_ready()

    films, written, added = [], 0, 0
    for film in filmography:
        entry = by_tmdb.pop(film["tmdb_id"], None)
        if entry is None:
            state = "untouched"
        elif entry["has_blurb"]:
            state = "written"
            written += 1
        else:
            state = "blank"
            added += 1
        films.append({
            **film,
            "state": state,
            "entry_id": entry["id"] if entry else None,
            # None, not False, when we have no library snapshot: unknown is not
            # the same as absent, and the UI should not claim otherwise.
            "on_plex": (plex.library_match(film["tmdb_id"], None) is not None)
                       if plex_ok else None,
        })

    return {
        "films": films,
        "extra": list(by_tmdb.values()),
        "total": len(filmography),
        "written": written,
        "blank": added,
    }


def update_collection(conn: sqlite3.Connection, slug: str, fields: dict) -> bool:
    """Patch authored fields on a collection. Unknown keys are ignored."""
    allowed = ("title", "intro", "director_intro", "director_name", "published",
               "sort_order")
    sets, params = [], []
    for key in allowed:
        if key not in fields:
            continue
        value = fields[key]
        if key == "published":
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return False
    params.append(slug)
    cur = db.execute(
        conn,
        f"UPDATE collections SET {', '.join(sets)}, updated_at = datetime('now') WHERE slug = ?",
        params,
    )
    return cur.rowcount > 0


def update_entry(conn: sqlite3.Connection, collection_id: int, entry_id: int,
                 blurb: str) -> bool:
    """Store the author's blurb for one entry."""
    cur = db.execute(
        conn,
        """UPDATE collection_entries SET blurb = ?, updated_at = datetime('now')
               WHERE id = ? AND collection_id = ?""",
        (blurb, entry_id, collection_id),
    )
    return cur.rowcount > 0


def delete_entry(conn: sqlite3.Connection, collection_id: int, entry_id: int) -> bool:
    """Remove one film from a collection. Returns False if it wasn't there."""
    cur = db.execute(
        conn,
        "DELETE FROM collection_entries WHERE id = ? AND collection_id = ?",
        (entry_id, collection_id),
    )
    return cur.rowcount > 0


def delete_collection(conn: sqlite3.Connection, slug: str) -> bool:
    """Delete a collection and, by foreign-key cascade, all of its entries."""
    cur = db.execute(conn, "DELETE FROM collections WHERE slug = ?", (slug,))
    return cur.rowcount > 0


def smart_title(text: str) -> str:
    """Capitalise a title that was typed entirely in lower case.

    Titles are shown large and are the first thing on the page, where "david
    cronenberg" reads as a bug rather than a style. Anything containing an
    uppercase letter is left exactly as typed, so deliberate casing — eXistenZ,
    JFK, a lowercase choice made on purpose — survives.
    """
    text = (text or "").strip()
    if not text or any(c.isupper() for c in text):
        return text
    return " ".join(w[:1].upper() + w[1:] if w else w for w in text.split(" "))


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """Turn a title into a URL key. Falls back to 'collection' if nothing survives."""
    slug = _SLUG_STRIP.sub("-", str(title or "").lower()).strip("-")
    return slug[:60].strip("-") or "collection"


def unique_slug(conn: sqlite3.Connection, title: str) -> str:
    """A slug not already taken, suffixing -2, -3 … as needed."""
    base = slugify(title)
    slug, n = base, 1
    while db.query_one(conn, "SELECT 1 FROM collections WHERE slug = ?", (slug,)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def create_collection(conn: sqlite3.Connection, slug: str, title: str, *,
                      kind: str = "picked", intro: str | None = None,
                      director_name: str | None = None,
                      director_tmdb_id: int | None = None,
                      published: bool = False,
                      origin: str = "authored") -> int:
    cur = db.execute(
        conn,
        """INSERT INTO collections
               (slug, title, kind, intro, director_name, director_tmdb_id,
                published, origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, title, kind, intro, director_name, director_tmdb_id,
         1 if published else 0, origin),
    )
    return cur.lastrowid
