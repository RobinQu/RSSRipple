"""Pydantic schemas package."""

from app.schemas.common import APIResponse, success_response, error_response, paginated_response
from app.schemas.channel import ChannelCreate, ChannelUpdate, ChannelResponse
from app.schemas.file_resource import FileResourceResponse
from app.schemas.filter import FilterCreate, FilterUpdate, FilterResponse
from app.schemas.agent import AgentCreate, AgentUpdate, AgentResponse
from app.schemas.downloader import DownloaderCreate, DownloaderUpdate, DownloaderResponse
from app.schemas.download_task import DownloadTaskResponse
from app.schemas.pending_decision import PendingDecisionResponse
from app.schemas.episode import EpisodeResponse
from app.schemas.series import TVSeriesCreate, TVSeriesUpdate, TVSeriesResponse
from app.schemas.movie import MovieCreate, MovieUpdate, MovieResponse
