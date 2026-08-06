# Film Club — collection authoring session

Paste this whole file as the opening message of a fresh Claude Code session on
the Mac. It is written to be self-contained: assume the session starts knowing
nothing.

---

## What you are doing

I run a small, self-hosted film club app ("Film Club Tracker") on an unRAID
server at home. One feature is **curated collections** — themed, essay-style
pages of films, each with an introduction and a short blurb per film. They are
the editorial heart of the site.

Your job in this session is to **write new collections and revise existing
ones**, then apply them to the live server yourself over SSH. I am not looking
for a list of titles to type in — I want you to pick the films, write the prose,
and ship it.

Talk me through your picks, but don't wait for permission on every detail. If
you need a decision only I can make, ask; otherwise proceed.

## Connecting to the server

The app runs in a Docker container called `filmclub` on the unRAID box. SSH in
as root, then work through `docker exec`.

```bash
# At home (normal case) — local network
ssh -p 2222 -i ~/.ssh/<key> root@192.168.1.152

# Away from home — Tailscale
ssh -p 2222 -i ~/.ssh/<key> root@100.72.79.93
```

Port is **2222**, user is **root**. Substitute whichever key is set up on this
Mac. If `192.168.1.152` times out you are probably not on the home network —
fall back to the Tailscale address.

Sanity check before doing anything else:

```bash
ssh -p 2222 -i ~/.ssh/<key> root@192.168.1.152 'docker ps --filter name=filmclub --format "{{.Names}} {{.Status}}"'
```

## The tool you will use

Everything goes through `app/collection_tool.py`, which ships inside the
container. Four commands:

```bash
# What exists right now, in index order. A leading * means hand-placed.
docker exec -e PYTHONPATH=/app filmclub python -m app.collection_tool list

# Read one collection as an editable JSON document
docker exec -e PYTHONPATH=/app filmclub python -m app.collection_tool dump <slug>

# Create or update from JSON on stdin
docker exec -i -e PYTHONPATH=/app filmclub python -m app.collection_tool apply < payload.json
docker exec -i -e PYTHONPATH=/app filmclub python -m app.collection_tool apply --dry-run < payload.json

# Arrange the running order of the *index* (not films within a collection)
docker exec -e PYTHONPATH=/app filmclub python -m app.collection_tool order slug-a slug-b slug-c
```

Wrap each of those in the `ssh ... '<command>'` from above. **Always `--dry-run`
first** and show me the diff before writing.

`dump` emits exactly the shape `apply` accepts, so revising is a round trip:
dump → edit the JSON → apply.

### Payload shape

```json
{
  "slug":      "the-west-revised",
  "title":     "The West, Revised",
  "kind":      "picked",
  "origin":    "generated",
  "published": true,
  "reorder":   true,
  "prune":     false,
  "films": [
    {"tmdb": 3114, "blurb": "Ford's seventh trip to Monument Valley…"}
  ]
}
```

- `apply` also accepts a **JSON array** of these, so a sweep across several
  collections is one command.
- A payload may list **only the films it changes**. Anything not mentioned keeps
  its blurb and its position.
- `reorder: true` makes the payload's order the running order. Use it when
  creating a collection; leave it off for edits.
- `prune: true` deletes entries absent from the payload. Off by default. It is
  automatically cancelled if any TMDB fetch failed, so a network blip can never
  delete somebody's writing.
- `origin` should be `"generated"` for anything you write — that is what every
  existing collection uses, and it makes the page read-only in the browser UI so
  nobody accidentally overwrites your prose by clicking on it.
- New collections default to unpublished; set `"published": true` to make them
  live immediately.

## Picking films

**Only pick films that are actually on my Plex server.** Check before
committing to a list — a collection full of things I can't watch is useless.
This resolves TMDB ids *and* checks the library in one pass:

```python
# ssh ... 'docker exec -e PYTHONPATH=/app filmclub python -c "..."'
import asyncio
from app import tmdb, plex

TITLES = [("The Searchers", 1956), ("Unforgiven", 1992)]

async def main():
    await plex.refresh_library()
    for title, year in TITLES:
        res = await tmdb.search(title, limit=4)
        pick = next((r for r in res if r["year"] == year), res[0] if res else None)
        if not pick:
            print(f"NOT FOUND: {title}"); continue
        on_plex = plex.library_match(pick["tmdb_id"], None) is not None
        print(f"{on_plex}\t{pick['tmdb_id']}\t{pick['title']} ({pick['year']})")

asyncio.run(main())
```

Check a generous candidate list — 25–35 titles — then curate down from what came
back `True`. Tell me what you had to drop for not being on the server; I may go
and add it.

## House style

Read two or three existing collections with `dump` before writing anything, and
match them. The essentials:

**Every collection needs a real thesis**, not a category. Not "war films" but an
argument the selection makes. Existing ones: films that turn spectatorship
itself into the subject; endurance-length cinema; giallo's evolution; films that
were nearly destroyed by censors; the western dismantling its own mythology;
comedy as precision engineering.

**Intro**: 2–4 sentences setting up that thesis. Reads like a person, not a
back-cover blurb.

**Blurbs**: 2–3 sentences, roughly 50–65 words. The pattern that works is *one
concrete, verifiable fact* — a production detail, a technical choice, a piece of
history — plus *a judgement*. Never plot summary.

> Michael Mann shot this on early digital specifically because film stock could
> not hold the detail of a Los Angeles night, and it shows — you can see down
> every side street. A two-hander in a taxi that keeps finding new rooms to open
> into.

Dry, confident, occasionally funny. British spelling. Never breathless, never
"a rollercoaster ride". Assume the reader is a grown-up who likes film.

**Verify your facts.** The style depends on specifics being true. If you are not
certain a production anecdote is real, cut it or look it up.

### Absolutely no spoilers

This is a hard rule; I had to have a whole pass done to strip them out.

Do not reveal endings, deaths, twists, reveals, or late-film turns. Do not say
"the final shot", "the ending", or describe the mechanism of a surprise. Do not
flag that a twist exists — telling someone to watch closely is itself a spoiler.
Tonal warnings ("more brutal than its reputation suggests") are fine and useful;
plot is not.

Premise is fair game — roughly, anything in the first ten minutes or on the
poster.

## Ordering the index

`order` sets the sequence of collections on the front page. Slugs you name are
placed in that order; anything unnamed falls in behind, newest first. Naming an
unknown slug refuses the whole arrangement rather than half-applying it.

This is about the front page only — to reorder *films within* a collection, use
`apply` with `reorder: true`.

I can also do this myself in the browser (Collections → ⋯ → Arrange
collections), so don't feel you have to be asked. If I say "put the funny one
first", just run `order` — you have the whole list from `list`.

## Working notes

- The app runs migrations at startup, so after any deploy the container restarts
  and the schema catches up. If the tool ever errors with `no such column`, the
  container is running older code than the database expects — tell me.
- Changes are live the moment they are applied. There is no staging.
- The repo lives at `/mnt/user/code/filmclub` on the server (and is a GitHub
  repo, `crdenn/filmclub`). You should not normally need to touch it — the tool
  is already inside the container.
- If you want to keep payloads around, put them in the repo directory on the
  server or locally on the Mac; don't leave them in `/tmp` inside the container.

## Start here

1. Connect and run `list`.
2. `dump` two or three collections to absorb the voice.
3. Ask me what I'm in the mood for, or propose two or three thesis ideas with a
   sample of what would be in each.
4. Check availability on Plex, write it, `--dry-run`, show me, then apply.
