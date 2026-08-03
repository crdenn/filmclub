"""Domain services: coverage/eligibility, backlog & watched assembly, ratings.

The eligibility rule is the heart of the app:

    A film is eligible to be picked if at least one member has NOT seen it.

Nuances that matter:
  * A member with no prior_views row is *unknown*, not unseen. Unknown is not
    evidence of eligibility.
  * "Ineligible" means every member has a prior_views row with seen=1.
  * Everything in between (some unknowns, nobody confirmed unseen) is
    "unconfirmed": we surface it but do not claim it's pickable.
"""
import json
import sqlite3
from datetime import date, timedelta

from . import config, db, plex


def all_members(conn: sqlite3.Connection) -> list[dict]:
    rows = db.query_all(conn, "SELECT * FROM members ORDER BY username COLLATE NOCASE")
    return [db.member_public(r) for r in rows]


def _coverage(members: list[dict], prior_rows: list[sqlite3.Row]) -> dict:
    """Compute unseen coverage for one movie.

    Returns per-bucket member id lists plus the derived eligibility flag.
    """
    by_member = {r["member_id"]: r["seen"] for r in prior_rows}
    seen, not_seen, unknown = [], [], []
    for m in members:
        state = by_member.get(m["id"])
        if state is None:
            unknown.append(m["id"])
        elif state:
            seen.append(m["id"])
        else:
            not_seen.append(m["id"])

    total = len(members)
    if total and len(seen) == total:
        eligibility = "ineligible"          # everyone has seen it
    elif not_seen:
        eligibility = "eligible"            # at least one confirmed unseen
    else:
        eligibility = "unconfirmed"         # only unknowns, nobody confirmed unseen

    return {
        "total_members": total,
        "seen_ids": seen,
        "not_seen_ids": not_seen,
        "unknown_ids": unknown,
        "unseen_count": len(not_seen),
        "unknown_count": len(unknown),
        "eligibility": eligibility,
    }


def coverage_for(conn: sqlite3.Connection, movie_id: int) -> dict:
    """Public: recompute one movie's coverage (used after a toggle)."""
    members = all_members(conn)
    prior = db.query_all(
        conn, "SELECT member_id, seen FROM prior_views WHERE movie_id = ?", (movie_id,))
    return _coverage(members, prior)


def _in_library(movie: dict) -> dict | None:
    imdb = movie.get("imdb_id")
    return plex.library_match(movie.get("tmdb_id"), imdb)


def backlog(conn: sqlite3.Connection, member_id: int, sort: str = "seconds",
            eligible_only: bool = False) -> list[dict]:
    """All suggested-but-unwatched films with coverage, votes, sorted/filtered.

    `member_id` is the requesting member, used to flag whether they've already
    seconded each film (and, implicitly, that a suggester can't second their own).
    """
    members = all_members(conn)
    movies = [db.movie_base(r) for r in db.query_all(
        conn, "SELECT * FROM movies WHERE status = 'suggested'")]

    prior = db.query_all(conn, "SELECT movie_id, member_id, seen FROM prior_views")
    prior_by_movie: dict[int, list] = {}
    for r in prior:
        prior_by_movie.setdefault(r["movie_id"], []).append(r)

    votes_by_movie = _votes_by_movie(conn)
    suggesters = {m["id"]: m for m in members}

    out = []
    for mv in movies:
        cov = _coverage(members, prior_by_movie.get(mv["id"], []))
        if eligible_only and cov["eligibility"] == "ineligible":
            continue
        voters = votes_by_movie.get(mv["id"], set())
        out.append({
            **mv,
            "coverage": cov,
            "suggester": suggesters.get(mv["suggested_by"]),
            "library": _in_library(mv),
            "vote_count": len(voters),
            "voter_ids": sorted(voters),
            "voted": member_id in voters,
            "can_vote": mv["suggested_by"] != member_id,
        })

    _sort_backlog(out, sort)
    return out


def _votes_by_movie(conn: sqlite3.Connection) -> dict[int, set]:
    """Map of movie_id -> set of member ids who have seconded it."""
    by_movie: dict[int, set] = {}
    for r in db.query_all(conn, "SELECT movie_id, member_id FROM votes"):
        by_movie.setdefault(r["movie_id"], set()).add(r["member_id"])
    return by_movie


def _sort_backlog(items: list[dict], sort: str) -> None:
    keymap = {
        "date": (lambda m: m["suggested_at"], True),          # newest first
        "title": (lambda m: (m["title"] or "").lower(), False),
        "year": (lambda m: m["year"] or 0, True),
        "runtime": (lambda m: m["runtime"] or 0, True),
        "unseen": (lambda m: m["coverage"]["unseen_count"], True),
        # Most-seconded first, breaking ties by unseen count so freshly-added
        # films with no votes still order sensibly.
        "seconds": (lambda m: (m["vote_count"], m["coverage"]["unseen_count"]), True),
    }
    key, reverse = keymap.get(sort, keymap["seconds"])
    items.sort(key=key, reverse=reverse)


def this_week(conn: sqlite3.Connection) -> list[dict]:
    """Films picked as the current week's watch (status 'scheduled').

    Same shape as a backlog item plus the discussion date (watched_at, set to the
    upcoming Tuesday at pick time). Rating data stays private until the movie is
    moved to watched, so this shared overview deliberately exposes none of it."""
    members = all_members(conn)
    movies = [db.movie_base(r) for r in db.query_all(
        conn, "SELECT * FROM movies WHERE status = 'scheduled' ORDER BY watched_at, id")]

    prior = db.query_all(conn, "SELECT movie_id, member_id, seen FROM prior_views")
    prior_by_movie: dict[int, list] = {}
    for r in prior:
        prior_by_movie.setdefault(r["movie_id"], []).append(r)

    suggesters = {m["id"]: m for m in members}
    out = []
    for mv in movies:
        cov = _coverage(members, prior_by_movie.get(mv["id"], []))
        out.append({
            **mv,
            "coverage": cov,
            "suggester": suggesters.get(mv["suggested_by"]),
            "library": _in_library(mv),
            "total_members": len(members),
        })
    return out


def watched(conn: sqlite3.Connection, member_id: int) -> list[dict]:
    """Watched films, most recent first, with average rating + coverage.

    `member_id` is the requesting member; each item carries `my_rated` so the
    page can flag films this member still needs to rate.
    """
    members = all_members(conn)
    total_members = len(members)
    movies = [db.movie_base(r) for r in db.query_all(
        conn, "SELECT * FROM movies WHERE status = 'watched' "
              "ORDER BY watched_at DESC, id DESC")]

    ratings = db.query_all(conn, "SELECT movie_id, member_id, score FROM ratings")
    r_by_movie: dict[int, list[float]] = {}
    my_score: dict[int, float] = {}
    for r in ratings:
        r_by_movie.setdefault(r["movie_id"], []).append(r["score"])
        if r["member_id"] == member_id:
            my_score[r["movie_id"]] = r["score"]

    suggesters = {m["id"]: m for m in members}
    out = []
    for mv in movies:
        scores = r_by_movie.get(mv["id"], [])
        out.append({
            **mv,
            "suggester": suggesters.get(mv["suggested_by"]),
            "avg_rating": round(sum(scores) / len(scores), 2) if scores else None,
            "rating_count": len(scores),
            "total_members": total_members,
            "my_rated": mv["id"] in my_score,
            "my_score": my_score.get(mv["id"]),
            "library": _in_library(mv),
        })
    return out


def member_profile(conn: sqlite3.Connection, member_id: int,
                   viewer_id: int) -> dict | None:
    """A member's public film-club activity: their suggestions, their ratings,
    and a few aggregate stats. Scheduled ratings are included only when a member
    views their own profile. Returns None if the member doesn't exist."""
    row = db.query_one(conn, "SELECT * FROM members WHERE id = ?", (member_id,))
    if not row:
        return None

    # Films this member suggested (any status). Group rating summaries remain
    # private until the film is watched.
    sug_rows = db.query_all(
        conn,
        """SELECT m.*,
                  CASE WHEN m.status = 'watched' THEN
                    (SELECT AVG(score) FROM ratings r WHERE r.movie_id = m.id)
                  END AS avg_rating,
                  CASE WHEN m.status = 'watched' THEN
                    (SELECT COUNT(*) FROM ratings r WHERE r.movie_id = m.id)
                  ELSE 0 END AS rating_count
           FROM movies m WHERE m.suggested_by = ?
           ORDER BY CASE m.status WHEN 'scheduled' THEN 0 WHEN 'suggested' THEN 1 ELSE 2 END,
                    COALESCE(m.watched_at, m.suggested_at) DESC""",
        (member_id,))
    suggestions = []
    for r in sug_rows:
        d = dict(r)
        suggestions.append({
            "id": d["id"], "title": d["title"], "year": d["year"],
            "poster_url": d["poster_url"], "status": d["status"],
            "language": d.get("language"), "library": _in_library(d),
            "avg_rating": round(d["avg_rating"], 2) if d["avg_rating"] is not None else None,
            "rating_count": d["rating_count"] or 0,
        })

    # Ratings this member has given.
    rat_rows = db.query_all(
        conn,
        """SELECT r.score, r.seen_before, r.note, r.created_at,
                  m.id AS movie_id, m.title, m.year, m.poster_url, m.status,
                  m.language, m.tmdb_id, m.imdb_id
           FROM ratings r JOIN movies m ON m.id = r.movie_id
           WHERE r.member_id = ? AND (m.status = 'watched' OR ? = ?)
           ORDER BY r.created_at DESC""",
        (member_id, member_id, viewer_id))
    ratings = [{
        "movie": {"id": r["movie_id"], "title": r["title"], "year": r["year"],
                  "poster_url": r["poster_url"], "status": r["status"],
                  "language": r["language"],
                  "library": _in_library(dict(r))},
        "score": r["score"], "seen_before": bool(r["seen_before"]),
        "note": r["note"], "created_at": r["created_at"],
    } for r in rat_rows]

    # Mean of every rating given to this member's picks (how their taste lands).
    picks_scores = [x["score"] for x in db.query_all(
        conn,
        """SELECT r.score FROM ratings r JOIN movies m ON m.id = r.movie_id
           WHERE m.suggested_by = ? AND m.status = 'watched'""",
        (member_id,))]

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    stats = {
        "suggested": len(suggestions),
        "suggested_watched": sum(1 for s in suggestions if s["status"] == "watched"),
        "suggested_backlog": sum(1 for s in suggestions if s["status"] == "suggested"),
        "ratings_count": len(ratings),
        "mean_score_given": mean([r["score"] for r in ratings]),
        "picks_mean_received": mean(picks_scores),
    }
    return {
        "member": db.member_public(row),
        "created_at": dict(row).get("created_at"),
        "stats": stats,
        "suggestions": suggestions,
        "ratings": ratings,
    }


def movie_detail(conn: sqlite3.Connection, movie_id: int, member_id: int) -> dict | None:
    row = db.query_one(conn, "SELECT * FROM movies WHERE id = ?", (movie_id,))
    if not row:
        return None
    mv = db.movie_base(row)
    members = all_members(conn)
    members_by_id = {m["id"]: m for m in members}

    # Seconding is only meaningful on backlog films; a suggester can't second
    # their own. Voters is a set of member ids who've +1'd it.
    voters = {r["member_id"] for r in db.query_all(
        conn, "SELECT member_id FROM votes WHERE movie_id = ?", (movie_id,))}

    # Coverage source depends on status.
    #   * Suggested film: the live, editable prior_views.
    #   * Watched film: reconstruct "who had seen it at watch". The authoritative
    #     per-member record is ratings.seen_before (captured when they rated);
    #     for members who haven't rated yet we fall back to the watch-time
    #     snapshot, then to unknown. This keeps the panel consistent with the
    #     first-watch/rewatch split and covers imported films (no snapshot).
    if mv["status"] == "watched":
        seen_map: dict[int, int] = {}
        if mv.get("seen_before_snapshot"):
            try:
                snap = json.loads(mv["seen_before_snapshot"])
                seen_map = {int(k): (1 if v else 0) for k, v in snap.items()}
            except (json.JSONDecodeError, TypeError):
                seen_map = {}
        for r in db.query_all(
                conn, "SELECT member_id, seen_before FROM ratings WHERE movie_id = ?", (movie_id,)):
            seen_map[r["member_id"]] = 1 if r["seen_before"] else 0  # rating wins
        prior = [{"member_id": mid, "seen": v} for mid, v in seen_map.items()]
    else:
        prior = db.query_all(
            conn, "SELECT member_id, seen FROM prior_views WHERE movie_id = ?", (movie_id,))
    cov = _coverage(members, prior)

    ratings_public = mv["status"] == "watched"
    rating_rows = db.query_all(
        conn,
        """SELECT * FROM ratings WHERE movie_id = ?
           AND (? OR member_id = ?) ORDER BY created_at""",
        (movie_id, ratings_public, member_id),
    )
    ratings = []
    for r in rating_rows:
        d = dict(r)
        ratings.append({
            "member": members_by_id.get(d["member_id"]),
            "member_id": d["member_id"],
            "score": d["score"],
            "seen_before": bool(d["seen_before"]),
            "note": d["note"],
            "created_at": d["created_at"],
        })

    first = [r["score"] for r in ratings if not r["seen_before"]] if ratings_public else []
    rewatch = [r["score"] for r in ratings if r["seen_before"]] if ratings_public else []
    rating_count = db.query_one(
        conn, "SELECT COUNT(*) c FROM ratings WHERE movie_id = ?", (movie_id,)
    )["c"]

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    return {
        **mv,
        "suggester": members_by_id.get(mv["suggested_by"]),
        "coverage": cov,
        "members": members,
        "ratings": ratings,
        "ratings_public": ratings_public,
        "rating_count": rating_count,
        "avg_rating": mean([r["score"] for r in ratings]) if ratings_public else None,
        "first_watch_mean": mean(first),
        "rewatch_mean": mean(rewatch),
        "first_watch_count": len(first),
        "rewatch_count": len(rewatch),
        "library": _in_library(mv),
        "vote_count": len(voters),
        "voter_ids": sorted(voters),
        "voted": member_id in voters,
        "can_vote": mv["status"] == "suggested" and mv["suggested_by"] != member_id,
    }


# --- mutations -------------------------------------------------------------

def set_prior_view(conn: sqlite3.Connection, movie_id: int, member_id: int, seen: bool | None) -> None:
    """Set (or clear, when seen is None) a member's prior-view state for a film."""
    if seen is None:
        db.execute(conn, "DELETE FROM prior_views WHERE movie_id = ? AND member_id = ?",
                   (movie_id, member_id))
        return
    db.execute(
        conn,
        """INSERT INTO prior_views (movie_id, member_id, seen, updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(movie_id, member_id)
           DO UPDATE SET seen = excluded.seen, updated_at = datetime('now')""",
        (movie_id, member_id, 1 if seen else 0),
    )


def set_display_name(conn: sqlite3.Connection, member_id: int, name: str | None) -> None:
    """Set (or clear, when name is falsy) a member's chosen display name.

    Cleared back to NULL, the member falls back to their Plex username everywhere.
    """
    cleaned = (name or "").strip() or None
    db.execute(conn, "UPDATE members SET display_name = ? WHERE id = ?",
               (cleaned, member_id))


def set_discord_user_id(conn: sqlite3.Connection, member_id: int, value: str | None) -> None:
    """Set (or clear) a member's Discord user id, admin-entered for @mentions
    in the weekly reminder digest."""
    cleaned = (value or "").strip() or None
    db.execute(conn, "UPDATE members SET discord_user_id = ? WHERE id = ?",
               (cleaned, member_id))


THEMES = ("system", "dark", "light")


def set_theme(conn: sqlite3.Connection, member_id: int, theme: str) -> None:
    """Set the member's visual mode.

    Whitelisted here as well as in the request model so a value can never reach
    the column from another call site.
    """
    if theme not in THEMES:
        raise ValueError(f"Unknown theme: {theme!r}")
    db.execute(conn, "UPDATE members SET theme = ? WHERE id = ?", (theme, member_id))


def set_plex_rating_sync_enabled(conn: sqlite3.Connection, member_id: int,
                                 enabled: bool) -> None:
    """Set the member's opt-in state for future rating changes in both directions."""
    db.execute(
        conn,
        "UPDATE members SET plex_rating_sync_enabled = ? WHERE id = ?",
        (1 if enabled else 0, member_id),
    )


def set_vote(conn: sqlite3.Connection, movie_id: int, member_id: int, voted: bool) -> list[int]:
    """Add or remove a member's second on a backlog film.

    Returns the full list of member ids who now second the film, so the caller can
    render *who* is keen rather than just how many — the count is len() of this.

    The caller is responsible for the eligibility checks (film is on the backlog,
    member isn't the suggester); this just applies the toggle idempotently.
    """
    if voted:
        db.execute(
            conn,
            "INSERT OR IGNORE INTO votes (movie_id, member_id) VALUES (?, ?)",
            (movie_id, member_id),
        )
    else:
        db.execute(conn, "DELETE FROM votes WHERE movie_id = ? AND member_id = ?",
                   (movie_id, member_id))
    rows = db.query_all(
        conn, "SELECT member_id FROM votes WHERE movie_id = ? ORDER BY member_id",
        (movie_id,))
    return [r["member_id"] for r in rows]


def todo_counts(conn: sqlite3.Connection, member_id: int) -> dict:
    """Per-member reminder counts for the nav badges.

    `backlog` = suggested films this member hasn't marked seen/not-seen (no
    prior_views row). `watched` = watched films this member hasn't rated.
    """
    backlog_unmarked = db.query_one(
        conn,
        """SELECT COUNT(*) c FROM movies m WHERE m.status = 'suggested'
           AND NOT EXISTS (SELECT 1 FROM prior_views p
                           WHERE p.movie_id = m.id AND p.member_id = ?)""",
        (member_id,))["c"]
    watched_unrated = db.query_one(
        conn,
        """SELECT COUNT(*) c FROM movies m WHERE m.status = 'watched'
           AND NOT EXISTS (SELECT 1 FROM ratings r
                           WHERE r.movie_id = m.id AND r.member_id = ?)""",
        (member_id,))["c"]
    return {"backlog": backlog_unmarked, "watched": watched_unrated}


def todo_details(conn: sqlite3.Connection, member_id: int, *, cap: int = 5) -> dict:
    """Per-member reminder detail: which specific films are outstanding.

    Same eligibility rules as todo_counts, but returns titles (oldest first,
    capped) instead of a bare count, for surfaces that need to say *what* is
    outstanding, not just *how many* (e.g. the Discord reminder digest).
    """
    backlog_rows = db.query_all(
        conn,
        """SELECT m.title FROM movies m WHERE m.status = 'suggested'
           AND NOT EXISTS (SELECT 1 FROM prior_views p
                           WHERE p.movie_id = m.id AND p.member_id = ?)
           ORDER BY m.suggested_at, m.id""",
        (member_id,))
    watched_rows = db.query_all(
        conn,
        """SELECT m.title FROM movies m WHERE m.status = 'watched'
           AND NOT EXISTS (SELECT 1 FROM ratings r
                           WHERE r.movie_id = m.id AND r.member_id = ?)
           ORDER BY m.watched_at, m.id""",
        (member_id,))

    def _shape(rows):
        titles = [r["title"] for r in rows]
        return {"count": len(titles), "titles": titles[:cap], "overflow": max(0, len(titles) - cap)}

    return {"backlog": _shape(backlog_rows), "watched": _shape(watched_rows)}


def add_suggestion(conn: sqlite3.Connection, meta: dict, member_id: int,
                   pitch: str | None = None) -> int:
    """Snapshot TMDB metadata into a new suggested movie. Returns movie id.

    `pitch` is the suggester's optional elevator pitch; blank/whitespace stores
    NULL so "no pitch" and "empty pitch" are the same thing downstream.
    """
    pitch = (pitch or "").strip() or None
    cur = db.execute(
        conn,
        """INSERT INTO movies
           (tmdb_id, title, year, poster_url, backdrop_url, runtime, director,
            language, content_rating, overview, genres, imdb_id, suggested_by,
            pitch, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'suggested')""",
        (
            meta.get("tmdb_id"),
            meta.get("title"),
            meta.get("year"),
            meta.get("poster_url"),
            meta.get("backdrop_url"),
            meta.get("runtime"),
            meta.get("director"),
            meta.get("language"),
            meta.get("content_rating") or "",
            meta.get("overview"),
            json.dumps(meta.get("genres") or []),
            meta.get("imdb_id"),
            member_id,
            pitch,
        ),
    )
    return cur.lastrowid


def set_seerr_status(conn: sqlite3.Connection, movie_id: int, status: str | None) -> None:
    """Record the outcome of a Seerr auto-request on the movie row."""
    db.execute(conn, "UPDATE movies SET seerr_status = ? WHERE id = ?", (status, movie_id))


def _next_tuesday(today: date | None = None) -> str:
    """The upcoming Tuesday as an ISO date string (the club's discussion night).

    Picking always happens for the *next* meeting, so if today is already Tuesday
    we roll forward a full week rather than returning today."""
    today = today or date.today()
    ahead = (1 - today.weekday()) % 7   # Monday=0 … Tuesday=1
    if ahead == 0:
        ahead = 7
    return (today + timedelta(days=ahead)).isoformat()


def schedule_movie(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Pick a backlog film as this week's movie (status 'suggested' -> 'scheduled').

    Sets the discussion date to the upcoming Tuesday and freezes the prior-views
    snapshot *now* (at pick time): the snapshot captures who had already seen the
    film before the club picked it, which seeds each member's 'seen before?'
    default when they rate. Members watching it during the week (flipping their
    prior_views to seen) therefore don't rewrite that historical fact.
    Returns False if the film isn't currently in the backlog."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "suggested":
        return False
    prior = db.query_all(
        conn, "SELECT member_id, seen FROM prior_views WHERE movie_id = ?", (movie_id,))
    snapshot = {str(r["member_id"]): bool(r["seen"]) for r in prior}
    db.execute(
        conn,
        "UPDATE movies SET status = 'scheduled', watched_at = ?, "
        "seen_before_snapshot = ? WHERE id = ?",
        (_next_tuesday(), json.dumps(snapshot), movie_id),
    )
    return True


def set_discuss_date(conn: sqlite3.Connection, movie_id: int, iso_date: str) -> bool:
    """Change the discussion date of this week's pick (e.g. the group moves the
    meeting off Tuesday). Only valid while the film is scheduled. `iso_date` must
    already be a validated 'YYYY-MM-DD' string. Returns False if not scheduled."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "scheduled":
        return False
    db.execute(conn, "UPDATE movies SET watched_at = ? WHERE id = ?", (iso_date, movie_id))
    return True


def archive_movie(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Close out this week's movie after the group discusses it
    (status 'scheduled' -> 'watched'). Keeps the discussion date and the
    already-frozen snapshot; ratings carry over untouched. Archiving is a single
    deliberate group action and doesn't depend on everyone having watched it, so
    a member who missed the week still sees it in Watched.
    Returns False if the film isn't the current pick."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "scheduled":
        return False
    db.execute(conn, "UPDATE movies SET status = 'watched' WHERE id = ?", (movie_id,))
    return True


def return_to_this_week(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Reopen an archived film as this week's pick (watched -> scheduled).

    This is an administrative correction: only the status changes. Discussion
    date, prior-view snapshot, ratings, notes, votes, and metadata stay intact.
    Returns False if the film isn't currently watched."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "watched":
        return False
    db.execute(conn, "UPDATE movies SET status = 'scheduled' WHERE id = ?", (movie_id,))
    return True


def unschedule_movie(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Reverse a pick: send this week's movie back to the backlog
    (status 'scheduled' -> 'suggested'). Only the lifecycle status changes;
    ratings and all other movie data remain available if it is picked again.
    Returns False if the film isn't the current pick."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "scheduled":
        return False
    db.execute(conn, "UPDATE movies SET status = 'suggested' WHERE id = ?", (movie_id,))
    return True


def unmark_watched(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Send an archived film all the way back to the backlog
    (status 'watched' -> 'suggested'). Only the lifecycle status changes;
    ratings, discussion date, snapshot, prior views, votes, and metadata remain.
    Returns False if the film isn't currently watched."""
    row = db.query_one(conn, "SELECT status FROM movies WHERE id = ?", (movie_id,))
    if not row or row["status"] != "watched":
        return False
    db.execute(conn, "UPDATE movies SET status = 'suggested' WHERE id = ?", (movie_id,))
    return True


def default_seen_before(conn: sqlite3.Connection, movie_id: int, member_id: int) -> bool:
    """The rating input's default 'had you seen this before?' value.

    Prefer the frozen watch-time snapshot; fall back to live prior_views (for
    films watched before snapshots existed); unknown defaults to False. Always
    editable by the member at rate time.
    """
    row = db.query_one(
        conn, "SELECT seen_before_snapshot FROM movies WHERE id = ?", (movie_id,))
    if row and row["seen_before_snapshot"]:
        try:
            snap = json.loads(row["seen_before_snapshot"])
            if str(member_id) in snap:
                return bool(snap[str(member_id)])
        except (json.JSONDecodeError, TypeError):
            pass
    live = db.query_one(
        conn, "SELECT seen FROM prior_views WHERE movie_id = ? AND member_id = ?",
        (movie_id, member_id))
    return bool(live["seen"]) if live else False


def upsert_rating(conn: sqlite3.Connection, movie_id: int, member_id: int,
                  score: float, seen_before: bool, note: str | None) -> None:
    db.execute(
        conn,
        """INSERT INTO ratings (movie_id, member_id, score, seen_before, note, created_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(movie_id, member_id)
           DO UPDATE SET score = excluded.score,
                         seen_before = excluded.seen_before,
                         note = excluded.note""",
        (movie_id, member_id, score, 1 if seen_before else 0, note),
    )


def sync_rating_from_plex(conn: sqlite3.Connection, movie_id: int,
                          member_id: int, score: float) -> bool:
    """Apply a Plex score while preserving Film Club-specific rating context.

    Existing notes and seen-before answers are untouched. A new rating uses the
    same frozen/default seen-before behavior as the UI. Returns whether data
    changed, which also suppresses webhook echoes from outbound sync.
    """
    existing = db.query_one(
        conn, "SELECT score FROM ratings WHERE movie_id = ? AND member_id = ?",
        (movie_id, member_id),
    )
    if existing and float(existing["score"]) == float(score):
        return False
    if existing:
        db.execute(
            conn,
            "UPDATE ratings SET score = ? WHERE movie_id = ? AND member_id = ?",
            (score, movie_id, member_id),
        )
    else:
        db.execute(
            conn,
            """INSERT INTO ratings (movie_id, member_id, score, seen_before)
               VALUES (?, ?, ?, ?)""",
            (movie_id, member_id, score,
             1 if default_seen_before(conn, movie_id, member_id) else 0),
        )
    return True


# --- admin -----------------------------------------------------------------

def admin_members(conn: sqlite3.Connection) -> list[dict]:
    """Members enriched for the admin panel: activity counts and owner/admin
    flags."""
    rows = db.query_all(conn, "SELECT * FROM members ORDER BY username COLLATE NOCASE")
    members = [dict(r) for r in rows]

    def count(sql, params):
        return db.query_one(conn, sql, params)["c"]

    out = []
    for m in members:
        owner = bool(m.get("is_owner")) or m["plex_id"] in config.ADMIN_PLEX_IDS
        suggested = count("SELECT COUNT(*) c FROM movies WHERE suggested_by = ?", (m["id"],))
        watched_sug = count(
            "SELECT COUNT(*) c FROM movies WHERE suggested_by = ? AND status='watched'", (m["id"],))
        ratings = count("SELECT COUNT(*) c FROM ratings WHERE member_id = ?", (m["id"],))
        providers = [
            r["provider"] for r in db.query_all(
                conn,
                "SELECT provider FROM identities WHERE member_id = ? ORDER BY provider",
                (m["id"],),
            )
        ]
        display = (m.get("display_name") or "").strip() or None
        out.append({
            "id": m["id"],
            # Same "effective name" convention as db.member_public(): the
            # chosen display name if set, otherwise the raw Plex username.
            "username": display or m["username"],
            "plex_username": m["username"],
            "email": m.get("email"),
            "thumb": m.get("thumb"),
            "discord_user_id": m.get("discord_user_id"),
            "color": m["color"],
            "plex_id": m["plex_id"],
            "is_admin": bool(m.get("is_admin")) or owner,
            "is_owner": owner,
            "can_curate_collections": bool(m.get("can_curate_collections")),
            "identity_providers": providers,
            "created_at": m.get("created_at"),
            "counts": {"suggested": suggested, "suggested_watched": watched_sug, "ratings": ratings},
        })
    return out


def set_member_admin(conn: sqlite3.Connection, member_id: int, value: bool) -> None:
    """Grant/revoke the admin flag. Owner accounts (env allowlist) stay admin
    regardless, since effective-admin ORs in the allowlist."""
    row = db.query_one(conn, "SELECT plex_id, is_owner FROM members WHERE id = ?", (member_id,))
    if not row:
        raise ValueError("Member not found")
    if (row["is_owner"] or row["plex_id"] in config.ADMIN_PLEX_IDS) and not value:
        raise ValueError("Can't remove admin from the owner account")
    db.execute(conn, "UPDATE members SET is_admin = ? WHERE id = ?",
               (1 if value else 0, member_id))


def set_member_curator(conn: sqlite3.Connection, member_id: int, value: bool) -> None:
    """Grant/revoke collection-curation rights. Independent of is_admin, and
    carries no risk symmetrical to demoting an admin — it grants nothing
    outside collections and only over collections that member created — so
    unlike set_member_admin there is no owner-protection case to guard."""
    if not db.query_one(conn, "SELECT id FROM members WHERE id = ?", (member_id,)):
        raise ValueError("Member not found")
    db.execute(conn, "UPDATE members SET can_curate_collections = ? WHERE id = ?",
               (1 if value else 0, member_id))


def delete_movie(conn: sqlite3.Connection, movie_id: int) -> bool:
    """Delete a movie and its ratings/prior_views (FK cascade)."""
    if not db.query_one(conn, "SELECT id FROM movies WHERE id = ?", (movie_id,)):
        return False
    db.execute(conn, "DELETE FROM movies WHERE id = ?", (movie_id,))
    return True
