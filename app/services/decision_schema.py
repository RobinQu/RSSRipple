"""Install pending-key guards only after legacy pending rows were reviewed.

Caller owns an ordinary DDL transaction. This helper never selects survivors,
changes candidates/status, or guesses keys for historical pending decisions.
"""

import json
import re

from sqlalchemy import inspect, text

from app.services.decision_store import stored_choice_matches


def _normalized_sql(sql):
    sql = re.sub(r"::(?:decision_status|text)", "", sql.lower())
    return re.sub(r'[\s()"]+', "", sql).replace("ifnotexists", "")


def _trigger_sql(operation):
    return (
        f"CREATE TRIGGER IF NOT EXISTS ck_pending_decision_key_{operation.lower()} "
        f"BEFORE {operation} ON pending_decisions "
        "WHEN NEW.status='pending' AND NEW.decision_key IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'pending decision requires identity key'); END"
    )


async def _validate_existing_guards(conn):
    indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("pending_decisions"))
    for index in indexes:
        if index["name"] != "uq_pending_decisions_agent_key":
            continue
        where = index.get("dialect_options", {}).get(f"{conn.dialect.name}_where")
        if (
            not index["unique"]
            or index["column_names"] != ["agent_id", "decision_key"]
            or where is None
            or _normalized_sql(str(where)) != "status='pending'"
        ):
            raise ValueError("Unexpected pending decision index definition")
    if conn.dialect.name == "sqlite":
        for operation in ("INSERT", "UPDATE"):
            existing = await conn.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
                {"name": f"ck_pending_decision_key_{operation.lower()}"},
            )
            if existing and _normalized_sql(existing) != _normalized_sql(_trigger_sql(operation)):
                raise ValueError("Unexpected pending decision trigger definition")
    else:
        constraints = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints("pending_decisions"))
        for constraint in constraints:
            if (
                constraint["name"] == "ck_pending_decision_key"
                and _normalized_sql(constraint["sqltext"]) != "status!='pending'ordecision_keyisnotnull"
            ):
                # PostgreSQL prints != as <>.
                if _normalized_sql(constraint["sqltext"]) != "status<>'pending'ordecision_keyisnotnull":
                    raise ValueError("Unexpected pending decision CHECK definition")


async def install_decision_constraints(conn):
    if conn.dialect.name not in {"sqlite", "postgresql"}:
        raise ValueError("Unsupported decision schema backend")
    await _validate_existing_guards(conn)
    columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("pending_decisions")})
    # Keep prepared result shapes stable across ADD COLUMN and rollback.
    projection = ["id", "agent_id"] + [name for name in ("decision_key", "decision_scope") if name in columns]
    pending = (
        await conn.execute(
            text(f"SELECT {', '.join(projection)} FROM pending_decisions WHERE status='pending' ORDER BY id")
        )
    ).mappings()
    seen = set()
    invalid = []
    for row in pending:
        key, scope = row.get("decision_key"), row.get("decision_scope")
        if isinstance(scope, str):
            try:
                scope = json.loads(scope)
            except ValueError:
                scope = None
        if not stored_choice_matches(key, scope):
            invalid.append(row["id"])
        pair = row["agent_id"], key
        if pair in seen:
            invalid.append(row["id"])
        seen.add(pair)
    if invalid:
        raise ValueError("Pending decisions require reviewed identity migration: " + ", ".join(sorted(set(invalid))))
    if "decision_key" not in columns:
        await conn.execute(text("ALTER TABLE pending_decisions ADD COLUMN decision_key VARCHAR(80)"))
    if "decision_scope" not in columns:
        await conn.execute(text("ALTER TABLE pending_decisions ADD COLUMN decision_scope JSON"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_decisions_agent_key "
            "ON pending_decisions (agent_id, decision_key) WHERE status='pending'"
        )
    )
    if conn.dialect.name == "postgresql":
        present = await conn.scalar(
            text(
                "SELECT 1 FROM pg_constraint WHERE conrelid='pending_decisions'::regclass "
                "AND conname='ck_pending_decision_key'"
            )
        )
        if not present:
            await conn.execute(
                text(
                    "ALTER TABLE pending_decisions ADD CONSTRAINT ck_pending_decision_key "
                    "CHECK (status != 'pending' OR decision_key IS NOT NULL)"
                )
            )
    elif conn.dialect.name == "sqlite":
        # SQLite cannot ADD CHECK. Both writes are guarded, including a
        # historical row becoming pending, without rebuilding unknown columns.
        for operation in ("INSERT", "UPDATE"):
            await conn.execute(text(_trigger_sql(operation)))


async def prepare_existing_decision_schema(conn):
    """Run before business startup/backfills; fresh tables use ORM constraints."""
    exists = await conn.run_sync(lambda sync: inspect(sync).has_table("pending_decisions"))
    if exists:
        await install_decision_constraints(conn)
