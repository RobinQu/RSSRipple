"""LLM-based RSS feed analysis service.

Uses an OpenAI-compatible LLM to analyze sample RSS entries and generate
per-channel field mappings for the dynamic resource parser.
"""

import json
import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

ANALYSIS_SYSTEM_PROMPT = """You are an RSS feed analyzer. Given sample RSS entry data (in JSON format),
generate field mapping rules that extract structured fields for a download resource manager.

The target fields to extract are:
- title_cn (str): Chinese title/name
- title_en (str): English title/name
- subtitle_group (str): Release group name (often in brackets at the start)
- episode (int): Episode number
- resolution (str): Video resolution (e.g., "1080p", "720p")
- source (str): Source type (e.g., "WebRip", "WEB-DL", "BDRip")
- video_codec (str): Video codec (e.g., "HEVC", "AVC", "H264")
- audio_codec (str): Audio codec (e.g., "AAC", "FLAC")
- subtitle_type (str): Subtitle type (e.g., "CHS", "CHT", "简繁")
- container (str): Container format (e.g., "MP4", "MKV")
- file_size (int): File size in bytes
- torrent_url (str): Download URL for the .torrent file
- detail_url (str): Detail page URL
- published_at (datetime): Publication date/time

Each field mapping rule has this structure:
{
  "source": "<feedparser entry field path>",
  "regex": "<optional regex pattern>",
  "group": <capture group index, default 0>,
  "transform": "<optional: int, float, iso_datetime, lowercase, uppercase>"
}

The "source" field is a dotted path into the feedparser entry object:
- "title" -> entry.title
- "enclosures[0].url" -> first enclosure's URL
- "description" -> entry description

Example mapping for a typical anime RSS feed:
{
  "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*/", "group": 1},
  "title_en": {"source": "title", "regex": "/\\s*(.+?)\\s*-", "group": 1},
  "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
  "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
  "resolution": {"source": "title", "regex": "\\b(1080p|720p|480p|2160p|4K)\\b", "group": 1, "transform": "lowercase"},
  "torrent_url": {"source": "enclosures[0].url"},
  "file_size": {"source": "enclosures[0].length", "transform": "int"},
  "published_at": {"source": "published", "transform": "iso_datetime"}
}

Respond with ONLY valid JSON containing the field_mapping object. No markdown, no explanation."""

ANALYSIS_USER_TEMPLATE = """Analyze these {count} sample RSS entries and generate field mapping rules:

{samples}

Generate the field_mapping JSON that can extract structured fields from entries like these."""


async def analyze_feed(entries: list[dict], sample_count: int = 5) -> dict:
    """Analyze sample RSS entries using LLM and generate field mappings.

    Args:
        entries: List of feedparser entry dicts (as plain dicts).
        sample_count: Number of entries to send to LLM (default 5).

    Returns:
        Dict with keys: field_mapping, sample_results, confidence
    """
    samples = entries[:sample_count]

    if not settings.llm_api_key:
        logger.warning("LLM API key not configured, cannot analyze feed")
        return {
            "field_mapping": {},
            "sample_results": [],
            "confidence": "low",
        }

    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )

    try:
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": ANALYSIS_USER_TEMPLATE.format(
                    count=len(samples),
                    samples=json.dumps(samples, ensure_ascii=False, indent=2, default=str),
                )},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        field_mapping = json.loads(content)

        # Basic validation: check that the mapping has expected structure
        validated_mapping = {}
        for field_name, rule in field_mapping.items():
            if isinstance(rule, dict) and "source" in rule:
                validated_mapping[field_name] = rule

        # Determine confidence based on coverage
        expected_fields = {
            "title_cn", "title_en", "subtitle_group", "episode",
            "resolution", "torrent_url",
        }
        covered = set(validated_mapping.keys()) & expected_fields
        coverage_ratio = len(covered) / len(expected_fields) if expected_fields else 0

        if coverage_ratio >= 0.8:
            confidence = "high"
        elif coverage_ratio >= 0.5:
            confidence = "medium"
        else:
            confidence = "low"

        return {
            "field_mapping": validated_mapping,
            "sample_results": [],
            "confidence": confidence,
        }

    except Exception as e:
        logger.error("LLM feed analysis failed: %s", e)
        return {
            "field_mapping": {},
            "sample_results": [],
            "confidence": "low",
        }
