"""Mock test data generation for integration tests.

Generates diverse test data simulating:
- Anime series with multiple subtitle groups (dmhy, mikanani style)
- Western TV shows (eztv/scene style)
- Movies with IMDB-like metadata
- Small test files for torrent creation
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class TestFile:
    """A mock file that can be served as torrent content."""
    name: str
    size: int
    content: bytes
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            self.checksum = hashlib.sha1(self.content).hexdigest()


@dataclass
class AnimeRelease:
    """Mock anime release (dmhy/mikanani style)."""
    subtitle_group: str
    title_cn: str
    title_en: str
    episode: int
    resolution: str = "1080p"
    source: str = "WebRip"
    video_codec: str = "HEVC-10bit"
    audio_codec: str = "AAC"
    subtitle_type: str = "简繁内封字幕"
    container: str = "MKV"
    file: TestFile | None = None


@dataclass
class TVShowRelease:
    """Mock Western TV show release (eztv/scene style)."""
    show_name: str
    season: int
    episode: int
    resolution: str = "720p"
    source: str = "WEB"
    codec: str = "H264"
    release_group: str = "JFF"
    file: TestFile | None = None

    @property
    def title(self) -> str:
        return f"{self.show_name} S{self.season:02d}E{self.episode:02d} {self.resolution} {self.source} {self.codec}-{self.release_group}"


@dataclass
class MovieRelease:
    """Mock movie release with IMDB-like metadata."""
    imdb_id: str
    title: str
    original_title: str
    year: int
    genre: list[str]
    plot: str
    release_group: str = "YTS"
    resolution: str = "1080p"
    file: TestFile | None = None

    @property
    def title_scene(self) -> str:
        return f"{self.title.replace(' ', '.')} {self.year} {self.resolution} BluRay x264-{self.release_group}"


def _make_test_file(name: str, size: int = 4096) -> TestFile:
    """Create a small test file with deterministic content."""
    # Generate deterministic content based on name
    seed = hashlib.sha256(name.encode()).digest()
    content = (seed * ((size // len(seed)) + 1))[:size]
    return TestFile(name=name, size=size, content=content)


# ─── Anime Series Data ───────────────────────────────────────────────

ANIME_SERIES = [
    {
        "title_cn": "黄泉使者",
        "title_en": "Daemons of the Shadow Realm",
        "episodes": 12,
    },
    {
        "title_cn": "葬送的芙莉莲",
        "title_en": "Frieren: Beyond Journey's End",
        "episodes": 28,
    },
    {
        "title_cn": "药屋少女的呢喃",
        "title_en": "The Apothecary Diaries",
        "episodes": 24,
    },
    {
        "title_cn": "咒术回战",
        "title_en": "Jujutsu Kaisen",
        "episodes": 23,
    },
    {
        "title_cn": "小书痴的下克上",
        "title_en": "Honzuki no Gekokujou S04",
        "episodes": 12,
    },
]

SUBTITLE_GROUPS = ["LoliHouse", "ANi", "Skymoon-Raws", "7³ACG", "云光字幕组", "NEST", "KitaujiSub", "VCB-Studio"]


def generate_anime_releases(
    series_index: int = 0,
    episode_start: int = 1,
    episode_count: int = 3,
    groups: list[str] | None = None,
) -> list[AnimeRelease]:
    """Generate multiple anime releases for a series."""
    series = ANIME_SERIES[series_index]
    groups = groups or SUBTITLE_GROUPS[:3]
    releases = []

    for ep in range(episode_start, episode_start + episode_count):
        for group in groups:
            # Vary quality by group
            if group == "LoliHouse":
                res, codec, sub = "1080p", "HEVC-10bit", "简繁内封字幕"
            elif group == "ANi":
                res, codec, sub = "1080p", "AVC", "CHT"
            elif group == "7²ACG":
                res, codec, sub = "1080p", "AV1", "简繁字幕"
            else:
                res, codec, sub = "720p", "HEVC", "简体"

            fname = f"[{group}] {series['title_cn']} - {ep:02d} [{res} {codec} AAC][{sub}].mkv"
            releases.append(AnimeRelease(
                subtitle_group=group,
                title_cn=series["title_cn"],
                title_en=series["title_en"],
                episode=ep,
                resolution=res,
                video_codec=codec,
                subtitle_type=sub,
                file=_make_test_file(fname, 8192),
            ))

    return releases


# ─── Western TV Show Data ────────────────────────────────────────────

TV_SHOWS = [
    {"name": "The Last of Us", "seasons": 2, "episodes_per_season": 9},
    {"name": "House of the Dragon", "seasons": 2, "episodes_per_season": 10},
    {"name": "Severance", "seasons": 2, "episodes_per_season": 9},
    {"name": "Shogun", "seasons": 1, "episodes_per_season": 10},
    {"name": "Ace Of The Diamond", "seasons": 4, "episodes_per_season": 13},
]

SCENE_GROUPS = ["JFF", "ETHEL", "MeGusta", "FLUX", "NTb", "ION10", "BATV"]

MOVIE_GROUPS = ["YTS", "RARBG", "MeGusta", "JFF", "FLUX"]


def generate_tv_releases(
    show_index: int = 0,
    season: int = 1,
    episode_start: int = 1,
    episode_count: int = 3,
    groups: list[str] | None = None,
) -> list[TVShowRelease]:
    """Generate TV show releases in scene naming format."""
    show = TV_SHOWS[show_index]
    groups = groups or SCENE_GROUPS[:3]
    releases = []

    for ep in range(episode_start, episode_start + episode_count):
        for group in groups:
            if group in ("FLUX", "NTb"):
                res, codec = "1080p", "H264"
            else:
                res, codec = "720p", "H264"

            fname = f"{show['name']} S{season:02d}E{ep:02d} {res} WEB {codec}-{group}.mkv"
            releases.append(TVShowRelease(
                show_name=show["name"],
                season=season,
                episode=ep,
                resolution=res,
                codec=codec,
                release_group=group,
                file=_make_test_file(fname, 6144),
            ))

    return releases


# ─── Movie Data (IMDB-like) ──────────────────────────────────────────

MOVIES = [
    {
        "imdb_id": "tt1375666",
        "title": "Inception",
        "original_title": "Inception",
        "year": 2010,
        "genre": ["Action", "Sci-Fi", "Thriller"],
        "plot": "A thief who steals corporate secrets through dream-sharing technology.",
    },
    {
        "imdb_id": "tt0468569",
        "title": "The Dark Knight",
        "original_title": "The Dark Knight",
        "year": 2008,
        "genre": ["Action", "Crime", "Drama"],
        "plot": "Batman faces the Joker, a criminal mastermind who plunges Gotham into anarchy.",
    },
    {
        "imdb_id": "tt0111161",
        "title": "The Shawshank Redemption",
        "original_title": "The Shawshank Redemption",
        "year": 1994,
        "genre": ["Drama"],
        "plot": "Two imprisoned men bond over years, finding solace and eventual redemption.",
    },
]


def generate_movie_releases(
    movie_index: int = 0,
    groups: list[str] | None = None,
) -> list[MovieRelease]:
    """Generate movie releases with IMDB metadata."""
    movie = MOVIES[movie_index]
    groups = groups or ["YTS", "RARBG"]
    releases = []

    for group in groups:
        fname = f"{movie['title'].replace(' ', '.')} {movie['year']} 1080p BluRay x264-{group}.mkv"
        releases.append(MovieRelease(
            imdb_id=movie["imdb_id"],
            title=movie["title"],
            original_title=movie["original_title"],
            year=movie["year"],
            genre=movie["genre"],
            plot=movie["plot"],
            release_group=group,
            file=_make_test_file(fname, 10240),
        ))

    return releases


def generate_all_test_files() -> dict[str, TestFile]:
    """Generate all test files from all release types.

    Returns:
        Dict mapping file name to TestFile.
    """
    files: dict[str, TestFile] = {}

    # Anime: first 2 series, 3 episodes each, 3 groups (keep torrent creation lean)
    # RSS generators can use all series dynamically; torrents only needed for the subset
    for si in range(2):
        for release in generate_anime_releases(series_index=si, episode_count=3):
            if release.file:
                files[release.file.name] = release.file

    # TV: first 2 shows, 3 episodes each, 3 groups
    for si in range(2):
        for release in generate_tv_releases(show_index=si, episode_count=3):
            if release.file:
                files[release.file.name] = release.file

    # Movies: first 3 movies, 2 groups
    for mi in range(3):
        for release in generate_movie_releases(movie_index=mi):
            if release.file:
                files[release.file.name] = release.file

    return files
