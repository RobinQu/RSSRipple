"""Read-only export of orphan rows for the lightweight-migration foreign keys.

Run with the target DATABASE_URL before upgrading. Exit 1 means manual repair
is needed; this command never calls startup DDL or invents parent associations.
"""

import argparse
import asyncio
import json

from sqlalchemy import inspect, text

import app.database as database
import app.models  # noqa: F401
from app.services.schema_foreign_keys import _COLUMNS


async def export_orphans(path: str) -> int:
    total = 0
    async with database.engine.connect() as conn:
        quote = conn.dialect.identifier_preparer.quote
        existing = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
        columns = {}
        with open(path, "w", encoding="utf-8") as output:
            for table, column in _COLUMNS:
                if table not in existing:
                    continue
                if table not in columns:
                    columns[table] = {
                        c["name"] for c in await conn.run_sync(lambda sync: inspect(sync).get_columns(table))
                    }
                if column not in columns[table]:
                    continue
                (fk,) = database.Base.metadata.tables[table].c[column].foreign_keys
                target, target_column = fk.column.table.name, fk.column.name
                join = ""
                predicate = ""
                if target in existing:
                    join = f" LEFT JOIN {quote(target)} parent ON child.{quote(column)}=parent.{quote(target_column)}"
                    predicate = f" AND parent.{quote(target_column)} IS NULL"
                query = text(
                    f"SELECT child.id, child.{quote(column)} AS parent_id FROM {quote(table)} child"
                    f"{join} WHERE child.{quote(column)} IS NOT NULL{predicate} ORDER BY child.id"
                ).execution_options(yield_per=100)
                async for row in await conn.stream(query):
                    output.write(
                        json.dumps(
                            {
                                "table": table,
                                "column": column,
                                "id": row.id,
                                "parent_id": row.parent_id,
                                "target": f"{target}.{target_column}",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    total += 1
    print(f"orphan references: {total}; report: {path}")
    return 1 if total else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="JSONL file containing every orphan reference")
    args = parser.parse_args()

    async def run():
        try:
            return await export_orphans(args.output)
        finally:
            await database.engine.dispose()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
