"""HTTP confirmation uses the same exact coverage evidence as dispatch."""
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment


async def test_detail_and_dashboard_agree_on_known_and_unknown_coverage(
    client, db_session, sample_channel, sample_series,
):
    resources = []
    for tag in ["known", "unknown"]:
        row = FileResource(channel_id=sample_channel.id, series_id=sample_series.id,
            guid=f"synthetic-coverage-{tag}", title_raw=f"Synthetic {tag} batch",
            torrent_url="https://example.invalid/torrent", is_batch=True,
            batch_scope="season", season=sample_series.season_number)
        row.file_assignments = []
        if tag == "known":
            row.file_assignments = [ResourceFileAssignment(series_id=sample_series.id,
                season=sample_series.season_number, file_path="synthetic.mkv",
                episode_start=1, episode_end=12, source="manual")]
        resources.append(row)
    db_session.add_all(resources)
    await db_session.commit()
    for row, expected in zip(resources, [False, True]):
        response = await client.get(f"/api/v1/resources/{row.id}")
        assert response.status_code == 200
        assert ("batch_coverage_unknown" in response.json()["data"]["confirmation_kinds"]) is expected
    dashboard = await client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    kinds = {item["resource"]["id"]: item["kinds"]
             for item in dashboard.json()["data"]["pending_confirmations"]}
    assert "batch_coverage_unknown" not in kinds.get(resources[0].id, [])
    assert "batch_coverage_unknown" in kinds[resources[1].id]
