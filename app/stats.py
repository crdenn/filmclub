"""Server-side statistics.

Everything the Stats view shows is computed here, once, from the DB.

Small-n handling is mandatory and pervasive:
  * MIN_FILMS_PER_MEMBER — a per-member statistic derived from fewer rated
    films than this is flagged low-confidence (never silently rendered).
  * MIN_RATERS_PER_FILM — a per-film statistic (divisiveness, splits) needs at
    least this many raters or it is flagged / suppressed.
  * MIN_OVERLAP — a pairwise correlation needs at least this many co-rated
    films or the cell is suppressed.

The charts will look plausible long before they are meaningful; these guards
are the point, not decoration.
"""
import math
import sqlite3
from collections import defaultdict

from . import db

MIN_FILMS_PER_MEMBER = 5
MIN_RATERS_PER_FILM = 3
MIN_OVERLAP = 5


# --- small numeric helpers -------------------------------------------------

def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pstdev(xs: list[float]) -> float | None:
    """Population standard deviation. None if fewer than 2 points."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None if undefined (n<2 or zero variance)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / (sx * sy)


def _round(v: float | None, ndigits: int = 2) -> float | None:
    return round(v, ndigits) if v is not None else None


# --- data loading ----------------------------------------------------------

def _load(conn: sqlite3.Connection):
    members = [db.member_public(r) for r in db.query_all(
        conn, "SELECT * FROM members ORDER BY username COLLATE NOCASE")]
    movies = [db.movie_base(r) for r in db.query_all(
        conn, "SELECT * FROM movies WHERE status = 'watched'")]
    # Scheduled-film scores are private until archive. Filtering here prevents
    # aggregate statistics from leaking them indirectly before reveal.
    ratings = [dict(r) for r in db.query_all(
        conn,
        """SELECT r.* FROM ratings r JOIN movies m ON m.id = r.movie_id
           WHERE m.status = 'watched'""",
    )]
    all_movies = [db.movie_base(r) for r in db.query_all(conn, "SELECT * FROM movies")]
    return members, movies, ratings, all_movies


# --- individual stats ------------------------------------------------------

def _suggestions_per_member(members, all_movies) -> list[dict]:
    suggested = defaultdict(int)
    watched = defaultdict(int)
    for mv in all_movies:
        sid = mv.get("suggested_by")
        if sid is None:
            continue
        suggested[sid] += 1
        if mv["status"] == "watched":
            watched[sid] += 1
    out = []
    for m in members:
        s = suggested[m["id"]]
        w = watched[m["id"]]
        out.append({
            "member": m,
            "suggested": s,
            "watched": w,
            "watch_rate": round(w / s, 2) if s else None,
        })
    return out


def _rater_profiles(members, ratings) -> list[dict]:
    by_member: dict[int, list[float]] = defaultdict(list)
    for r in ratings:
        by_member[r["member_id"]].append(r["score"])

    all_scores = [r["score"] for r in ratings]
    group_mean = _mean(all_scores)

    out = []
    for m in members:
        scores = by_member[m["id"]]
        mean = _mean(scores)
        out.append({
            "member": m,
            "n": len(scores),
            "mean": _round(mean),
            "stdev": _round(_pstdev(scores)),
            "delta_from_group": _round(mean - group_mean) if (mean is not None and group_mean is not None) else None,
            "low_confidence": len(scores) < MIN_FILMS_PER_MEMBER,
        })
    return out, _round(group_mean)


def _agreement_matrix(members, ratings) -> dict:
    # member_id -> {movie_id: score}
    scores: dict[int, dict[int, float]] = defaultdict(dict)
    for r in ratings:
        scores[r["member_id"]][r["movie_id"]] = r["score"]

    ids = [m["id"] for m in members]
    cells = {}
    for a in ids:
        for b in ids:
            if a == b:
                continue
            shared = set(scores[a]) & set(scores[b])
            xs = [scores[a][mv] for mv in shared]
            ys = [scores[b][mv] for mv in shared]
            r = _pearson(xs, ys)
            cells[f"{a}:{b}"] = {
                "r": _round(r),
                "overlap": len(shared),
                "suppressed": len(shared) < MIN_OVERLAP or r is None,
            }
    return {
        "member_ids": ids,
        "cells": cells,
        "min_overlap": MIN_OVERLAP,
    }


def _divisiveness(watched, ratings) -> list[dict]:
    by_movie: dict[int, list[dict]] = defaultdict(list)
    for r in ratings:
        by_movie[r["movie_id"]].append(r)

    movies_by_id = {mv["id"]: mv for mv in watched}
    out = []
    for mid, rs in by_movie.items():
        mv = movies_by_id.get(mid)
        if not mv:
            continue
        scores = [r["score"] for r in rs]
        first = [r["score"] for r in rs if not r["seen_before"]]
        rewatch = [r["score"] for r in rs if r["seen_before"]]
        out.append({
            "movie": {"id": mv["id"], "title": mv["title"], "year": mv["year"],
                      "poster_url": mv["poster_url"]},
            "n": len(scores),
            "stdev": _round(_pstdev(scores)),
            "first_watch_stdev": _round(_pstdev(first)),
            "rewatch_stdev": _round(_pstdev(rewatch)),
            "first_watch_n": len(first),
            "rewatch_n": len(rewatch),
            # A high overall SD explained entirely by the first/rewatch split is
            # distinguishable from a genuine within-group disagreement.
            "split_explains": _split_explains(scores, first, rewatch),
            "low_confidence": len(scores) < MIN_RATERS_PER_FILM,
        })
    out.sort(key=lambda x: (x["stdev"] is None, -(x["stdev"] or 0)))
    return out


def _split_explains(scores, first, rewatch) -> bool | None:
    """Heuristic: is the overall spread mostly a first-vs-rewatch group gap?

    True when both groups are internally tight but their means differ. None when
    we can't tell (a group missing / too small).
    """
    if len(first) < 2 or len(rewatch) < 2:
        return None
    overall = _pstdev(scores)
    if overall is None or overall == 0:
        return None
    within = ((_pstdev(first) or 0.0) + (_pstdev(rewatch) or 0.0)) / 2
    gap = abs((_mean(first) or 0.0) - (_mean(rewatch) or 0.0))
    return gap > overall and within < overall


def _first_vs_rewatch_delta(watched, ratings) -> list[dict]:
    """Per film: mean(seen before) - mean(first watch). Requires both sides."""
    by_movie: dict[int, list[dict]] = defaultdict(list)
    for r in ratings:
        by_movie[r["movie_id"]].append(r)
    movies_by_id = {mv["id"]: mv for mv in watched}

    out = []
    for mid, rs in by_movie.items():
        mv = movies_by_id.get(mid)
        if not mv:
            continue
        first = [r["score"] for r in rs if not r["seen_before"]]
        rewatch = [r["score"] for r in rs if r["seen_before"]]
        if not first or not rewatch:
            continue  # suppress: needs at least one on each side
        delta = _mean(rewatch) - _mean(first)
        out.append({
            "movie": {"id": mv["id"], "title": mv["title"], "year": mv["year"],
                      "poster_url": mv["poster_url"]},
            "rewatch_mean": _round(_mean(rewatch)),
            "first_watch_mean": _round(_mean(first)),
            "delta": _round(delta),
            "rewatch_n": len(rewatch),
            "first_watch_n": len(first),
        })
    out.sort(key=lambda x: -x["delta"])
    return out


def _member_rewatch_bias(members, ratings) -> list[dict]:
    first: dict[int, list[float]] = defaultdict(list)
    rewatch: dict[int, list[float]] = defaultdict(list)
    for r in ratings:
        (rewatch if r["seen_before"] else first)[r["member_id"]].append(r["score"])

    out = []
    for m in members:
        f, rw = first[m["id"]], rewatch[m["id"]]
        both = bool(f) and bool(rw)
        delta = (_mean(rw) - _mean(f)) if both else None
        out.append({
            "member": m,
            "first_watch_mean": _round(_mean(f)),
            "rewatch_mean": _round(_mean(rw)),
            "delta": _round(delta),
            "first_watch_n": len(f),
            "rewatch_n": len(rw),
            # need a floor on each side to mean anything
            "low_confidence": (len(f) < 3 or len(rw) < 3) if both else True,
            "available": both,
        })
    return out


def _suggester_scorecard(members, all_movies, ratings) -> list[dict]:
    avg_by_movie: dict[int, float] = {}
    tmp: dict[int, list[float]] = defaultdict(list)
    for r in ratings:
        tmp[r["movie_id"]].append(r["score"])
    for mid, xs in tmp.items():
        avg_by_movie[mid] = _mean(xs)

    watched_by_suggester: dict[int, list[float]] = defaultdict(list)
    for mv in all_movies:
        if mv["status"] != "watched":
            continue
        sid = mv.get("suggested_by")
        if sid is None or mv["id"] not in avg_by_movie:
            continue
        watched_by_suggester[sid].append(avg_by_movie[mv["id"]])

    out = []
    for m in members:
        xs = watched_by_suggester[m["id"]]
        out.append({
            "member": m,
            "n_films": len(xs),
            "avg_rating": _round(_mean(xs)),
            "low_confidence": len(xs) < MIN_FILMS_PER_MEMBER,
        })
    out.sort(key=lambda x: (x["avg_rating"] is None, -(x["avg_rating"] or 0)))
    return out


def _genre_decade_runtime(watched) -> dict:
    genres: dict[str, int] = defaultdict(int)
    decades: dict[str, int] = defaultdict(int)
    total_runtime = 0
    for mv in watched:
        for g in mv.get("genres", []):
            genres[g] += 1
        yr = mv.get("year")
        if yr:
            decades[f"{(yr // 10) * 10}s"] += 1
        total_runtime += mv.get("runtime") or 0
    return {
        "genres": sorted(({"genre": k, "count": v} for k, v in genres.items()),
                         key=lambda x: -x["count"]),
        "decades": sorted(({"decade": k, "count": v} for k, v in decades.items()),
                          key=lambda x: x["decade"]),
        "total_runtime_minutes": total_runtime,
    }


def compute(conn: sqlite3.Connection) -> dict:
    members, watched, ratings, all_movies = _load(conn)
    rater_profiles, group_mean = _rater_profiles(members, ratings)
    gdr = _genre_decade_runtime(watched)

    return {
        "thresholds": {
            "min_films_per_member": MIN_FILMS_PER_MEMBER,
            "min_raters_per_film": MIN_RATERS_PER_FILM,
            "min_overlap": MIN_OVERLAP,
        },
        "totals": {
            "watched": len(watched),
            "suggested_open": sum(1 for m in all_movies if m["status"] == "suggested"),
            "ratings": len(ratings),
            "members": len(members),
            "group_mean": group_mean,
            "total_runtime_minutes": gdr["total_runtime_minutes"],
        },
        "members": members,
        "suggestions_per_member": _suggestions_per_member(members, all_movies),
        "rater_profiles": rater_profiles,
        "agreement_matrix": _agreement_matrix(members, ratings),
        "divisiveness": _divisiveness(watched, ratings),
        "first_vs_rewatch_delta": _first_vs_rewatch_delta(watched, ratings),
        "member_rewatch_bias": _member_rewatch_bias(members, ratings),
        "suggester_scorecard": _suggester_scorecard(members, all_movies, ratings),
        "genres": gdr["genres"],
        "decades": gdr["decades"],
    }
