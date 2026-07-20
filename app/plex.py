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
                        rt_tmdb=rt_tmdb, rt_imdb=rt_imdb,
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


def library_rating_key(tmdb_id: int | None, imdb_id: str | None) -> str | None:
    """Return the cached Plex ratingKey for a movie, when known."""
    rk = _library["rk_tmdb"].get(tmdb_id)
    if rk is None and imdb_id:
        rk = _library["rk_imdb"].get(imdb_id)
    return str(rk) if rk is not None else None


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
