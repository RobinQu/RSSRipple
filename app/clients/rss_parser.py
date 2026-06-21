"""RSS feed parser using feedparser.

Provides both sync and async entry access for downstream services.
The mikanani-specific namespace fields are extracted but the parser
is generic enough to work with any RSS/Atom feed.
"""

import asyncio
from datetime import datetime
from dataclasses import dataclass

import feedparser


@dataclass
class RawRSSItem:
    """Raw RSS item before database insertion."""
    guid: str
    title: str
    description: str | None
    link: str | None
    torrent_url: str | None
    content_length: int | None
    published_at: datetime | None


def _parse_feed_sync(url: str) -> feedparser.FeedParserDict:
    """Synchronous feedparser call (blocking)."""
    return feedparser.parse(url)


async def parse_rss_feed(url: str) -> list[RawRSSItem]:
    """Fetch and parse an RSS feed asynchronously.

    Args:
        url: The RSS feed URL.

    Returns:
        List of RawRSSItem parsed from the feed.
    """
    feed = await asyncio.to_thread(_parse_feed_sync, url)
    items = []

    for entry in feed.entries:
        # Extract enclosure URL (.torrent download link)
        torrent_url = None
        content_length = None
        if hasattr(entry, "enclosures") and entry.enclosures:
            enc = entry.enclosures[0]
            torrent_url = enc.get("url")
            if enc.get("length"):
                try:
                    content_length = int(enc["length"])
                except (ValueError, TypeError):
                    pass

        # Extract published date — try mikan namespace first, then standard
        published_at = None
        if hasattr(entry, "torrent_pubdate"):
            try:
                published_at = datetime.fromisoformat(entry.torrent_pubdate)
            except (ValueError, TypeError):
                pass
        elif hasattr(entry, "published_parsed") and entry.published_parsed:
            published_at = datetime(*entry.published_parsed[:6])

        # Try mikan namespace content length as fallback
        if content_length is None and hasattr(entry, "torrent_contentlength"):
            try:
                content_length = int(entry.torrent_contentlength)
            except (ValueError, TypeError):
                pass

        items.append(RawRSSItem(
            guid=getattr(entry, "id", entry.get("title", "")),
            title=entry.get("title", ""),
            description=entry.get("description"),
            link=entry.get("link"),
            torrent_url=torrent_url,
            content_length=content_length,
            published_at=published_at,
        ))

    return items


async def get_raw_entries(url: str, limit: int = 5) -> list[dict]:
    """Fetch RSS feed and return raw entry dicts for LLM analysis.

    Converts feedparser entries to plain dicts, suitable for JSON serialization.

    Args:
        url: The RSS feed URL.
        limit: Max number of entries to return.

    Returns:
        List of entry dicts.
    """
    feed = await asyncio.to_thread(_parse_feed_sync, url)
    entries = []
    for entry in feed.entries[:limit]:
        # Convert feedparser entry to a plain dict
        entry_dict = {}
        for key in entry.keys():
            value = entry[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                entry_dict[key] = value
            elif isinstance(value, list):
                entry_dict[key] = [
                    dict(item) if hasattr(item, "keys") else str(item)
                    for item in value
                ]
            else:
                entry_dict[key] = str(value)
        entries.append(entry_dict)
    return entries


def validate_rss_url(url: str) -> tuple[bool, str, int]:
    """Validate that an RSS URL is reachable and parseable.

    Returns:
        (is_valid, message, item_count)
    """
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            return False, f"Feed parse error: {feed.bozo_exception}", 0
        return True, "OK", len(feed.entries)
    except Exception as e:
        return False, str(e), 0
