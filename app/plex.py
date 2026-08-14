"""Plex enrichment + auth helpers.

Two responsibilities:
  1. Library enrichment: pull the movie library's GUIDs periodically and hold a
     local set of TMDB/IMDB ids so we can render an "In Library" badge with a
     deep link. If Plex is unreachable we degrade silently (hide the badge).
  2. OAuth support: pin creation/polling, identity, and the server-access check
     that is the actual authorisation signal.
"""
import asyncio
import logging
import random
import re
import time
from urllib.parse import quote

import httpx

from . import config, events

log = logging.getLogger("filmclub.plex")

# --- Library enrichment ----------------------------------------------------

# Module-level cache of ids present in the Plex library, plus the server's
# machineIdentifier (for deep links). Refreshed on a background loop.
_library = {
    "tmdb": set(),    # set[int]
    "imdb": set(),    # set[str]
    "rk_tmdb": {},    # tmdb id -> Plex ratingKey (for per-film deep links)
    "rk_imdb": {},    # imdb id -> Plex ratingKey
    "rt_tmdb": {},    # tmdb id -> Rotten Tomatoes critic/audience scores
    "rt_imdb": {},    # imdb id -> Rotten Tomatoes critic/audience scores
    # Per-film display data for surfaces that browse the server library itself
    # rather than the club's own list (the Spin page). Keyed by ratingKey so a
    # thumb request can be checked against it — see `thumb_path`.
    "movies": {},     # ratingKey -> {rating_key, tmdb_id, title, year, thumb, ...}
    "machine_id": config.PLEX_MACHINE_ID or None,
    "last_refresh": 0.0,
    "ok": False,
}

_GUID_RE = re.compile(r"(tmdb|imdb|themoviedb|com\.plexapp\.agents\.imdb)://([^/?]+)")


def _parse_guid(guid: str, tmdb: set, imdb: set) -> None:
    m = _GUID_RE.search(guid or "")
    if not m:
        return
    kind, value = m.group(1), m.group(2)
    value = value.split("?")[0]
    if kind in ("tmdb", "themoviedb"):
        if value.isdigit():
            tmdb.add(int(value))
    elif "imdb" in kind:
        if value.startswith("tt"):
            imdb.add(value)


def external_ids_from_metadata(metadata: dict) -> tuple[set[int], set[str]]:
    """Extract TMDB/IMDb ids from a Plex metadata object or webhook payload."""
    tmdb: set[int] = set()
    imdb: set[str] = set()
    _parse_guid(metadata.get("guid", ""), tmdb, imdb)
    for guid in metadata.get("Guid", []) or []:
        if isinstance(guid, dict):
            _parse_guid(guid.get("id", ""), tmdb, imdb)
        elif isinstance(guid, str):
            _parse_guid(guid, tmdb, imdb)
    return tmdb, imdb


def rotten_tomatoes_from_metadata(metadata: dict) -> dict | None:
    """Extract Plex Rotten Tomatoes values and normalize them to percentages."""
    result: dict[str, int | str] = {}
    for value_key, image_key, name in (
        ("rating", "ratingImage", "critic"),
        ("audienceRating", "audienceRatingImage", "audience"),
    ):
        image = str(metadata.get(image_key) or "")
        value = metadata.get(value_key)
        if not image.startswith("rottentomatoes://") or value is None:
            continue
        try:
            result[name] = max(0, min(100, round(float(value) * 10)))
        except (TypeError, ValueError):
            continue
        result[f"{name}_state"] = image.rsplit(".", 1)[-1]
    return result or None


async def refresh_library() -> None:
    """Rebuild the local id set from the Plex movie library. Never raises."""
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        log.info("Plex not configured; skipping library refresh")
        return
    headers = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
    tmdb: set[int] = set()
    imdb: set[str] = set()
    rk_tmdb: dict[int, str] = {}
    rk_imdb: dict[str, str] = {}
    rt_tmdb: dict[int, dict] = {}
    rt_imdb: dict[str, dict] = {}
    movies: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            # Find movie library sections.
            secs = await client.get(f"{config.PLEX_URL}/library/sections")
            secs.raise_for_status()
            sections = secs.json().get("MediaContainer", {}).get("Directory", [])
            movie_keys = [s["key"] for s in sections if s.get("type") == "movie"]

            for key in movie_keys:
                # includeGuids=1 attaches external ids (tmdb/imdb) to each item.
                r = await client.get(
                    f"{config.PLEX_URL}/library/sections/{key}/all",
                    params={"includeGuids": 1},
                )
                r.raise_for_status()
                items = r.json().get("MediaContainer", {}).get("Metadata", [])
                for it in items:
                    it_tmdb: set[int] = set()
                    it_imdb: set[str] = set()
                    _parse_guid(it.get("guid", ""), it_tmdb, it_imdb)
                    for g in it.get("Guid", []) or []:
                        _parse_guid(g.get("id", ""), it_tmdb, it_imdb)
                    rk = it.get("ratingKey")
                    rt = rotten_tomatoes_from_metadata(it)
                    if rk is not None and it.get("title"):
                        movies[str(rk)] = {
                            "rating_key": str(rk),
                            # Carried so a spun film can be saved or suggested:
                            # both are keyed by TMDB id everywhere else.
                            "tmdb_id": min(it_tmdb) if it_tmdb else None,
                            "imdb_id": min(it_imdb) if it_imdb else None,
                            "title": it.get("title"),
                            "year": it.get("year"),
                            "thumb": it.get("thumb"),
                            "summary": it.get("summary") or "",
                            "duration": it.get("duration"),
                            "content_rating": it.get("contentRating"),
                            # Present on most section listings; absent is fine,
                            # the page just omits the chips.
                            "genres": [g.get("tag") for g in (it.get("Genre") or [])
                                       if g.get("tag")][:4],
                        }
                    for t in it_tmdb:
                        tmdb.add(t)
                        if rk is not None:
                            rk_tmdb.setdefault(t, str(rk))
                        if rt:
                            rt_tmdb.setdefault(t, rt)
                    for i in it_imdb:
                        imdb.add(i)
                        if rk is not None:
                            rk_imdb.setdefault(i, str(rk))
                        if rt:
                            rt_imdb.setdefault(i, rt)
        _library.update(tmdb=tmdb, imdb=imdb, rk_tmdb=rk_tmdb, rk_imdb=rk_imdb,
                        rt_tmdb=rt_tmdb, rt_imdb=rt_imdb, movies=movies,
                        machine_id=config.PLEX_MACHINE_ID or None,
                        last_refresh=time.time(), ok=True)
        log.info("Plex library refreshed: %d tmdb ids, %d imdb ids", len(tmdb), len(imdb))
        events.broadcast({"path": "/api/metadata/plex", "client": None})
    except Exception as e:  # noqa: BLE001 — degrade, don't error requests
        _library["ok"] = False
        log.warning("Plex library refresh failed: %s", e)


async def refresh_loop() -> None:
    while True:
        await refresh_library()
        await asyncio.sleep(config.PLEX_REFRESH_INTERVAL)


def library_match(tmdb_id: int | None, imdb_id: str | None) -> dict | None:
    """Return an 'In Library' descriptor for a movie, or None.

    Includes a Plex deep link when we know the server's machine id: a per-film
    link straight to the item's page when we captured its ratingKey during the
    refresh, otherwise a fallback that just opens the server's library.
    """
    if not _library["ok"]:
        return None
    hit = (tmdb_id in _library["tmdb"]) or (imdb_id in _library["imdb"] if imdb_id else False)
    if not hit:
        return None
    machine = _library["machine_id"]
    deep_link = None
    if machine:
        rk = _library["rk_tmdb"].get(tmdb_id)
        if rk is None and imdb_id:
            rk = _library["rk_imdb"].get(imdb_id)
        if rk:
            key = quote(f"/library/metadata/{rk}", safe="")
            deep_link = f"https://app.plex.tv/desktop/#!/server/{machine}/details?key={key}"
        else:
            # No ratingKey captured — land on the server library as a fallback.
            deep_link = f"https://app.plex.tv/desktop/#!/media/{machine}/com.plexapp.plugins.library"
    rt = _library["rt_tmdb"].get(tmdb_id)
    if rt is None and imdb_id:
        rt = _library["rt_imdb"].get(imdb_id)
    return {"in_library": True, "deep_link": deep_link, "rotten_tomatoes": rt}


def library_size() -> int:
    """How many library films we hold display data for."""
    return len(_library.get("movies", {}))


def deep_link_for(rating_key: str | None) -> str | None:
    """A Plex app link straight to one library item."""
    machine = _library["machine_id"]
    if not (machine and rating_key):
        return None
    key = quote(f"/library/metadata/{rating_key}", safe="")
    return f"https://app.plex.tv/desktop/#!/server/{machine}/details?key={key}"


def random_movies(n: int, allowed_tmdb: set[int] | None = None) -> list[dict]:
    """A random sample of library films, newest cache wins.

    `allowed_tmdb` restricts the draw to a caller-supplied set of TMDB ids —
    the Spin page passes `service.spin_pool`, which drops anything the club is
    already tracking or the member has already shortlisted. Films whose Plex
    GUID carried no parseable TMDB id are skipped when a filter is given: they
    can't be excluded reliably, and can't be saved or suggested afterwards.

    Sampled without replacement, and de-duplicated by title+year first: a
    library that holds the same film twice (two editions, two files) stores
    them under different rating keys, so an id-only sample could still hand the
    caller two identical posters. Returns [] when the cache has not completed a
    refresh, which the caller surfaces rather than pretending the server is
    empty.
    """
    if not _library["ok"]:
        return []
    unique: dict[tuple, dict] = {}
    for m in _library.get("movies", {}).values():
        if allowed_tmdb is not None and m.get("tmdb_id") not in allowed_tmdb:
            continue
        unique.setdefault(((m.get("title") or "").lower(), m.get("year")), m)
    pool = list(unique.values())
    if not pool:
        return []
    picks = random.sample(pool, min(n, len(pool)))
    return [dict(m, deep_link=deep_link_for(m["rating_key"])) for m in picks]


def thumb_path(rating_key: str) -> str | None:
    """The Plex thumb path for a known library item, else None.

    Deliberately a lookup rather than a passthrough: the proxy that uses this
    talks to Plex with the *server* token, so letting a caller name an
    arbitrary path would turn it into an open request forwarder. Only keys the
    refresh actually recorded can resolve.
    """
    entry = _library.get("movies", {}).get(str(rating_key))
    if not entry:
        return None
    thumb = entry.get("thumb")
    return thumb if thumb and thumb.startswith("/") else None


# Plex stores posters at full print resolution — 2000x3000 and well over a
# megabyte is normal. A reel preloads a whole draw at once, so the originals
# would be tens of megabytes per spin; its own transcoder resizes server-side
# for a fraction of that. Sized for a 2x display at the winner's zoomed width.
THUMB_W, THUMB_H = 400, 600


async def fetch_thumb(path: str) -> tuple[bytes, str]:
    """Fetch one poster from Plex, resized. Raises on failure.

    `path` must have come from `thumb_path`; this does not validate it, so no
    caller should ever pass user input straight in.
    """
    headers = {"X-Plex-Token": config.PLEX_TOKEN}
    async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"{config.PLEX_URL}/photo/:/transcode",
                params={"width": THUMB_W, "height": THUMB_H, "minSize": 1,
                        "upscale": 0, "url": path},
            )
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "image/jpeg")
        except Exception:  # noqa: BLE001
            # Older servers and some setups have the photo transcoder disabled;
            # the full-size original is heavy but still correct.
            log.debug("Plex thumb transcode failed for %s; using original", path)
        r = await client.get(f"{config.PLEX_URL}{path}")
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")


def library_ready() -> bool:
    """True when the library cache holds a successful refresh.

    Callers that must distinguish "this film is not on the server" from "we
    currently have no idea what is on the server" need this: `library_match`
    returns None for both, which is fine for hiding an optional badge but wrong
    for deciding that an author's curated entry is broken.
    """
    return bool(_library["ok"])


def library_rating_key(tmdb_id: int | None, imdb_id: str | None) -> str | None:
    """Return the cached Plex ratingKey for a movie, when known."""
    rk = _library["rk_tmdb"].get(tmdb_id)
    if rk is None and imdb_id:
        rk = _library["rk_imdb"].get(imdb_id)
    return str(rk) if rk is not None else None


def library_ok() -> bool:
    """Whether the cached library snapshot is usable.

    False when Plex isn't configured or the last refresh failed. Callers outside
    this module ask rather than reading the cache dict directly.
    """
    return bool(_library["ok"])


def library_tmdb_ids() -> set[int]:
    """Every TMDB id currently cached from the Plex movie library.

    A copy, not the live set: the refresh loop replaces it wholesale on its own
    schedule, and a caller iterating the real object could see it swapped
    mid-loop. Library items whose GUIDs carry no TMDB id are simply absent — the
    refresh only records ids it could parse.
    """
    return set(_library["tmdb"])


def tmdb_ids_for_imdb(imdb_ids: set[str]) -> set[int]:
    """TMDB ids of library items that also carry one of these IMDb ids.

    Joined through the ratingKey both id maps were built from during the same
    refresh, so a film we only know by IMDb id still resolves to the TMDB id the
    spin pool is keyed on.
    """
    keys = {_library["rk_imdb"][i] for i in imdb_ids if i in _library["rk_imdb"]}
    if not keys:
        return set()
    return {tid for tid, rk in _library["rk_tmdb"].items() if rk in keys}


async def rating_key_live(tmdb_id: int | None, imdb_id: str | None,
                          title: str | None) -> str | None:
    """Resolve one movie's ratingKey directly from Plex. Best-effort."""
    if not (config.PLEX_URL and config.PLEX_TOKEN and title):
        return None
    headers = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            secs = await client.get(f"{config.PLEX_URL}/library/sections")
            secs.raise_for_status()
            sections = secs.json().get("MediaContainer", {}).get("Directory", [])
            movie_keys = [s["key"] for s in sections if s.get("type") == "movie"]
            for key in movie_keys:
                r = await client.get(
                    f"{config.PLEX_URL}/library/sections/{key}/all",
                    params={"title": title, "includeGuids": 1},
                )
                r.raise_for_status()
                items = r.json().get("MediaContainer", {}).get("Metadata", [])
                for item in items:
                    item_tmdb, item_imdb = external_ids_from_metadata(item)
                    if (tmdb_id in item_tmdb) or (imdb_id and imdb_id in item_imdb):
                        rk = item.get("ratingKey")
                        return str(rk) if rk is not None else None
        return None
    except Exception as e:  # noqa: BLE001 — optional sync lookup
        log.warning("Live Plex rating-key lookup for %r failed: %s", title, e)
        return None


async def external_ids_for_rating_key(rating_key: str) -> tuple[set[int], set[str]]:
    """Resolve external ids for a Plex ratingKey, cache-first then live."""
    tmdb = {mid for mid, rk in _library["rk_tmdb"].items() if str(rk) == str(rating_key)}
    imdb = {mid for mid, rk in _library["rk_imdb"].items() if str(rk) == str(rating_key)}
    if tmdb or imdb:
        return tmdb, imdb
    if not (config.PLEX_URL and config.PLEX_TOKEN):
        return tmdb, imdb
    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            headers={"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"},
        ) as client:
            r = await client.get(
                f"{config.PLEX_URL}/library/metadata/{rating_key}",
                params={"includeGuids": 1},
            )
            r.raise_for_status()
            items = r.json().get("MediaContainer", {}).get("Metadata", [])
            return external_ids_from_metadata(items[0]) if items else (set(), set())
    except Exception as e:  # noqa: BLE001 — webhook must degrade safely
        log.warning("Could not resolve Plex ratingKey %s: %s", rating_key, e)
        return set(), set()


async def library_has_live(tmdb_id: int | None, imdb_id: str | None, title: str | None) -> bool:
    """Targeted live check of whether a single film is on Plex right now.

    The `library_match` cache only refreshes on an interval, so a film added to
    Plex in the last hour reads as missing. Before we auto-request such a film we
    do one live, title-scoped lookup (cheap) and compare GUIDs, catching those
    recent additions. Best-effort: returns False on any error or if Plex isn't
    configured, and the caller then lets Seerr's own de-dupe be the backstop.
    """
    if not (config.PLEX_URL and config.PLEX_TOKEN and title):
        return False
    headers = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            secs = await client.get(f"{config.PLEX_URL}/library/sections")
            secs.raise_for_status()
            sections = secs.json().get("MediaContainer", {}).get("Directory", [])
            movie_keys = [s["key"] for s in sections if s.get("type") == "movie"]
            for key in movie_keys:
                # title= narrows the scan to likely matches; includeGuids attaches
                # the external ids we match on (same parser as the full refresh).
                r = await client.get(
                    f"{config.PLEX_URL}/library/sections/{key}/all",
                    params={"title": title, "includeGuids": 1},
                )
                r.raise_for_status()
                items = r.json().get("MediaContainer", {}).get("Metadata", [])
                tmdb: set[int] = set()
                imdb: set[str] = set()
                for it in items:
                    _parse_guid(it.get("guid", ""), tmdb, imdb)
                    for g in it.get("Guid", []) or []:
                        _parse_guid(g.get("id", ""), tmdb, imdb)
                if (tmdb_id in tmdb) or (imdb_id and imdb_id in imdb):
                    return True
        return False
    except Exception as e:  # noqa: BLE001 — degrade to "not found", never error
        log.warning("Live Plex lookup for %r failed: %s", title, e)
        return False


# --- OAuth (PIN-based) -----------------------------------------------------

PLEX_API = "https://plex.tv/api/v2"


def _plex_headers(token: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": config.PLEX_PRODUCT,
        "X-Plex-Client-Identifier": config.PLEX_CLIENT_ID,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def request_headers(token: str | None = None) -> dict:
    """Public headers helper for other Plex integration modules."""
    return _plex_headers(token)


async def create_pin() -> dict:
    """Create a strong PIN. Returns {id, code}."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{PLEX_API}/pins",
            params={"strong": "true"},
            headers=_plex_headers(),
        )
        r.raise_for_status()
        d = r.json()
        return {"id": d["id"], "code": d["code"]}


def auth_url(code: str) -> str:
    forward = f"{config.APP_URL}/auth/callback"
    return (
        "https://app.plex.tv/auth#?"
        f"clientID={config.PLEX_CLIENT_ID}"
        f"&code={code}"
        f"&forwardUrl={forward}"
        f"&context%5Bdevice%5D%5Bproduct%5D={config.PLEX_PRODUCT.replace(' ', '%20')}"
    )


async def poll_pin(pin_id: int, code: str, attempts: int = 20, delay: float = 1.0) -> str | None:
    """Poll a PIN until it carries an authToken, or give up. Returns the token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(attempts):
            r = await client.get(
                f"{PLEX_API}/pins/{pin_id}",
                params={"code": code},
                headers=_plex_headers(),
            )
            if r.status_code == 200:
                token = r.json().get("authToken")
                if token:
                    return token
            await asyncio.sleep(delay)
    return None


async def get_user(token: str) -> dict:
    """Exchange a token for the Plex account identity."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{PLEX_API}/user", headers=_plex_headers(token))
        r.raise_for_status()
        d = r.json()
        return {
            "uuid": str(d.get("uuid") or d.get("id")),
            "account_id": str(d["id"]) if d.get("id") is not None else None,
            "username": d.get("username") or d.get("title") or "plexuser",
            "email": d.get("email"),
            "thumb": d.get("thumb"),
        }


async def has_server_access(token: str) -> bool:
    """The real authorisation check.

    A valid Plex account is not the same as club membership — any Plex account
    authenticates. Access to *our* server is the signal. Confirm our
    machineIdentifier appears in the user's resource list.
    """
    if not config.PLEX_MACHINE_ID:
        log.error("PLEX_MACHINE_ID not set — cannot authorise anyone")
        return False
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{PLEX_API}/resources",
            params={"includeHttps": 1},
            headers=_plex_headers(token),
        )
        r.raise_for_status()
        for res in r.json():
            if res.get("clientIdentifier") == config.PLEX_MACHINE_ID:
                return True
    return False
