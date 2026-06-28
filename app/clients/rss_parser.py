"""RSS feed parser using feedparser.

Provides both sync and async entry access for downstream services.
Supports extracting both .torrent file URLs and magnet links from RSS entries.
The mikanani-specific namespace fields are extracted but the parser
is generic enough to work with any RSS/Atom feed.
"""

import asyncio
import re
from datetime import datetime

import feedparser
from pydantic import BaseModel

# Regex to find magnet links in text content
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s\"'<>]+", re.IGNORECASE)


class RawRSSItem(BaseModel):
    """Raw RSS item before database insertion."""
    guid: str
    title: str
    description: str | None = None
    link: str | None = None
    torrent_url: str | None = None
    magnet_url: str | None = None
    content_length: int | None = None
    published_at: datetime | None = None


def _parse_feed_sync(url: str) -> feedparser.FeedParserDict:
    """Synchronous feedparser call (blocking)."""
    return feedparser.parse(url)


def _extract_download_urls(entry) -> tuple[str | None, str | None]:
    """Extract torrent file URL and magnet link from an RSS entry.

    Searches multiple locations:
    1. enclosures (type application/x-bittorrent or .torrent URL)
    2. entry link field (may be a magnet link)
    3. entry description (magnet links embedded in HTML)

    Returns:
        (torrent_url, magnet_url) — prefers .torrent URLs for torrent_url,
        falls back to magnet if no .torrent found.
    """
    torrent_url = None
    magnet_url = None

    # 1. Check enclosures — most common location for .torrent URLs
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            url = enc.get("url", "")
            enc_type = enc.get("type", "")
            if url.startswith("magnet:"):
                if magnet_url is None:
                    magnet_url = url
            elif ".torrent" in url or "bittorrent" in enc_type:
                if torrent_url is None:
                    torrent_url = url
            elif url and torrent_url is None:
                # Generic enclosure — might be a torrent
                torrent_url = url

    # 2. Check entry link — some feeds put magnet links or .torrent URLs here
    link = entry.get("link", "")
    if link.startswith("magnet:") and magnet_url is None:
        magnet_url = link
    elif ".torrent" in link and torrent_url is None:
        torrent_url = link

    # 3. Search description for magnet links
    if magnet_url is None:
        description = entry.get("description", "") or ""
        magnet_match = _MAGNET_RE.search(description)
        if magnet_match:
            magnet_url = magnet_match.group(0)

    # 4. If no torrent URL found, use magnet as fallback for torrent_url
    if torrent_url is None and magnet_url:
        torrent_url = magnet_url

    return torrent_url, magnet_url


def _extract_content_length(entry) -> int | None:
    """Extract content length from entry, checking multiple sources."""
    # From enclosures
    if hasattr(entry, "enclosures") and entry.enclosures:
        enc = entry.enclosures[0]
        if enc.get("length"):
            try:
                return int(enc["length"])
            except (ValueError, TypeError):
                pass

    # Mikan namespace
    if hasattr(entry, "torrent_contentlength"):
        try:
            return int(entry.torrent_contentlength)
        except (ValueError, TypeError):
            pass

    return None


def _extract_published_at(entry) -> datetime | None:
    """Extract published date from entry."""
    # Mikan namespace
    if hasattr(entry, "torrent_pubdate"):
        try:
            return datetime.fromisoformat(entry.torrent_pubdate)
        except (ValueError, TypeError):
            pass
    # Standard RSS
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6])
    return None


async def parse_rss_feed(url: str) -> list[RawRSSItem]:
    """Fetch and parse an RSS feed asynchronously.

    Extracts both .torrent file URLs and magnet links from entries.

    Args:
        url: The RSS feed URL.

    Returns:
        List of RawRSSItem parsed from the feed.
    """
    feed = await asyncio.to_thread(_parse_feed_sync, url)
    items = []

    for entry in feed.entries:
        torrent_url, magnet_url = _extract_download_urls(entry)

        items.append(RawRSSItem(
            guid=getattr(entry, "id", entry.get("title", "")),
            title=entry.get("title", ""),
            description=entry.get("description"),
            link=entry.get("link"),
            torrent_url=torrent_url,
            magnet_url=magnet_url,
            content_length=_extract_content_length(entry),
            published_at=_extract_published_at(entry),
        ))

    return items


def _entry_to_dict(entry) -> dict:
    """Convert a feedparser entry to a plain dict suitable for JSON serialization.

    Handles the three value types feedparser produces: scalars, lists of dicts,
    and everything else (stringified). Shared between get_raw_entries (LLM
    analysis) and fetch_channel_resources (field-mapping application).
    """
    result = {}
    for key in entry.keys():
        value = entry[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        elif isinstance(value, list):
            result[key] = [
                dict(item) if hasattr(item, "keys") else str(item)
                for item in value
            ]
        else:
            result[key] = str(value)
    return result


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
    return [_entry_to_dict(entry) for entry in feed.entries[:limit]]


async def validate_rss_url(url: str) -> tuple[bool, str, int, int]:
    """Validate that an RSS URL is reachable, parseable, and has downloadable content.

    Checks:
    1. Feed is reachable and parseable (not a bozo feed)
    2. Feed has at least one entry
    3. At least some entries have .torrent URLs or magnet links

    Args:
        url: The RSS feed URL to validate.

    Returns:
        (is_valid, message, item_count, downloadable_count)
    """
    try:
        feed = await asyncio.to_thread(_parse_feed_sync, url)
    except Exception as e:
        return False, f"Cannot reach feed: {e}", 0, 0

    if feed.bozo and not feed.entries:
        reason = str(feed.bozo_exception) if hasattr(feed, "bozo_exception") else "unknown error"
        return False, f"Feed parse error: {reason}", 0, 0

    item_count = len(feed.entries)
    if item_count == 0:
        return False, "Feed is empty — no entries found", 0, 0

    # Check how many entries have downloadable content
    downloadable = 0
    for entry in feed.entries:
        torrent_url, magnet_url = _extract_download_urls(entry)
        if torrent_url or magnet_url:
            downloadable += 1

    if downloadable == 0:
        return False, (
            f"Feed has {item_count} entries but none contain torrent files or magnet links"
        ), item_count, 0

    return True, (
        f"Feed is valid: {item_count} entries, {downloadable} with downloadable content"
    ), item_count, downloadable
