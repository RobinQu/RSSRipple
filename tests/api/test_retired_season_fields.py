"""API-written retired counts must not turn a season work into a legacy IP."""

import pytest
from sqlalchemy import select

from app.models.episode import Episode
from app.models.series import TVSeries
from app.services.metadata_episode_reconcile import is_unsplit_legacy_series
from app.services.metadata_service import upsert_episodes


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_retired_count_rejected_without_cross_season_pollution(client, db_session_factory, operation):
    if operation == "create":
        response = await client.post(
            "/api/v1/series",
            json={
                "title_cn": "Synthetic retired-field guard",
                "number_of_seasons": 2,
            },
        )
    else:
        created = await client.post("/api/v1/series", json={"title_cn": "Synthetic retired-field guard"})
        assert created.status_code == 201
        response = await client.put("/api/v1/series/" + created.json()["data"]["id"], json={"number_of_seasons": 2})
    if response.status_code in {200, 201}:
        identity = response.json()["data"]["id"]
        async with db_session_factory() as db:
            work = await db.get(TVSeries, identity)
            legacy = is_unsplit_legacy_series(work)
            await upsert_episodes(
                db,
                work,
                [
                    {"season": 1, "episode": 1, "title": "Synthetic S1"},
                    {"season": 2, "episode": 1, "title": "Synthetic S2"},
                ],
            )
            await db.commit()
            seasons = list(
                await db.scalars(select(Episode.season).where(Episode.series_id == identity).order_by(Episode.season))
            )
            print(
                {
                    "status": response.status_code,
                    "work_season": work.season_number,
                    "legacy": legacy,
                    "stored_episode_seasons": seasons,
                    "protected": work.manually_edited_fields,
                }
            )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"number_of_seasons": None},
        {"number_of_seasons": 1},
        {"number_of_seasons": 2},
        {"seasons": None},
        {"seasons": [{"season_number": 1}]},
        {"number_of_seasons": 2, "seasons": []},
    ],
)
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_rejection_has_no_work_collection_or_manual_field_side_effects(
    client, db_session_factory, payload, operation
):
    from app.models.work_collection import WorkCollection

    if operation == "update":
        created = await client.post(
            "/api/v1/series", json={"title_cn": "Synthetic preserved", "number_of_episodes": 12}
        )
        assert created.status_code == 201
        identity = created.json()["data"]["id"]
    async with db_session_factory() as db:
        before = [
            (w.id, w.title_cn, w.collection_id, w.number_of_episodes, w.manually_edited_fields)
            for w in await db.scalars(select(TVSeries))
        ]
        parents = list(await db.scalars(select(WorkCollection.id)))
    if operation == "create":
        response = await client.post("/api/v1/series", json={"title_cn": "Must not persist", **payload})
    else:
        response = await client.put("/api/v1/series/" + identity, json={"title_cn": "Must not persist", **payload})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    async with db_session_factory() as db:
        assert [
            (w.id, w.title_cn, w.collection_id, w.number_of_episodes, w.manually_edited_fields)
            for w in await db.scalars(select(TVSeries))
        ] == before
        assert list(await db.scalars(select(WorkCollection.id))) == parents


async def test_legal_season_work_stays_season_local(client, db_session_factory):
    created = await client.post(
        "/api/v1/series", json={"title_cn": "Synthetic single season", "number_of_episodes": 12}
    )
    assert created.status_code == 201
    identity = created.json()["data"]["id"]
    updated = await client.put("/api/v1/series/" + identity, json={"title_cn": "Synthetic protected title"})
    assert updated.status_code == 200
    async with db_session_factory() as db:
        work = await db.get(TVSeries, identity)
        assert not is_unsplit_legacy_series(work)
        assert work.number_of_seasons is None
        assert work.manually_edited_fields == ["title_cn"]
        await upsert_episodes(
            db,
            work,
            [
                {"season": 1, "episode": 1, "title": "Synthetic S1"},
                {"season": 2, "episode": 1, "title": "Synthetic S2"},
            ],
        )
        await db.commit()
        assert list(await db.scalars(select(Episode.season).where(Episode.series_id == identity))) == [1]
