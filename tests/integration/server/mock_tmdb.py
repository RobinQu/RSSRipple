"""Synthetic TMDB protocol fixtures; IDs are test data, not recorded identities."""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/tmdb/3")
WORKS = {
    900001: {"media_type": "tv", "name": "黄泉使者", "original_name": "Daemons of the Shadow Realm"},
    900002: {"media_type": "tv", "name": "葬送的芙莉莲", "original_name": "Frieren: Beyond Journey's End"},
    900003: {
        "media_type": "movie",
        "title": "黄泉使者 剧场版",
        "original_title": "Daemons of the Shadow Realm: The Movie",
    },
}


def _work(identity: int) -> dict:
    return {
        "id": identity,
        **WORKS[identity],
        "genre_ids": [16, 28],
        "original_language": "ja",
        "origin_country": ["JP"],
        "vote_average": 8.4,
        "number_of_seasons": 1,
        "seasons": [{"season_number": 1, "episode_count": 12}],
        "belongs_to_collection": None,
    }


@router.get("/search/multi")
def search(query: str):
    # Only the mock model's explicit source query gets these candidates.
    # Ordinary local/source matching must still exercise its not-found path.
    return {"results": [_work(i) for i in WORKS] if query == "mock search query" else []}


@router.get("/configuration")
def configuration():
    return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}}


@router.get("/genre/{media_type}/list")
def genres(media_type: str):
    return {"genres": [{"id": 16, "name": "Animation"}, {"id": 28, "name": "Action"}]}


@router.get("/{media_type}/{identity}")
def details(media_type: str, identity: int):
    if identity not in WORKS or WORKS[identity]["media_type"] != media_type:
        raise HTTPException(404)
    return _work(identity)


@router.get("/tv/{identity}/season/{season}")
def season_details(identity: int, season: int):
    if identity not in WORKS or WORKS[identity]["media_type"] != "tv" or season != 1:
        raise HTTPException(404)
    return {"episodes": [{"episode_number": n, "season_number": 1, "name": f"Episode {n}"} for n in range(1, 13)]}
