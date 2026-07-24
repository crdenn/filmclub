"""Seed the database with realistically-shaped fake data.

Purpose: let the stats views be verified against data with the same *shape* the
club will produce, before anyone logs in. In particular it deliberately plants
the edge cases that break naive stats code:

  * a backlog film everyone has seen        -> ineligible
  * a backlog film with unknowns (no row)   -> unknown != unseen
  * a watched film everyone had seen before -> first/rewatch delta must suppress
  * a watched film nobody had seen before   -> same, other direction
  * a watched film with only 1-2 ratings    -> small-n suppression must fire

Run:  python -m app.seed          (idempotent-ish: refuses if data exists)
      python -m app.seed --force  (wipes and reseeds)
"""
import json
import random
import sys
from datetime import datetime, timedelta

from . import db
from .colors import available_color

random.seed(1729)

# plex_ids use the "dev:<Name>" form so that running with DEV_BYPASS_USER=<Name>
# logs you in AS one of these seeded members, keeping the club at six people
# instead of provisioning a stray seventh. In real use, Plex supplies real ids.
MEMBERS = [
    ("dev:Alice", "Alice"),
    ("dev:Bob", "Bob"),
    ("dev:Carol", "Carol"),
    ("dev:Dave", "Dave"),
    ("dev:Erin", "Erin"),
    ("dev:Frank", "Frank"),
]

# (tmdb_id, title, year, runtime, director, [genres])
FILMS = [
    (238, "The Godfather", 1972, 175, "Francis Ford Coppola", ["Crime", "Drama"]),
    (680, "Pulp Fiction", 1994, 154, "Quentin Tarantino", ["Crime", "Thriller"]),
    (13, "Forrest Gump", 1994, 142, "Robert Zemeckis", ["Drama", "Romance"]),
    (155, "The Dark Knight", 2008, 152, "Christopher Nolan", ["Action", "Crime", "Drama"]),
    (550, "Fight Club", 1999, 139, "David Fincher", ["Drama"]),
    (27205, "Inception", 2010, 148, "Christopher Nolan", ["Action", "Sci-Fi"]),
    (19404, "Dilwale Dulhania Le Jayenge", 1995, 190, "Aditya Chopra", ["Comedy", "Drama", "Romance"]),
    (389, "12 Angry Men", 1957, 96, "Sidney Lumet", ["Drama"]),
    (496243, "Parasite", 2019, 132, "Bong Joon-ho", ["Comedy", "Thriller", "Drama"]),
    (372058, "Your Name.", 2016, 106, "Makoto Shinkai", ["Animation", "Romance", "Drama"]),
    (129, "Spirited Away", 2001, 125, "Hayao Miyazaki", ["Animation", "Family", "Fantasy"]),
    (497, "The Green Mile", 1999, 189, "Frank Darabont", ["Fantasy", "Drama", "Crime"]),
    (11216, "Cinema Paradiso", 1988, 155, "Giuseppe Tornatore", ["Drama", "Romance"]),
    (637, "Life Is Beautiful", 1997, 116, "Roberto Benigni", ["Comedy", "Drama"]),
    (12477, "Grave of the Fireflies", 1988, 89, "Isao Takahata", ["Animation", "Drama", "War"]),
    (346, "Seven Samurai", 1954, 207, "Akira Kurosawa", ["Action", "Drama"]),
    (335984, "Blade Runner 2049", 2017, 164, "Denis Villeneuve", ["Sci-Fi", "Drama"]),
    (429, "The Good, the Bad and the Ugly", 1966, 161, "Sergio Leone", ["Western"]),
    (1891, "The Empire Strikes Back", 1980, 124, "Irvin Kershner", ["Action", "Adventure", "Sci-Fi"]),
    (274, "The Silence of the Lambs", 1991, 119, "Jonathan Demme", ["Crime", "Drama", "Horror"]),
    (807, "Se7en", 1995, 127, "David Fincher", ["Crime", "Mystery", "Thriller"]),
    (311, "Once Upon a Time in America", 1984, 229, "Sergio Leone", ["Crime", "Drama"]),
]


def _score() -> float:
    return random.choice([2.0, 2.5, 3.0, 3.0, 3.5, 3.5, 4.0, 4.0, 4.5, 5.0])


def wipe(conn):
    for t in ("ratings", "prior_views", "movies", "members"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def seed(force: bool = False):
    db.init_db()
    conn = db.connect()
    try:
        existing = conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"]
        if existing and not force:
            print(f"Refusing to seed: {existing} movies already present. "
                  f"Re-run with --force to wipe and reseed.")
            return
        if force:
            wipe(conn)

        # --- members ---
        member_ids = []
        for plex_id, name in MEMBERS:
            cur = conn.execute(
                "INSERT INTO members (plex_id, username, email, thumb, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (plex_id, name, f"{name.lower()}@example.com", None,
                 available_color(conn, plex_id)),
            )
            member_ids.append(cur.lastrowid)
        conn.commit()
        alice, bob, carol, dave, erin, frank = member_ids

        base_date = datetime(2025, 1, 6)  # a Monday

        def insert_movie(film, status, suggester, weeks_ago, snapshot=None):
            tmdb_id, title, year, runtime, director, genres = film
            suggested_at = (base_date + timedelta(weeks=weeks_ago)).isoformat(sep=" ")
            watched_at = None
            snap_json = None
            if status == "watched":
                watched_at = (base_date + timedelta(weeks=weeks_ago, days=5)).isoformat(sep=" ")
                if snapshot is not None:
                    snap_json = json.dumps({str(mid): bool(v) for mid, v in snapshot.items()})
            cur = conn.execute(
                """INSERT INTO movies
                   (tmdb_id, title, year, poster_url, backdrop_url, runtime, director,
                    language, overview, genres, imdb_id, suggested_by, suggested_at, status,
                    watched_at, seen_before_snapshot)
                   VALUES (?, ?, ?, NULL, NULL, ?, ?, 'English', ?, ?, NULL, ?, ?, ?, ?, ?)""",
                (tmdb_id, title, year, runtime, director,
                 f"A seeded overview for {title}.", json.dumps(genres),
                 suggester, suggested_at, status, watched_at, snap_json),
            )
            conn.commit()
            return cur.lastrowid

        def set_prior(movie_id, member_id, seen):
            conn.execute(
                "INSERT INTO prior_views (movie_id, member_id, seen) VALUES (?, ?, ?)",
                (movie_id, member_id, 1 if seen else 0))
            conn.commit()

        def rate(movie_id, member_id, score, seen_before, note=None):
            conn.execute(
                "INSERT INTO ratings (movie_id, member_id, score, seen_before, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (movie_id, member_id, score, 1 if seen_before else 0, note))
            conn.commit()

        all_members = member_ids
        week = 0

        # ----- WATCHED films, most with full ratings -----------------------
        # 1) Normal watched film, mixed first/rewatch.
        m1 = insert_movie(FILMS[0], "watched", alice, week,
                          snapshot={bob: True, dave: True})  # two had seen before
        week += 1
        for mid in all_members:
            sb = mid in (bob, dave)
            rate(m1, mid, _score(), sb)

        # 2) EDGE: everyone had seen it before -> first/rewatch delta suppresses.
        m2 = insert_movie(FILMS[1], "watched", bob, week,
                          snapshot={mid: True for mid in all_members})
        week += 1
        for mid in all_members:
            rate(m2, mid, _score(), True)

        # 3) EDGE: nobody had seen it before -> delta suppresses (other side).
        m3 = insert_movie(FILMS[2], "watched", carol, week,
                          snapshot={mid: False for mid in all_members})
        week += 1
        for mid in all_members:
            rate(m3, mid, _score(), False)

        # 4) EDGE: only two ratings -> small-n suppression must fire.
        m4 = insert_movie(FILMS[3], "watched", dave, week)
        week += 1
        rate(m4, alice, 4.5, False)
        rate(m4, bob, 2.0, True, "Seen it a dozen times; still holds up.")

        # 5) Divisive film: wide spread, genuine disagreement (all first watch).
        m5 = insert_movie(FILMS[4], "watched", erin, week,
                          snapshot={mid: False for mid in all_members})
        week += 1
        for mid, sc in zip(all_members, [5.0, 1.5, 4.5, 2.0, 5.0, 1.5]):
            rate(m5, mid, sc, False)

        # 6) Divisive by SPLIT: rewatchers love it, newcomers cold.
        m6 = insert_movie(FILMS[5], "watched", frank, week,
                          snapshot={alice: True, bob: True, carol: True,
                                    dave: False, erin: False, frank: False})
        week += 1
        for mid, sc, sb in [(alice, 4.5, True), (bob, 5.0, True), (carol, 4.5, True),
                            (dave, 2.5, False), (erin, 2.0, False), (frank, 3.0, False)]:
            rate(m6, mid, sc, sb)

        # 7-12) A run of ordinary watched films to give members >=5 ratings and
        # populate genre/decade/runtime and correlation overlap.
        ordinary = [7, 8, 9, 10, 11, 12, 13, 14]
        suggesters = [alice, bob, carol, dave, erin, frank, alice, carol]
        for idx, sidx in zip(ordinary, suggesters):
            mv = insert_movie(FILMS[idx], "watched", sidx, week)
            week += 1
            # Random prior-seen snapshot; most raters, occasionally one abstains.
            snap = {}
            for mid in all_members:
                seen_before = random.random() < 0.25
                snap[mid] = seen_before
            conn.execute("UPDATE movies SET seen_before_snapshot = ? WHERE id = ?",
                         (json.dumps({str(k): v for k, v in snap.items()}), mv))
            conn.commit()
            raters = all_members if random.random() < 0.8 else random.sample(all_members, 5)
            for mid in raters:
                rate(mv, mid, _score(), snap[mid])

        # ----- BACKLOG (suggested) films with coverage edge cases ----------
        # A) EDGE: everyone has seen it -> ineligible.
        b1 = insert_movie(FILMS[15], "suggested", alice, week); week += 1
        for mid in all_members:
            set_prior(b1, mid, True)

        # B) EDGE: unknowns present (only some members answered).
        b2 = insert_movie(FILMS[16], "suggested", bob, week); week += 1
        set_prior(b2, alice, True)
        set_prior(b2, bob, False)
        set_prior(b2, carol, False)
        # dave, erin, frank: no row -> unknown

        # C) Clearly eligible: several haven't seen it. Carries an elevator pitch
        # so the detail page's pitch treatment is exercised by the seed data.
        b3 = insert_movie(FILMS[17], "suggested", carol, week); week += 1
        conn.execute(
            "UPDATE movies SET pitch = ? WHERE id = ?",
            ("Trust me on this one — it's the rare crowd-pleaser that's also "
             "genuinely clever, and it's short enough for a school night.", b3))
        conn.commit()
        for mid, seen in [(alice, True), (bob, False), (carol, False),
                          (dave, False), (erin, True), (frank, False)]:
            set_prior(b3, mid, seen)

        # D) All unknown (nobody has answered yet) -> unconfirmed.
        insert_movie(FILMS[18], "suggested", dave, week); week += 1

        # E) One unseen, rest seen -> eligible but only just.
        b5 = insert_movie(FILMS[19], "suggested", erin, week); week += 1
        for mid in all_members:
            set_prior(b5, mid, mid != frank)

        # F) Mixed with a couple unknown.
        b6 = insert_movie(FILMS[20], "suggested", frank, week); week += 1
        set_prior(b6, alice, False)
        set_prior(b6, bob, True)
        set_prior(b6, dave, True)

        counts = {
            "members": conn.execute("SELECT COUNT(*) c FROM members").fetchone()["c"],
            "movies": conn.execute("SELECT COUNT(*) c FROM movies").fetchone()["c"],
            "watched": conn.execute("SELECT COUNT(*) c FROM movies WHERE status='watched'").fetchone()["c"],
            "ratings": conn.execute("SELECT COUNT(*) c FROM ratings").fetchone()["c"],
            "prior_views": conn.execute("SELECT COUNT(*) c FROM prior_views").fetchone()["c"],
        }
        print("Seeded:", counts)
    finally:
        conn.close()


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
