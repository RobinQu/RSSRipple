"""ORM models package - import all for SQLAlchemy discovery."""

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.movie import Movie
from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.agent_suggestion import AgentSuggestion
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.downloader import DownloaderInstance
from app.models.download_task import DownloadTask
from app.models.pending_decision import PendingDecision
from app.models.metadata_cache import MetadataCache

__all__ = [
    "Channel",
    "FileResource",
    "Episode",
    "TVSeries",
    "Movie",
    "Agent",
    "AgentWork",
    "AgentSuggestion",
    "ChannelRawTitleMapping",
    "DownloaderInstance",
    "DownloadTask",
    "PendingDecision",
    "MetadataCache",
]
