"""ResourceMetadata: the intermediate result between a raw RSS title and the
DB entities (TVSeries / Movie / AudioWork). Pure data module - no DB, no LLM.

Extracted verbatim from metadata_agent.py (Phase 0 leaf extraction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ResourceMetadata:
    """Metadata extracted from a single RSS resource title.

    Independent of any DB entity. Used as the output of MetadataAgent
    in both production (applied to FileResource/TVSeries/Movie) and
    evaluation (compared against GroundTruth) flows.
    """

    # ── Core ──
    clean_title: str
    content_type: Literal[
        "tv", "movie", "asmr", "music", "drama_cd", "radio", "other"
    ] = "tv"
    found: bool = True

    # ── Inferred resource fields (subset of FileResource columns) ──
    title_cn: str | None = None
    title_en: str | None = None
    episode: int | None = None
    season: int | None = None
    # Multi-episode batch (合集). ``is_batch`` marks torrents containing many
    # episodes. ``episode_start`` / ``episode_end`` are best-effort — a batch
    # title may not spell out the boundaries (e.g. "Batch", "全集").
    is_batch: bool = False
    episode_start: int | None = None
    episode_end: int | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    subtitle_group: str | None = None
    # BCP-47 language tags: ["zh-CN", "zh-TW", "ja", "en"], or ["multi"] for
    # titles marked "多语言" / "多国字幕" without specifics. None means the
    # LLM had nothing to say — pre-parser output is kept.
    subtitle_langs: list[str] | None = None
    container: str | None = None

    # ── Matched entity metadata (upserted into TVSeries or Movie) ──
    matched_entity: dict | None = None
    # Keys: external_id, external_source, title_cn, title_en,
    #       original_title, description, poster_url, rating, genre,
    #       status, number_of_episodes, number_of_seasons,
    #       start_date, end_date, release_date, runtime,
    #       canonical_name, wikipedia_url

    # ── Quality ──
    confidence: float = 0.0
    reason: str | None = None

    # ── Ambiguity (for manual resolution) ──
    ambiguous: bool = False
    ambiguous_candidates: list[dict] = field(default_factory=list)

    # ── Search tracking (for eval) ──
    search_method: str | None = None
    data_sources_used: list[str] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    search_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ResourceMetadata:
        """Construct from the finalize tool's JSON output."""
        entity = data.get("matched_entity") or {}
        return cls(
            clean_title=data.get("clean_title", ""),
            content_type=data.get("content_type", "tv"),
            found=data.get("found", True),
            title_cn=data.get("title_cn") or entity.get("title_cn"),
            title_en=data.get("title_en") or entity.get("title_en"),
            episode=data.get("inferred_episode"),
            season=data.get("inferred_season"),
            is_batch=bool(data.get("is_batch", False)),
            episode_start=data.get("inferred_episode_start") or data.get("episode_start"),
            episode_end=data.get("inferred_episode_end") or data.get("episode_end"),
            resolution=data.get("resolution"),
            source=data.get("source"),
            video_codec=data.get("video_codec"),
            audio_codec=data.get("audio_codec"),
            subtitle_type=data.get("subtitle_type"),
            subtitle_group=data.get("subtitle_group"),
            subtitle_langs=data.get("subtitle_langs"),
            container=data.get("container"),
            matched_entity=entity if entity else None,
            confidence=float(data.get("confidence", 0.0)),
            reason=data.get("reason"),
            ambiguous=data.get("ambiguous", False),
            ambiguous_candidates=data.get("ambiguous_candidates", []),
            search_method=data.get("search_method"),
            data_sources_used=data.get("data_sources_used") or [],
            source_errors=data.get("source_errors") or {},
            search_error=data.get("search_error"),
        )
