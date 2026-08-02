"""TMDB client.

TMDB is the source of truth for film metadata. We search on demand
(search-as-you-type) but *snapshot* the full metadata into our DB on selection
so page loads never re-hit TMDB.
"""
import asyncio

import httpx

from . import config

BASE = "https://api.themoviedb.org/3"


def _params(extra: dict | None = None) -> dict:
    p = {"api_key": config.TMDB_API_KEY}
    if extra:
        p.update(extra)
    return p


def _poster(path: str | None, size: str = "w500") -> str | None:
    return f"{config.TMDB_IMAGE_BASE}/{size}{path}" if path else None


def _backdrop(path: str | None, size: str = "w1280") -> str | None:
    return f"{config.TMDB_IMAGE_BASE}/{size}{path}" if path else None


def language_name(movie: dict) -> str | None:
    """Return TMDB's English name for the movie's original language."""
    code = movie.get("original_language")
    if not code:
        return None
    for language in movie.get("spoken_languages", []) or []:
        if language.get("iso_639_1") == code:
            return language.get("english_name") or language.get("name") or code.upper()
    return str(code).upper()


def us_content_rating(movie: dict) -> str | None:
    """Return the preferred US movie certification from TMDB release data."""
    results = movie.get("release_dates", {}).get("results", []) or []
    us = next((result for result in results if result.get("iso_3166_1") == "US"), None)
    if not us:
        return None
    # Prefer a wide theatrical certification, then limited theatrical and
    # home-release records. TMDB may include several US dates with the same
    # certification, so the release date provides a stable tie-breaker.
    type_order = {3: 0, 2: 1, 4: 2, 5: 3, 6: 4, 1: 5}
    rated = [release for release in us.get("release_dates", [])
             if str(release.get("certification") or "").strip()]
    if not rated:
        return None
    rated.sort(key=lambda release: (
        type_order.get(release.get("type"), 99),
        str(release.get("release_date") or ""),
    ))
    return str(rated[0]["certification"]).strip()


async def _director(client: httpx.AsyncClient, tmdb_id: int) -> str | None:
    """Look up a film's director. Degrades to None on any failure."""
    try:
        r = await client.get(f"{BASE}/movie/{tmdb_id}/credits", params=_params())
        r.raise_for_status()
        for crew in r.json().get("crew", []):
            if crew.get("job") == "Director":
                return crew.get("name")
    except Exception:  # noqa: BLE001 — a missing director must not break search
        return None
    return None


async def search(query: str, limit: int = 6) -> list[dict]:
    """Search-as-you-type. Results carry poster, title, year, and director.

    The search endpoint doesn't return crew, so we fetch directors in parallel
    for the (capped, debounced) result set. Failures degrade to no director
    rather than erroring the whole search.
    """
    query = query.strip()
    if not query or not config.TMDB_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{BASE}/search/movie",
            params=_params({"query": query, "include_adult": "false"}),
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[:limit]

        directors = await asyncio.gather(*(_director(client, r["id"]) for r in results))

        out = []
        for r, director in zip(results, directors):
            year = (r.get("release_date") or "")[:4]
            out.append(
                {
                    "tmdb_id": r["id"],
                    "title": r.get("title") or r.get("original_title") or "Untitled",
                    "year": int(year) if year.isdigit() else None,
                    "poster_url": _poster(r.get("poster_path")),
                    "overview": r.get("overview"),
                    "director": director,
                    "language": language_name(r),
                }
            )
    return out


def _profile(path: str | None, size: str = "w300") -> str | None:
    return f"{config.TMDB_IMAGE_BASE}/{size}{path}" if path else None


async def find_director(name: str) -> dict | None:
    """Resolve a director's name to a TMDB person.

    Prefers someone TMDB classifies in the directing department; a plain name
    search otherwise returns actors first for anyone who has both careers.
    """
    name = (name or "").strip()
    if not name or not config.TMDB_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(f"{BASE}/search/person", params=_params({"query": name}))
        resp.raise_for_status()
        results = resp.json().get("results", [])
    if not results:
        return None
    directing = [r for r in results if r.get("known_for_department") == "Directing"]
    chosen = (directing or results)[0]
    return {
        "tmdb_id": chosen["id"],
        "name": chosen.get("name") or name,
        "portrait_url": _profile(chosen.get("profile_path")),
    }


async def person(person_id: int) -> dict:
    """Biographical scaffolding for a director: portrait and dates."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(f"{BASE}/person/{person_id}", params=_params())
        resp.raise_for_status()
        d = resp.json()
    return {
        "tmdb_id": d["id"],
        "name": d.get("name"),
        "portrait_url": _profile(d.get("profile_path")),
        "born": d.get("birthday"),
        "died": d.get("deathday"),
        "place_of_birth": d.get("place_of_birth"),
    }


async def directed_films(person_id: int) -> list[dict]:
    """Every film TMDB credits this person with directing, newest first.

    Deduplicated by film: TMDB sometimes lists a person twice on one title
    (say, as both director and writer-director on differing credit rows).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE}/person/{person_id}/movie_credits", params=_params())
        resp.raise_for_status()
        crew = resp.json().get("crew", [])

    seen: set[int] = set()
    films = []
    for entry in crew:
        if entry.get("job") != "Director" or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        year = (entry.get("release_date") or "")[:4]
        films.append({
            "tmdb_id": entry["id"],
            "title": entry.get("title") or entry.get("original_title") or "Untitled",
            "year": int(year) if year.isdigit() else None,
            "poster_url": _poster(entry.get("poster_path"), "w185"),
        })
    # Undated entries are usually unreleased; they belong at the end, not the top.
    films.sort(key=lambda f: (f["year"] is None, -(f["year"] or 0), f["title"]))
    return films


async def details(tmdb_id: int) -> dict:
    """Full metadata snapshot, including credits and US content rating."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{BASE}/movie/{tmdb_id}",
            params=_params({"append_to_response": "credits,release_dates"}),
        )
        resp.raise_for_status()
        d = resp.json()

    director = None
    for crew in d.get("credits", {}).get("crew", []):
        if crew.get("job") == "Director":
            director = crew.get("name")
            break

    year = (d.get("release_date") or "")[:4]
    return {
        "tmdb_id": d["id"],
        "title": d.get("title") or d.get("original_title") or "Untitled",
        "year": int(year) if year.isdigit() else None,
        "poster_url": _poster(d.get("poster_path")),
        "backdrop_url": _backdrop(d.get("backdrop_path")),
        "runtime": d.get("runtime"),
        "director": director,
        "language": language_name(d),
        "content_rating": us_content_rating(d),
        "overview": d.get("overview"),
        "genres": [g["name"] for g in d.get("genres", [])],
        "imdb_id": d.get("imdb_id"),
    }
