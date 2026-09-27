"""ORM models package - import all for SQLAlchemy discovery."""

from app.models.agent import Agent
from app.models.agent_publication_progress import AgentPublicationProgress
from app.models.agent_resource_request import AgentResourceRequest
from app.models.agent_run import AgentRun
from app.models.agent_suggestion import AgentSuggestion
from app.models.agent_webhook import AgentWebhook
from app.models.agent_work import AgentWork
from app.models.api_key import ApiKey
from app.models.app_setting import AppSetting
from app.models.audio_work import AudioWork
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.decision_migration import DecisionMigration
from app.models.download_dispatch import DownloadDispatch
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.eval_job import EvalJob
from app.models.file_resource import FileResource
from app.models.fts_outbox import FtsOutbox
from app.models.ground_truth import GroundTruthEntry
from app.models.library import Library
from app.models.media_server import MediaServerBinding, MediaServerInstance
from app.models.metadata_cache import MetadataCache
from app.models.movie import Movie
from app.models.notification_build_failure import NotificationBuildFailure
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_configuration import OrganizeConfiguration
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_publication import ChannelPublicationCounter, ResourcePublication
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.storage_volume import StorageVolume
from app.models.subtitle_group_mapping import SubtitleGroupMapping
from app.models.webhook_delivery import WebhookDelivery
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId

__all__ = [
    "AuthRateLimitBucket",
    "AgentPublicationProgress",
    "ChannelPublicationCounter",
    "ResourcePublication",
    "WorkExternalId",
    "Channel",
    "FileResource",
    "Episode",
    "TVSeries",
    "Movie",
    "AudioWork",
    "Agent",
    "AgentResourceRequest",
    "AgentRun",
    "AgentWork",
    "AgentSuggestion",
    "AgentWebhook",
    "ChannelRawTitleMapping",
    "DownloaderInstance",
    "DownloadTask",
    "DownloadDispatch",
    "DownloadNotification",
    "NotificationBuildFailure",
    "PendingDecision",
    "DecisionMigration",
    "ResourceWorkLink",
    "ResourceFileAssignment",
    "MetadataCache",
    "GroundTruthEntry",
    "EvalJob",
    "FtsOutbox",
    "AppSetting",
    "ApiKey",
    "WorkCollection",
    "WebhookDelivery",
    "Library",
    "StorageVolume",
    "SubtitleGroupMapping",
    "MediaServerInstance",
    "MediaServerBinding",
    "OrganizeRule",
    "OrganizePlan",
    "OrganizePlanOp",
    "OrganizeAuditEntry",
    "OrganizeConfiguration",
]

# Register ORM event hooks that keep search indexes in sync (Turso fts_outbox
# enqueue + PostgreSQL search_text maintenance). Imported last so the model
# classes above are already defined.
import app.services.organize_config_events as _organize_config_events  # noqa: E402, F401
import app.services.work_search_events as _work_search_events  # noqa: E402, F401

