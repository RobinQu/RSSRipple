"""Genre schema types — the closed TMDB genre set as a Pydantic Literal.

Single source of truth is ``app.services.genre_registry.GENRE_NAMES``; this
Literal is a hand-synced copy so OpenAPI (/docs) renders the full enum on
work Create/Update/Response schemas and the notification payload. Works API
rejects any value outside this set with 422.
"""

from typing import Literal

# Keep in sync with app/services/genre_registry.py GENRE_NAMES (TMDB 27).
GenreName = Literal[
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Music",
    "Mystery",
    "Romance",
    "Science Fiction",
    "TV Movie",
    "Thriller",
    "War",
    "Western",
    "Action & Adventure",
    "Kids",
    "News",
    "Reality",
    "Sci-Fi & Fantasy",
    "Soap",
    "Talk",
    "War & Politics",
]
