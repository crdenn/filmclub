"""Stable, distinct per-member colours.

A member's preferred colour is derived from their plex_id, then the allocator
walks the palette to avoid colours already used by another member. Used for
chart series and small UI accents. The palette is chosen to stay distinct
against a dark background and to remain legible as a compact avatar fill.
"""
import colorsys
import hashlib
import sqlite3
from collections.abc import Iterable

# Curated, reasonably distinct on dark backgrounds. Ordered so the first six
# (the common club size) are maximally separated in hue.
PALETTE = [
    "#e5484d",  # red
    "#4cc38a",  # green
    "#5b8def",  # blue
    "#e5a13a",  # amber
    "#a97bf0",  # violet
    "#3fb8c4",  # teal
    "#ec6cb9",  # pink
    "#8ab63f",  # lime
    "#f0803c",  # orange
    "#6e79d6",  # indigo
    "#c04ac0",  # magenta
    "#c9a227",  # gold
]


def _normalized(colors: Iterable[str]) -> set[str]:
    return {str(color).strip().lower() for color in colors if str(color).strip()}


def _fallback_color(plex_id: str, attempt: int) -> str:
    """Generate a readable deterministic colour after the palette is full."""
    digest = hashlib.sha256(f"{plex_id}:{attempt}".encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535
    saturation = 0.62 + (digest[2] / 255) * 0.16
    lightness = 0.52 + (digest[3] / 255) * 0.10
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def color_for(plex_id: str, used_colors: Iterable[str] = ()) -> str:
    """Return a stable colour not present in ``used_colors``.

    The identity hash chooses a preferred palette position. Collisions walk the
    palette in order, which keeps the normal small-club colours visually
    distinct. A deterministic generated fallback guarantees unique stored
    values even when the member count exceeds the curated palette.
    """
    used = _normalized(used_colors)
    h = hashlib.sha256(str(plex_id).encode("utf-8")).hexdigest()
    start = int(h[:8], 16) % len(PALETTE)
    for offset in range(len(PALETTE)):
        candidate = PALETTE[(start + offset) % len(PALETTE)]
        if candidate.lower() not in used:
            return candidate
    attempt = 0
    while True:
        candidate = _fallback_color(plex_id, attempt)
        if candidate.lower() not in used:
            return candidate
        attempt += 1


def available_color(conn: sqlite3.Connection, plex_id: str) -> str:
    """Allocate a colour distinct from every member currently in ``conn``."""
    used = (row[0] for row in conn.execute("SELECT color FROM members"))
    return color_for(plex_id, used)


def reconcile_member_colors(conn: sqlite3.Connection) -> int:
    """Repair duplicate or blank member colours while preserving unique ones.

    Earlier releases hashed directly into the finite palette, so collisions
    were expected. The oldest member keeps a duplicated colour and later rows
    receive the next available stable colour. The caller owns the transaction.
    """
    used: set[str] = set()
    updates: list[tuple[str, int]] = []
    for row in conn.execute("SELECT id, plex_id, color FROM members ORDER BY id"):
        current = str(row["color"] or "").strip()
        normalized = current.lower()
        if current and normalized not in used:
            used.add(normalized)
            continue
        replacement = color_for(row["plex_id"], used)
        updates.append((replacement, row["id"]))
        used.add(replacement.lower())
    if updates:
        conn.executemany("UPDATE members SET color = ? WHERE id = ?", updates)
    return len(updates)
