"""RSS feed generation server.

Generates mock RSS feeds in 3 formats:
- dmhy.org style (Chinese anime, magnet links in enclosure)
- mikanani.me style (anime, .torrent files in enclosure + torrent namespace)
- myrss.org/eztv style (Western TV, both magnet and .torrent)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from .test_data import (
    AnimeRelease,
    MovieRelease,
    TVShowRelease,
    generate_anime_releases,
    generate_movie_releases,
    generate_tv_releases,
)


def _rfc822(dt: datetime) -> str:
    """Format datetime as RFC 822."""
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def _iso8601(dt: datetime) -> str:
    """Format datetime as ISO 8601."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _fake_info_hash(name: str) -> str:
    """Generate a deterministic fake info hash from a name."""
    return hashlib.sha1(name.encode()).hexdigest().upper()


def _fake_magnet(name: str, tracker_url: str) -> str:
    """Generate a magnet URI for a file."""
    info_hash = _fake_info_hash(name)
    from urllib.parse import quote
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}&tr={quote(tracker_url)}"


def generate_dmhy_feed(
    releases: list[AnimeRelease] | None = None,
    server_url: str = "http://test-server:8080",
    tracker_url: str = "http://test-server:8080/announce",
    series_index: int = 0,
) -> str:
    """Generate a dmhy.org-style RSS feed.

    Characteristics:
    - Magnet links in enclosure URL
    - enclosure length="1" (fake)
    - CDATA-wrapped titles and descriptions
    - author field
    - category field
    """
    if releases is None:
        releases = generate_anime_releases(series_index=series_index, episode_count=3)

    now = datetime.now(timezone.utc)

    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:wfw="http://wellformedweb.org/CommentAPI/">',
        "<channel>",
        "<title><![CDATA[季度全集-動漫花園資源網]]></title>",
        f"<link>{server_url}</link>",
        "<description><![CDATA[動漫花園資訊網 - 測試數據]]></description>",
        "<language>zh-cn</language>",
        f"<pubDate>{_rfc822(now)}</pubDate>",
    ]

    for i, release in enumerate(releases):
        title = f"[{release.subtitle_group}] {release.title_cn}/{release.title_en} - {release.episode:02d} [{release.subtitle_type}] {release.source} {release.resolution} {release.video_codec} {release.audio_codec}"
        guid = f"{server_url}/topics/view/{1000 + i}_{hashlib.md5(title.encode()).hexdigest()[:12]}.html"
        pub_date = now - timedelta(hours=len(releases) - i)
        info_hash = _fake_info_hash(release.file.name if release.file else title)
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn=&tr={tracker_url}"

        xml_parts.extend([
            "<item>",
            f"<title><![CDATA[{title}]]></title>",
            f"<link>{guid}</link>",
            f"<pubDate>{_rfc822(pub_date)}</pubDate>",
            f'<description><![CDATA[<p>{title} [775.6MB]</p>]]></description>',
            f'<enclosure url="{magnet}" length="1" type="application/x-bittorrent"></enclosure>',
            f"<author><![CDATA[{release.subtitle_group}]]></author>",
            f'<guid isPermaLink="true">{guid}</guid>',
            '<category domain="http://share.dmhy.org/topics/list/sort_id/31"><![CDATA[季度全集]]></category>',
            "</item>",
        ])

    xml_parts.extend(["</channel>", "</rss>"])
    return "\n".join(xml_parts)


def generate_mikanani_feed(
    releases: list[AnimeRelease] | None = None,
    server_url: str = "http://test-server:8080",
    series_index: int = 1,
) -> str:
    """Generate a mikanani.me-style RSS feed.

    Characteristics:
    - .torrent file URLs in enclosure
    - torrent namespace with contentLength and pubDate
    - guid is the title string (isPermaLink=false)
    - description = title + [filesize]
    """
    if releases is None:
        releases = generate_anime_releases(series_index=series_index, episode_count=3)

    now = datetime.now(timezone.utc)

    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>Mikan Project - 我的番组</title>",
        f"<link>{server_url}/rss/mikanani</link>",
        "<description>Mikan Project - 我的番组</description>",
    ]

    for i, release in enumerate(releases):
        title = f"[{release.subtitle_group}] {release.title_cn} / {release.title_en} - {release.episode:02d} [{release.source} {release.resolution} {release.video_codec} {release.audio_codec}][{release.subtitle_type}]"
        ep_hash = hashlib.sha1(title.encode()).hexdigest()
        torrent_url = f"{server_url}/torrents/{_fake_info_hash(release.file.name if release.file else title)}.torrent"
        file_size = release.file.size if release.file else 813275520
        pub_date = now - timedelta(hours=len(releases) - i)

        xml_parts.extend([
            "<item>",
            f'<guid isPermaLink="false">{title}</guid>',
            f"<link>{server_url}/Home/Episode/{ep_hash}</link>",
            f"<title>{title}</title>",
            f"<description>{title}[{file_size / 1024 / 1024:.1f}MB]</description>",
            f'<torrent xmlns="https://mikanani.me/0.1/">',
            f"<link>{server_url}/Home/Episode/{ep_hash}</link>",
            f"<contentLength>{file_size}</contentLength>",
            f"<pubDate>{_iso8601(pub_date)}</pubDate>",
            "</torrent>",
            f'<enclosure type="application/x-bittorrent" length="{file_size}" url="{torrent_url}" />',
            "</item>",
        ])

    xml_parts.extend(["</channel>", "</rss>"])
    return "\n".join(xml_parts)


def generate_eztv_feed(
    releases: list[TVShowRelease] | None = None,
    server_url: str = "http://test-server:8080",
    tracker_url: str = "http://test-server:8080/announce",
    show_index: int = 0,
) -> str:
    """Generate an EZTV-style RSS feed.

    Characteristics:
    - torrent: namespace with magnetURI, infoHash, contentLength, fileName
    - Both .torrent in enclosure and magnet in torrent:magnetURI
    - Scene-style title: Show SxxExy res source codec-Group
    - No description, no author
    """
    if releases is None:
        releases = generate_tv_releases(show_index=show_index, episode_count=3)

    now = datetime.now(timezone.utc)

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/">',
        "<channel>",
        "<title>TV Torrents RSS feed - EZTVx.to (official website)</title>",
        f"<link>{server_url}/rss/eztv</link>",
        "<description>TV Torrents RSS feed - EZTVx.to (test data)</description>",
        f"<lastBuildDate>{_rfc822(now)}</lastBuildDate>",
    ]

    for i, release in enumerate(releases):
        title = release.title
        info_hash = _fake_info_hash(release.file.name if release.file else title)
        magnet = _fake_magnet(release.file.name if release.file else title, tracker_url)
        file_size = release.file.size if release.file else 1192191817
        file_name = release.file.name if release.file else f"{title}.mkv"
        guid = f"{server_url}/ep/{1000 + i}/{title.lower().replace(' ', '-')}/"
        torrent_url = f"{server_url}/torrents/{info_hash}.torrent"
        pub_date = now - timedelta(hours=len(releases) - i)

        xml_parts.extend([
            "<item>",
            f"<title>{title}</title>",
            "<category>TV</category>",
            f"<link>{guid}</link>",
            f"<guid>{guid}</guid>",
            f"<pubDate>{_rfc822(pub_date)}</pubDate>",
            f"<torrent:contentLength>{file_size}</torrent:contentLength>",
            f"<torrent:infoHash>{info_hash}</torrent:infoHash>",
            f"<torrent:magnetURI><![CDATA[{magnet}]]></torrent:magnetURI>",
            "<torrent:seeds>5</torrent:seeds>",
            "<torrent:peers>10</torrent:peers>",
            "<torrent:verified>1</torrent:verified>",
            f"<torrent:fileName>{file_name}</torrent:fileName>",
            f'<enclosure url="{torrent_url}" length="{file_size}" type="application/x-bittorrent" />',
            "</item>",
        ])

    xml_parts.extend(["</channel>", "</rss>"])
    return "\n".join(xml_parts)


def generate_movie_feed(
    releases: list[MovieRelease] | None = None,
    server_url: str = "http://test-server:8080",
    tracker_url: str = "http://test-server:8080/announce",
) -> str:
    """Generate a movie RSS feed with IMDB-style data (EZTV-like format)."""
    if releases is None:
        releases = []
        for mi in range(3):
            releases.extend(generate_movie_releases(movie_index=mi))

    now = datetime.now(timezone.utc)

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/">',
        "<channel>",
        "<title>Movie Torrents RSS feed</title>",
        f"<link>{server_url}/rss/movies</link>",
        "<description>Movie torrents with IMDB metadata</description>",
        f"<lastBuildDate>{_rfc822(now)}</lastBuildDate>",
    ]

    for i, release in enumerate(releases):
        title = release.title_scene
        info_hash = _fake_info_hash(release.file.name if release.file else title)
        magnet = _fake_magnet(release.file.name if release.file else title, tracker_url)
        file_size = release.file.size if release.file else 2147483648
        file_name = release.file.name if release.file else f"{title}.mkv"
        guid = f"{server_url}/movie/{release.imdb_id}/{title.lower().replace(' ', '-')}/"
        torrent_url = f"{server_url}/torrents/{info_hash}.torrent"
        pub_date = now - timedelta(hours=len(releases) - i)

        xml_parts.extend([
            "<item>",
            f"<title>{title}</title>",
            "<category>Movies</category>",
            f"<link>{guid}</link>",
            f"<guid>{guid}</guid>",
            f"<pubDate>{_rfc822(pub_date)}</pubDate>",
            f"<torrent:contentLength>{file_size}</torrent:contentLength>",
            f"<torrent:infoHash>{info_hash}</torrent:infoHash>",
            f"<torrent:magnetURI><![CDATA[{magnet}]]></torrent:magnetURI>",
            f"<torrent:fileName>{file_name}</torrent:fileName>",
            f'<enclosure url="{torrent_url}" length="{file_size}" type="application/x-bittorrent" />',
            "</item>",
        ])

    xml_parts.extend(["</channel>", "</rss>"])
    return "\n".join(xml_parts)
