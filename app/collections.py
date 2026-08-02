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
    return d


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


def list_collections(conn: sqlite3.Connection, *, include_unpublished: bool = False) -> list[dict]:
    """All collections, newest first. Drafts are admin-only.

    Each row carries the artwork of its first entry, so the index can show a
    cover without a second round trip. ``entry_count`` counts stored entries,
    not the publicly visible subset — the index is a table of contents, not a
    claim about what a reader will see.
    """
    sql = """
        SELECT c.*,
               (SELECT e.still_url FROM collection_entries e
                     WHERE e.collection_id = c.id AND e.still_url IS NOT NULL
                     ORDER BY e.position, e.id LIMIT 1) AS cover_url,
               (SELECT COUNT(*) FROM collection_entries e2
                     WHERE e2.collection_id = c.id) AS entry_count
          FROM collections c
    """
    if not include_unpublished:
        sql += " WHERE c.published = 1"
    sql += " ORDER BY c.created_at DESC, c.id DESC"
    return [collection_base(r) for r in db.query_all(conn, sql)]


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
    allowed = ("title", "intro", "director_intro", "director_name", "published")
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
                      published: bool = False) -> int:
    cur = db.execute(
        conn,
        """INSERT INTO collections
               (slug, title, kind, intro, director_name, director_tmdb_id, published)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (slug, title, kind, intro, director_name, director_tmdb_id, 1 if published else 0),
    )
    return cur.lastrowid
