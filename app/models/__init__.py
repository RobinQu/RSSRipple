"""ORM models package - import all for SQLAlchemy discovery."""

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_suggestion import AgentSuggestion
from app.models.agent_work import AgentWork
from app.models.app_setting import AppSetting
from app.models.audio_work import AudioWork
from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.eval_job import EvalJob
from app.models.file_resource import FileResource
from app.models.ground_truth import GroundTruthEntry
from app.models.metadata_cache import MetadataCache
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries

__all__ = [
    "Channel",
    "FileResource",
    "Episode",
    "TVSeries",
    "Movie",
    "AudioWork",
    "Agent",
    "AgentRun",
    "AgentWork",
    "AgentSuggestion",
    "ChannelRawTitleMapping",
    "DownloaderInstance",
    "DownloadTask",
    "PendingDecision",
    "MetadataCache",
    "GroundTruthEntry",
    "EvalJob",
    "AppSetting",
]
