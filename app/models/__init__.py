"""ORM models package - import all for SQLAlchemy discovery."""

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.movie import Movie
from app.models.filter import ResourceFilter
from app.models.agent import Agent
from app.models.downloader import DownloaderInstance
from app.models.download_task import DownloadTask
from app.models.pending_decision import PendingDecision

__all__ = [
    "Channel",
    "FileResource",
    "Episode",
    "TVSeries",
    "Movie",
    "ResourceFilter",
    "Agent",
    "DownloaderInstance",
    "DownloadTask",
    "PendingDecision",
]
