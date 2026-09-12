"""Shared query/serialization helpers for the download-task list endpoints
(downloader local tasks, agent tasks). Both endpoints expose the same
capabilities: effective-status display, ``status`` filter, ``sort`` spec and
eager-loaded resource/work metadata for the task table.
"""

import logging

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries

logger = logging.getLogger(__name__)

# Sortable keys for the ``sort`` query param (`key:asc|desc`, comma-separated,
# applied in order). ``status`` ranks by completion so asc puts unfinished
# tasks first; ``title`` needs the resource join.
TASK_SORT_KEYS = ("status", "created_at", "progress", "title")

DEFAULT_TASK_SORT: list[tuple[str, bool]] = [("status", True), ("created_at", False)]


def parse_task_sort(sort: str | None) -> list[tuple[str, bool]]:
    """Parse the sort spec into (key, ascending) entries; unknown keys are
    dropped and an all-unknown spec falls back to the default ordering."""
    entries: list[tuple[str, bool]] = []
    for part in (sort or "").split(","):
        key, _, direction = part.strip().partition(":")
        if key in TASK_SORT_KEYS:
            entries.append((key, direction.lower() != "desc"))
    return entries or list(DEFAULT_TASK_SORT)


def apply_task_status_filter(query, status: str | None):
    """Filter by *effective* status (the display status): ``completed`` also
    matches organized rows (raw ``cancelled`` with ``completed_at`` set —
    organize cleanup flips completed tasks), and ``cancelled`` only matches
    genuine cancels."""
    if not status:
        return query
    if status == "completed":
        return query.where(
            or_(
                DownloadTask.status == "completed",
                and_(
                    DownloadTask.status == "cancelled",
                    DownloadTask.completed_at.isnot(None),
                ),
            )
        )
    if status == "cancelled":
        return query.where(
            DownloadTask.status == "cancelled",
            DownloadTask.completed_at.is_(None),
        )
    return query.where(DownloadTask.status == status)


def apply_task_sort(query, entries: list[tuple[str, bool]]):
    """Order a task query by parsed sort entries, with the id ascending as a
    stable tiebreaker so paginating a sorted list never reshuffles rows."""
    sort_columns = {
        "status": case(
            (DownloadTask.status.in_(["completed", "cancelled"]), 1),
            else_=0,
        ),
        "created_at": DownloadTask.created_at,
        "progress": DownloadTask.progress,
        "title": FileResource.title_raw,
    }
    if any(key == "title" for key, _ in entries):
        query = query.outerjoin(
            FileResource, DownloadTask.file_resource_id == FileResource.id
        )
    order_clauses = [
        sort_columns[key].asc() if ascending else sort_columns[key].desc()
        for key, ascending in entries
    ]
    order_clauses.append(DownloadTask.id.asc())
    return query.order_by(*order_clauses)


def task_list_load_options():
    """Eager-load the resource's linked works (and their collections) so the
    task table can render poster/work metadata like the channel list does."""
    return [
        selectinload(DownloadTask.file_resource).selectinload(
            FileResource.series
        ).selectinload(TVSeries.collection),
        selectinload(DownloadTask.file_resource).selectinload(
            FileResource.movie
        ).selectinload(Movie.collection),
        selectinload(DownloadTask.file_resource).selectinload(FileResource.audio_work),
        selectinload(DownloadTask.file_resource).selectinload(FileResource.collection),
        selectinload(DownloadTask.agent),
    ]


async def apply_effective_task_statuses(
    db: AsyncSession, tasks: list[DownloadTask], payload: list[dict]
) -> None:
    """Rewrite ``status`` in the serialized payload to the effective display
    status, in place.

    The raw status lies about completion: organize cleanup
    (``task_cleanup.delete_task_after_organize``) flips a *completed* task to
    ``cancelled`` after moving its files into the library, and the progress
    sync does the same when the torrent leaves the daemon. ``completed_at``
    is the reliable completion marker — same semantics as the channel
    resource list's dispatch outcome (resources.py).
    """
    affected = [t for t in tasks if t.status == "cancelled" and t.completed_at is not None]
    if not affected:
        return
    from app.models.download_notification import DownloadNotification
    from app.models.organize_plan import OrganizePlan

    plan_rows = (await db.execute(
        select(DownloadNotification.download_task_id, OrganizePlan.status)
        .select_from(DownloadNotification)
        .outerjoin(
            OrganizePlan,
            OrganizePlan.notification_id == DownloadNotification.id,
        )
        .where(DownloadNotification.download_task_id.in_([t.id for t in affected]))
    )).all()
    plan_status_by_task = {task_id: plan_status for task_id, plan_status in plan_rows}
    effective = {
        t.id: "organized" if plan_status_by_task.get(t.id) == "done" else "completed"
        for t in affected
    }
    for row in payload:
        if row["id"] in effective:
            row["status"] = effective[row["id"]]
