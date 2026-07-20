"""Seerr (Overseerr/Jellyseerr) auto-request integration.

Optional and entirely additive. When SEERR_URL / SEERR_API_KEY are configured,
adding a film that isn't already on Plex submits a request to the Seerr instance
so it gets fetched automatically.

Design rules:
  * Never raise into the add-to-backlog path. Every function returns a status
    dict and degrades to {"status": "failed", ...} on any error, so a Seerr
    outage can never break the core "add a suggestion" action.
  * De-dupe before (and as a backstop, during) the request: query the film's
    current media status so we don't fire a request for something already
    requested/available, and treat a 409/"already exists" response as such.

Status vocabulary returned to callers (and stored on movies.seerr_status):
    disabled    Seerr not configured (feature off)
    in_library  already on Plex; no Seerr request made (decided by the caller)
    available   Seerr reports the film is already available
    requested   newly requested, OR already pending/processing in Seerr
    failed      Seerr was unreachable or errored (film still added)
"""
import logging

import httpx

from . import config

log = logging.getLogger("filmclub.seerr")

# Seerr mediaInfo.status enum:
#   1 unknown, 2 pending, 3 processing, 4 partially available, 5 available
_STATUS_AVAILABLE = {4, 5}
_STATUS_PENDING = {2, 3}


def is_configured() -> bool:
    return bool(config.SEERR_URL and config.SEERR_API_KEY)


def _headers() -> dict:
    return {
        "X-Api-Key": config.SEERR_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _detail(resp: httpx.Response) -> str | None:
    """Best-effort human-readable message out of a Seerr error response."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return (resp.text or "").strip() or None
    if isinstance(body, dict):
        return body.get("message") or body.get("detail") or None
    return None


async def _existing_status(client: httpx.AsyncClient, tmdb_id: int) -> str | None:
    """Return 'available'/'requested' if Seerr already knows this film, else None.

    Best-effort de-dupe. Any error (including the film being unknown to Seerr)
    returns None so the caller proceeds to request it.
    """
    try:
        r = await client.get(f"{config.SEERR_URL}/api/v1/movie/{tmdb_id}")
        if r.status_code != 200:
            return None
        media = (r.json() or {}).get("mediaInfo")
        if not media:
            return None
        status = media.get("status")
        if status in _STATUS_AVAILABLE:
            return "available"
        if status in _STATUS_PENDING:
            return "requested"
    except Exception as e:  # noqa: BLE001 — de-dupe is optional
        log.debug("Seerr status lookup for tmdb=%s failed: %s", tmdb_id, e)
    return None


async def request_movie(tmdb_id: int) -> dict:
    """Submit a movie request to Seerr. Returns a status dict. Never raises.

    Checks the film's current status first (de-dupe), then POSTs a request,
    treating a 409/"already exists" as an existing request rather than a failure.
    """
    if not is_configured():
        return {"status": "disabled", "detail": None}
    try:
        async with httpx.AsyncClient(timeout=config.SEERR_TIMEOUT, headers=_headers()) as client:
            existing = await _existing_status(client, tmdb_id)
            if existing:
                return {"status": existing, "detail": "already known to Seerr"}

            r = await client.post(
                f"{config.SEERR_URL}/api/v1/request",
                json={"mediaType": "movie", "mediaId": int(tmdb_id)},
            )
        if r.status_code in (200, 201):
            return {"status": "requested", "detail": None}
        detail = _detail(r)
        # Seerr answers an already-requested film with 409, and some builds use
        # a 400 whose message mentions "already". Treat both as a dedupe hit.
        if r.status_code == 409 or (r.status_code == 400 and detail and "already" in detail.lower()):
            return {"status": "requested", "detail": detail or "already requested"}
        log.warning("Seerr request for tmdb=%s returned %s: %s", tmdb_id, r.status_code, detail)
        return {"status": "failed", "detail": detail or f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001 — must never break the add flow
        log.warning("Seerr request for tmdb=%s failed: %s", tmdb_id, e)
        return {"status": "failed", "detail": str(e)}
