"""RSS title parser for anime/bangumi releases.

Title format: [SubtitleGroup] ChineseName / EnglishName - Episode [Quality][Codec][Subtitle][Container]
Examples:
  [LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]
  [Skymoon-Raws] 黄泉双使 (黄泉使者) / Daemons of the Shadow Realm - 12 [ViuTV][WEB-DL][CHT][1080p][AVC AAC]
  [ANi] Daemons of the Shadow Realm / 黄泉使者 - 12 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]
"""

import re
from dataclasses import dataclass, field


@dataclass
class ParsedTitle:
    """Parsed fields from an RSS item title."""
    raw: str = ""
    subtitle_group: str | None = None
    title_cn: str | None = None
    title_en: str | None = None
    episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    subtitle_type: str | None = None
    container: str | None = None
    file_size: int | None = None


# Regex patterns
_SUBTITLE_GROUP_RE = re.compile(r"^\[([^\]]+)\]")
_NAMES_EPISODE_RE = re.compile(
    r"]\s*(.+?)\s*/\s*(.+?)\s*-\s*(\d+)\b"
)
_NAMES_EPISODE_ALT_RE = re.compile(
    r"]\s*(.+?)\s*-\s*(\d+)\b"
)

# Quality tags patterns
_RESOLUTION_RE = re.compile(r"\b(1080[pP]|720[pP]|480[pP]|2160[pP]|4K)\b")
_SOURCE_RE = re.compile(r"\b(WebRip|WEB[- ]DL|BDRip|Baha|ViuTV|AT-X|TokyoMX|BD|DVD)\b", re.IGNORECASE)
_VIDEO_CODEC_RE = re.compile(r"\b(HEVC[- ]?10bit|HEVC|AVC|H\.?264|H\.?265|x264|x265|AV1)\b", re.IGNORECASE)
_AUDIO_CODEC_RE = re.compile(r"\b(AAC|FLAC|Opus|DTS|EAC3|AC3|TrueHD)\b", re.IGNORECASE)
_CONTAINER_RE = re.compile(r"\b(MP4|MKV|AVI)\b", re.IGNORECASE)
_SUBTITLE_TYPE_RE = re.compile(
    r"(简繁内封字幕|简繁内封|简繁|简体|繁体|简日|繁日|CHT|CHS|GB|BIG5|简中|繁中|中英|中日)",
    re.IGNORECASE,
)

# File size from description
_FILE_SIZE_RE = re.compile(r"\[?([\d.]+)\s*(GB|MB|KB)\]?", re.IGNORECASE)


def parse_title(title: str, description: str | None = None) -> ParsedTitle:
    """Parse an RSS item title into structured fields.

    Args:
        title: The raw RSS item title.
        description: Optional description (may contain file size).

    Returns:
        ParsedTitle with extracted fields.
    """
    result = ParsedTitle(raw=title)

    # 1. Extract subtitle group (first bracketed content)
    sg_match = _SUBTITLE_GROUP_RE.match(title)
    if sg_match:
        result.subtitle_group = sg_match.group(1).strip()

    # 2. Extract names and episode
    ne_match = _NAMES_EPISODE_RE.search(title)
    if ne_match:
        name1 = ne_match.group(1).strip()
        name2 = ne_match.group(2).strip()
        result.episode = int(ne_match.group(3))
        # Determine which is CN and which is EN
        result.title_cn, result.title_en = _classify_names(name1, name2)
    else:
        ne_alt_match = _NAMES_EPISODE_ALT_RE.search(title)
        if ne_alt_match:
            name = ne_alt_match.group(1).strip()
            result.episode = int(ne_alt_match.group(2))
            if _has_cjk(name):
                result.title_cn = name
            else:
                result.title_en = name

    # 3. Extract quality fields from bracketed tags
    bracket_content = " ".join(re.findall(r"\[([^\]]*)\]", title))
    # Also include the full title for patterns that appear outside brackets
    search_text = title

    res_match = _RESOLUTION_RE.search(search_text)
    if res_match:
        result.resolution = res_match.group(1).lower().replace("p", "p")
        if result.resolution == "1080p" or result.resolution == "1080P":
            result.resolution = "1080p"
        elif result.resolution == "720p" or result.resolution == "720P":
            result.resolution = "720p"

    src_match = _SOURCE_RE.search(bracket_content)
    if src_match:
        result.source = src_match.group(1)

    vc_match = _VIDEO_CODEC_RE.search(bracket_content)
    if vc_match:
        result.video_codec = vc_match.group(1)

    ac_match = _AUDIO_CODEC_RE.search(bracket_content)
    if ac_match:
        result.audio_codec = ac_match.group(1).upper()

    ct_match = _CONTAINER_RE.search(bracket_content)
    if ct_match:
        result.container = ct_match.group(1).upper()

    st_match = _SUBTITLE_TYPE_RE.search(search_text)
    if st_match:
        result.subtitle_type = st_match.group(1)

    # 4. Extract file size from description
    if description:
        size_match = _FILE_SIZE_RE.search(description)
        if size_match:
            size_val = float(size_match.group(1))
            unit = size_match.group(2).upper()
            if unit == "GB":
                result.file_size = int(size_val * 1024 * 1024 * 1024)
            elif unit == "MB":
                result.file_size = int(size_val * 1024 * 1024)
            elif unit == "KB":
                result.file_size = int(size_val * 1024)

    return result


def _classify_names(name1: str, name2: str) -> tuple[str | None, str | None]:
    """Classify two names as Chinese and English.

    Returns:
        (title_cn, title_en)
    """
    n1_cjk = _has_cjk(name1)
    n2_cjk = _has_cjk(name2)

    if n1_cjk and not n2_cjk:
        return name1, name2
    elif not n1_cjk and n2_cjk:
        return name2, name1
    elif n1_cjk and n2_cjk:
        # Both CJK, first is likely primary
        return name1, name2
    else:
        # Neither CJK
        return None, name1  # Treat first as English


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _has_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    return bool(_CJK_RE.search(text))


def extract_file_size_from_content_length(content_length: int | None) -> int | None:
    """Convert contentLength (bytes) to file_size."""
    return content_length
