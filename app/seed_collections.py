"""Seed one hand-picked collection, for developing the collection page.

Deliberately *not* part of ``app.seed``: that script clears application rows on
``--force``, and this one must be safe to run against a database with real data.
It only inserts, and re-running it refreshes the metadata snapshot without
touching any blurb that has since been edited.

    python -m app.seed_collections            # seed / refresh
    python -m app.seed_collections --broken   # also add an unresolvable entry

Film metadata is looked up through TMDB rather than hardcoded, so the snapshot
matches what the rest of the app would store. Requires TMDB_API_KEY.
"""
import argparse
import asyncio
import sys

from . import collections as coll
from . import config, db, tmdb

SLUG = "night-drives"
TITLE = "Night Drives"

INTRO = """Some films are about cities, and some films are about being awake in
them at the wrong hour. These five belong to the second kind — headlights on wet
asphalt, a windscreen, somebody with nowhere to be until morning."""

# (title, year, blurb). Placeholder prose: long enough to be an honest test of
# the row typography, meant to be rewritten in place once editing exists.
FILMS = [
    ("Drive", 2011,
     "Refn shoots Los Angeles as a series of pools of light with darkness "
     "between them, and Gosling barely speaks for the first twenty minutes. "
     "The violence, when it arrives, is shocking precisely because the film "
     "has spent so long being quiet and beautiful."),
    ("Collateral", 2004,
     "Michael Mann shot this on early digital specifically because film stock "
     "could not hold the detail of a Los Angeles night, and it shows — you can "
     "see down every side street. A two-hander in a taxi that keeps finding "
     "new rooms to open into."),
    ("Taxi Driver", 1976,
     "The one everybody quotes and few rewatch, which is a shame, because the "
     "quotable parts are the least of it. Bernard Herrmann's last score turns "
     "the whole city into something humid and half-asleep."),
    ("Lost in Translation", 2003,
     "Nocturnal in a completely different register: neon through hotel glass, "
     "jet lag as an emotional state. Nothing happens, twice, and it is one of "
     "the most re-watchable films of its decade."),
    ("Heat", 1995,
     "Three hours long and not a wasted minute. Mann again, and the same "
     "fascination with professionals working at night, but here the city is "
     "vast and cold rather than intimate."),
]

# A tmdb id that will never exist, to exercise the unresolved path end to end.
BROKEN_TMDB_ID = 999999999


async def _lookup(title: str, year: int) -> dict | None:
    """Find a film by title and pin it to the right year, then snapshot it."""
    results = await tmdb.search(title, limit=8)
    if not results:
        print(f"  ! no TMDB match for {title!r}", file=sys.stderr)
        return None
    exact = [r for r in results if r.get("year") == year]
    chosen = (exact or results)[0]
    if not exact:
        print(f"  ! no {year} match for {title!r}; using {chosen.get('year')}", file=sys.stderr)
    return await tmdb.details(chosen["tmdb_id"])


async def main(include_broken: bool = False) -> int:
    if not config.TMDB_API_KEY:
        print("TMDB_API_KEY is not set; cannot look up film metadata.", file=sys.stderr)
        return 1

    db.init_db()
    conn = db.connect()
    try:
        existing = coll.get_by_slug(conn, SLUG)
        if existing:
            collection_id = existing["id"]
            print(f"Collection {SLUG!r} already exists (id {collection_id}); refreshing entries.")
        else:
            collection_id = coll.create_collection(
                conn, SLUG, TITLE, kind="picked", intro=INTRO.strip(), published=True,
            )
            print(f"Created collection {SLUG!r} (id {collection_id}).")

        for position, (title, year, blurb) in enumerate(FILMS):
            meta = await _lookup(title, year)
            if meta is None:
                continue
            coll.upsert_entry(conn, collection_id, meta, blurb=blurb, position=position)
            print(f"  + {meta['title']} ({meta.get('year')}) tmdb:{meta['tmdb_id']}")

        if include_broken:
            coll.upsert_entry(
                conn, collection_id,
                {
                    "tmdb_id": BROKEN_TMDB_ID,
                    "title": "A Film No Longer On The Server",
                    "year": 1970,
                    "runtime": 90,
                    "director": "Nobody",
                    "imdb_id": None,
                    "backdrop_url": None,
                },
                blurb="Seeded to exercise the unresolved path; safe to delete.",
                position=len(FILMS),
            )
            print(f"  + unresolvable entry (tmdb:{BROKEN_TMDB_ID})")

        detail = coll.collection_detail(conn, SLUG, is_admin=True)
        print(
            f"\n{detail['title']}: {len(detail['entries'])} entries, "
            f"{len(detail['unresolved'])} unresolved, "
            f"plex_ready={detail['plex_ready']}"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broken", action="store_true",
                        help="also seed an entry that cannot resolve on Plex")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(include_broken=args.broken)))
