"""Bidirectional per-user rating synchronization with Plex.

Outbound writes use the member's encrypted Plex token. Inbound changes arrive
as Plex `media.rate` webhooks. Both directions are best-effort: Film Club's core
rating workflow remains available when Plex is missing or unreachable.
"""
import logging
import sqlite3
from typing import Any

import httpx

from . import config, db, plex, service
from .token_crypto import decrypt_plex_token

log = logging.getLogger("filmclub.plex_ratings")


async def push_rating(movie: sqlite3.Row | dict, member_id: int, score: float) -> dict:
    """Write one Film Club rating to Plex as that member. Never raises."""
    conn = db.connect()
    try:
        member = db.query_one(
            conn,
            "SELECT plex_token_encrypted, plex_rating_sync_enabled "
            "FROM members WHERE id = ?",
            (member_id,),
        )
    finally:
        conn.close()
    if member and not member["plex_rating_sync_enabled"]:
        return {"status": "disabled_by_user"}
    token = decrypt_plex_token(member["plex_token_encrypted"] if member else None)
    if not token:
        return {"status": "not_connected"}
    if not config.PLEX_URL:
        return {"status": "disabled"}

    mv = dict(movie)
    rating_key = plex.library_rating_key(mv.get("tmdb_id"), mv.get("imdb_id"))
    if not rating_key:
        rating_key = await plex.rating_key_live(
            mv.get("tmdb_id"), mv.get("imdb_id"), mv.get("title"))
    if not rating_key:
        return {"status": "not_in_library"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.put(
                f"{config.PLEX_URL}/:/rate",
                params={
                    "identifier": "com.plexapp.plugins.library",
                    "key": rating_key,
                    "rating": float(score) * 2,
                },
                headers=plex.request_headers(token),
            )
            response.raise_for_status()
        return {"status": "synced"}
    except Exception as e:  # noqa: BLE001 — local rating must remain saved
        log.warning("Plex rating push failed for movie=%s member=%s: %s",
                    mv.get("id"), member_id, e)
        return {"status": "failed"}


def _member_for_webhook(conn: sqlite3.Connection, account: dict) -> sqlite3.Row | None:
    account_id = account.get("id")
    if account_id is not None:
        row = db.query_one(
            conn, "SELECT * FROM members WHERE plex_account_id = ?", (str(account_id),))
        if row:
            return row
    title = (account.get("title") or "").strip()
    if not title:
        return None
    rows = db.query_all(
        conn,
        """SELECT * FROM members WHERE username = ? COLLATE NOCASE
           AND plex_id NOT LIKE 'dev:%' AND plex_id NOT LIKE 'seed:%'
           AND plex_id NOT LIKE 'import:%'""",
        (title,),
    )
    return rows[0] if len(rows) == 1 else None


async def _movie_for_webhook(conn: sqlite3.Connection,
                             metadata: dict) -> sqlite3.Row | None:
    tmdb_ids, imdb_ids = plex.external_ids_from_metadata(metadata)
    rating_key = metadata.get("ratingKey")
    if not (tmdb_ids or imdb_ids) and rating_key is not None:
        tmdb_ids, imdb_ids = await plex.external_ids_for_rating_key(str(rating_key))

    for tmdb_id in tmdb_ids:
        row = db.query_one(conn, "SELECT * FROM movies WHERE tmdb_id = ?", (tmdb_id,))
        if row:
            return row
    for imdb_id in imdb_ids:
        row = db.query_one(conn, "SELECT * FROM movies WHERE imdb_id = ?", (imdb_id,))
        if row:
            return row
    return None


async def apply_webhook(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict:
    """Apply a validated Plex webhook payload. Never writes unrelated media."""
    if payload.get("event") != "media.rate":
        return {"status": "ignored", "reason": "event"}
    server = payload.get("Server") or {}
    if str(server.get("uuid") or "") != config.PLEX_MACHINE_ID:
        return {"status": "ignored", "reason": "server"}
    metadata = payload.get("Metadata") or {}
    if metadata.get("type") != "movie":
        return {"status": "ignored", "reason": "media_type"}

    try:
        plex_score = float(metadata.get("userRating"))
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "rating"}
    score = plex_score / 2
    steps = round(score * 2)
    if steps < 1 or steps > 10 or abs(steps / 2 - score) > 1e-9:
        return {"status": "ignored", "reason": "rating"}

    member = _member_for_webhook(conn, payload.get("Account") or {})
    if not member:
        return {"status": "ignored", "reason": "member"}
    if not member["plex_rating_sync_enabled"]:
        return {"status": "ignored", "reason": "member_disabled"}
    movie = await _movie_for_webhook(conn, metadata)
    if not movie:
        return {"status": "ignored", "reason": "movie"}
    if movie["status"] not in ("scheduled", "watched"):
        return {"status": "ignored", "reason": "movie_status"}

    changed = service.sync_rating_from_plex(
        conn, movie["id"], member["id"], steps / 2)
    return {
        "status": "updated" if changed else "unchanged",
        "movie_id": movie["id"],
        "member_id": member["id"],
    }
