"""Atomic plan revision transitions; filesystem ownership is a separate guard."""
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.models.organize_configuration import CONFIGURATION_ID, OrganizeConfiguration
from app.models.organize_plan import OrganizePlan


async def configuration_revision(db):
    revision = await db.scalar(select(OrganizeConfiguration.revision).where(
        OrganizeConfiguration.id == CONFIGURATION_ID,
    ))
    return revision if revision is not None else 0


async def reserve_revision(db, plan, *, expected_revision, config_revision, states, expected_owner=None, **values):
    """Reserve before changing operations. Caller commits all changes together.

    A running recovery can pass config_revision=None only while holding the
    shared file lock; its previously frozen operation semantics are retained.
    """
    conditions = [OrganizePlan.id == plan.id, OrganizePlan.revision == expected_revision,
                  OrganizePlan.status.in_(states)]
    if expected_owner is not None:
        conditions.append(OrganizePlan.owner_token == expected_owner)
    if config_revision is not None:
        current = select(OrganizeConfiguration.revision).where(
            OrganizeConfiguration.id == CONFIGURATION_ID,
        ).scalar_subquery()
        conditions.append(current == config_revision)
    changed = await db.scalar(update(OrganizePlan).where(*conditions).values(
        revision=OrganizePlan.revision + 1, **values,
    ).returning(OrganizePlan.revision).execution_options(synchronize_session=False))
    if changed is None:
        return False
    set_committed_value(plan, "revision", changed)
    for key, value in values.items():
        set_committed_value(plan, key, value)
    return True
