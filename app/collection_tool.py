"""Author a collection from a JSON file, instead of from the browser.

Writing a seven-film collection through the UI means seven searches and seven
click-to-edit blurbs, and nothing to review before it is live. This is the same
work as one reviewable document:

    python -m app.collection_tool dump westerns > westerns.json
    $EDITOR westerns.json
    python -m app.collection_tool apply < westerns.json

``dump`` emits exactly the shape ``apply`` accepts, so editing an existing
collection is a round trip rather than a different procedure from creating one.
Both are idempotent: re-applying an unchanged payload reports no changes and
touches nothing.

Deliberately a module inside ``app`` rather than a loose script, so it ships in
the image with every deploy and can be run against the live container with no
files to copy in first:

    docker exec -i filmclub python -m app.collection_tool apply < payload.json

Payload shape (only ``slug`` and ``films`` are required on apply):

    {
      "slug":      "westerns",
      "title":     "Westerns",
      "intro":     "Some genres get nostalgic about themselves…",
      "kind":      "picked",       # or "director"
      "origin":    "generated",    # or "authored"
      "published": true,
      "prune":     false,          # true = delete entries absent from `films`
      "films": [
        {"tmdb": 11048, "blurb": "Ford shot Monument Valley for the last…"}
      ]
    }
"""
import argparse
import asyncio
import json
import sys

from . import collections as coll
from . import db, tmdb

# Metadata for each film is one TMDB call. Bounded concurrency keeps a large
# collection from opening a socket per film, matching main._sync_director.
_TMDB_LIMIT = 5


def _load(stream) -> dict:
    spec = json.load(stream)
    if not isinstance(spec, dict):
        raise SystemExit("payload must be a JSON object")
    if not spec.get("slug"):
        raise SystemExit("payload needs a 'slug'")
    films = spec.get("films")
    if not isinstance(films, list):
        raise SystemExit("payload needs a 'films' array")
    for i, film in enumerate(films):
        if not isinstance(film, dict) or not film.get("tmdb"):
            raise SystemExit(f"films[{i}] needs a numeric 'tmdb' id")
    return spec


async def _fetch(films: list[dict]) -> list[tuple[dict, dict | None]]:
    """Snapshot TMDB metadata for every film, concurrently.

    One film failing must not abandon the rest — the caller skips a None and
    says so, leaving a collection that is short a film rather than half-written.
    """
    limit = asyncio.Semaphore(_TMDB_LIMIT)

    async def one(film):
        async with limit:
            try:
                return film, await tmdb.details(int(film["tmdb"]))
            except Exception as e:  # noqa: BLE001 — reported per film, not fatal
                print(f"  ! tmdb {film['tmdb']} failed: {e}", file=sys.stderr)
                return film, None

    return list(await asyncio.gather(*(one(f) for f in films)))


async def _apply(spec: dict, *, dry_run: bool) -> int:
    conn = db.connect()
    try:
        slug = spec["slug"]
        existing = coll.get_by_slug(conn, slug)
        changes: list[str] = []

        if existing is None:
            title = coll.smart_title(spec.get("title") or slug.replace("-", " "))
            if dry_run:
                print(f"would create {slug!r} ({title})")
                collection_id = None
            else:
                collection_id = coll.create_collection(
                    conn, slug, title,
                    kind=spec.get("kind", "picked"),
                    intro=spec.get("intro"),
                    published=bool(spec.get("published", False)),
                    origin=spec.get("origin", "generated"),
                )
                changes.append(f"created {slug!r} (id {collection_id})")
        else:
            collection_id = existing["id"]
            # Only fields actually present in the payload are touched, so a
            # partial payload cannot silently blank an intro it omitted.
            fields = {k: spec[k] for k in ("title", "intro", "published")
                      if k in spec}
            differing = {k: v for k, v in fields.items()
                         if (bool(v) if k == "published" else v) != existing.get(k)}
            if differing and not dry_run:
                coll.update_collection(conn, slug, differing)
            for key in differing:
                changes.append(f"{key} updated")

        films = spec.get("films") or []
        results = await _fetch(films) if films else []

        before = {e["tmdb_id"]: e for e in
                  (coll.entries_for(conn, collection_id) if collection_id else [])}
        keep: set[int] = set()

        for position, (film, meta) in enumerate(results):
            if meta is None:
                continue
            keep.add(meta["tmdb_id"])
            blurb = film.get("blurb")
            prior = before.get(meta["tmdb_id"])
            if prior is None:
                changes.append(f"+ {meta['title']} ({meta.get('year')})")
            elif blurb is not None and blurb != prior.get("blurb"):
                changes.append(f"~ {meta['title']} blurb rewritten")
            elif prior.get("position") != position:
                changes.append(f"~ {meta['title']} moved to {position}")
            if not dry_run and collection_id:
                coll.upsert_entry(conn, collection_id, meta,
                                  blurb=blurb, position=position)

        # Pruning is opt-in: a payload listing five films is far more often a
        # partial edit than an instruction to delete everything else.
        #
        # A film TMDB refused to serve is missing from `keep` because the
        # network failed, not because the author dropped it — pruning on that
        # basis would delete a film, and its blurb, over a transient 502. A
        # partial fetch therefore cancels the prune and says so.
        failed = sum(1 for _, meta in results if meta is None)
        if spec.get("prune") and failed:
            print(f"  ! {failed} film(s) failed to fetch — skipping prune so "
                  f"nothing is deleted on incomplete data", file=sys.stderr)
        elif spec.get("prune") and collection_id:
            for tmdb_id, entry in before.items():
                if tmdb_id in keep:
                    continue
                changes.append(f"- {entry['title']} removed")
                if not dry_run:
                    coll.delete_entry(conn, collection_id, entry["id"])

        if not changes:
            print(f"{slug}: no changes")
        else:
            print(f"{slug}: {'would apply' if dry_run else 'applied'} "
                  f"{len(changes)} change(s)")
            for line in changes:
                print(f"  {line}")
        return 0
    finally:
        conn.close()


def _dump(slug: str) -> int:
    conn = db.connect()
    try:
        collection = coll.get_by_slug(conn, slug)
        if not collection:
            print(f"no collection with slug {slug!r}", file=sys.stderr)
            return 1
        entries = coll.entries_for(conn, collection["id"])
        payload = {
            "slug": collection["slug"],
            "title": collection["title"],
            "intro": collection["intro"],
            "kind": collection["kind"],
            "origin": collection["origin"],
            "published": collection["published"],
            "films": [
                # `title` and `year` are comments to the human editing this
                # file; apply keys on `tmdb` alone and re-reads the rest.
                {"tmdb": e["tmdb_id"], "title": e["title"], "year": e["year"],
                 "blurb": e["blurb"]}
                for e in entries
            ],
        }
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    finally:
        conn.close()


def _list() -> int:
    conn = db.connect()
    try:
        for c in coll.list_collections(conn, include_unpublished=True):
            n = len(coll.entries_for(conn, c["id"]))
            state = "published" if c["published"] else "draft"
            print(f"{c['slug']:34} {c['origin']:9} {state:9} {n:3} films  {c['title']}")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.collection_tool",
                                     description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list every collection")
    dump = sub.add_parser("dump", help="print one collection as an apply payload")
    dump.add_argument("slug")
    apply_p = sub.add_parser("apply", help="create or update from JSON on stdin")
    apply_p.add_argument("--dry-run", action="store_true",
                         help="report what would change without writing")

    args = parser.parse_args(argv)
    if args.command == "list":
        return _list()
    if args.command == "dump":
        return _dump(args.slug)
    return asyncio.run(_apply(_load(sys.stdin), dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
