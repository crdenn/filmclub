"""Deterministic per-member colours.

A member's colour is derived from their plex_id so it is stable across
restarts, re-seeds, and machines. Used for chart series and small UI accents.
The palette is chosen to stay distinct against a dark background and to remain
legible when used as a thin accent rather than a fill.
"""
import hashlib

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


def color_for(plex_id: str) -> str:
    """Stable colour for a plex_id.

    Uses a hash so ordering of member creation doesn't matter and the same
    person always gets the same colour.
    """
    h = hashlib.sha256(str(plex_id).encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(PALETTE)
    return PALETTE[idx]
