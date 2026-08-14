"""HTTP integration tests for the organize subsystem API surface.

Covers the R2/R3 organize endpoints that the in-process pipeline tests cannot
reach over HTTP — logical storage volumes, media-server CRUD/scan/test (against
the test-server's mock Plex), derived libraries, organize rules, and the
plan/audit listing endpoints. These run against the primary app (under
coverage), complementing ``tests/integration/organize/test_organize_pipeline.py``
which exercises the planning/execution services in-process.
"""

from __future__ import annotations

import time
import uuid

from tests.integration.http._http import TEST_SERVER, _api

PLEX_URL = f"{TEST_SERVER}/plex"


def _suffix() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def _quiet_delete(path: str) -> None:
    try:
        _api(path, method="delete")
    except Exception:
        pass


def _create_volume(name: str | None = None, mount_path: str = "/tmp") -> dict:
    r = _api(
        "/api/v1/volumes",
        method="post",
        json={
            "name": name or f"cov-vol-{_suffix()}",
            "mount_path": mount_path,
            "remark": "coverage",
        },
    )
    assert r.status_code == 201, f"create volume failed: {r.status_code} {r.text}"
    return r.json()["data"]


# =========================================================================
# StorageVolume CRUD
# =========================================================================


class TestStorageVolume:
    def test_crud_and_validation(self):
        vol = _create_volume()
        vid = vol["id"]
        try:
            assert vol["mount_path"] == "/tmp"

            # List (paginated) contains the row.
            r = _api("/api/v1/volumes", params={"page_size": 100})
            assert r.status_code == 200
            assert any(v["id"] == vid for v in r.json()["data"])

            # Get.
            r = _api(f"/api/v1/volumes/{vid}")
            assert r.status_code == 200
            assert r.json()["data"]["name"] == vol["name"]

            # Update (rename + remark).
            r = _api(
                f"/api/v1/volumes/{vid}",
                method="put",
                json={"name": f"cov-vol-renamed-{_suffix()}", "remark": "x"},
            )
            assert r.status_code == 200
            assert "renamed" in r.json()["data"]["name"]

            # Check mount existence/writability.
            r = _api(f"/api/v1/volumes/{vid}/check", method="post")
            assert r.status_code == 200
            assert r.json()["data"]["exists"] is True
        finally:
            _quiet_delete(f"/api/v1/volumes/{vid}")

    def test_error_paths(self):
        # Non-existent mount path → 422.
        r = _api(
            "/api/v1/volumes",
            method="post",
            json={"name": f"cov-vol-{_suffix()}", "mount_path": "/no/such/dir"},
        )
        assert r.status_code == 422

        # Empty name → 422.
        r = _api(
            "/api/v1/volumes",
            method="post",
            json={"name": "   ", "mount_path": "/tmp"},
        )
        assert r.status_code == 422

        # Duplicate name → 409.
        vol = _create_volume()
        try:
            r = _api(
                "/api/v1/volumes",
                method="post",
                json={"name": vol["name"], "mount_path": "/tmp"},
            )
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "DUPLICATE_SUBMISSION"
        finally:
            _quiet_delete(f"/api/v1/volumes/{vol['id']}")

        # 404s.
        assert _api("/api/v1/volumes/no-such-id").status_code == 404
        assert _api("/api/v1/volumes/no-such-id", method="put", json={"name": "x"}).status_code == 404
        assert _api("/api/v1/volumes/no-such-id/check", method="post").status_code == 404
        assert _api("/api/v1/volumes/no-such-id", method="delete").status_code == 404


# =========================================================================
# Media server CRUD + test/scan (mock Plex)
# =========================================================================


def _create_media_server(volume_id: str, *, enabled: bool = True, url: str = PLEX_URL) -> dict:
    r = _api(
        "/api/v1/media-servers",
        method="post",
        json={
            "name": f"cov-plex-{_suffix()}",
            "type": "plex",
            "url": url,
            "token": "mock-token",
            "enabled": enabled,
            "bindings": [
                {"server_path_prefix": "/data/tv", "volume_id": volume_id, "subpath": "tv"},
                {"server_path_prefix": "/data/movies", "volume_id": volume_id, "subpath": "movies"},
            ],
        },
    )
    assert r.status_code == 201, f"create media server failed: {r.status_code} {r.text}"
    return r.json()["data"]


class TestMediaServer:
    def test_create_test_scan_and_libraries(self):
        vol = _create_volume()
        vid = vol["id"]
        server = None
        try:
            server = _create_media_server(vid)
            sid = server["id"]
            assert server["enabled"] is True
            assert len(server["bindings"]) == 2
            assert "token" not in server  # token never echoed

            # Connectivity test → ok + mock version.
            r = _api(f"/api/v1/media-servers/{sid}/test", method="post")
            assert r.status_code == 200
            assert r.json()["data"]["ok"] is True
            assert "mock" in r.json()["data"]["server_version"]

            # Scan → creates 2 bound libraries + 1 unbound (artist skipped).
            r = _api(f"/api/v1/media-servers/{sid}/scan", method="post")
            assert r.status_code == 200, f"scan failed: {r.text}"
            stats = r.json()["data"]
            assert stats["created"] == 3
            assert stats["unbound"] == 1

            # Re-scan is idempotent (updates, not creates).
            r = _api(f"/api/v1/media-servers/{sid}/scan", method="post")
            assert r.status_code == 200
            assert r.json()["data"]["created"] == 0
            assert r.json()["data"]["updated"] == 3

            # List reports library counts.
            r = _api("/api/v1/media-servers")
            assert r.status_code == 200
            row = next(s for s in r.json()["data"] if s["id"] == sid)
            assert row["library_count"] == 3
            assert row["unbound_library_count"] == 1

            # Get + update + 404s.
            r = _api(f"/api/v1/media-servers/{sid}")
            assert r.status_code == 200
            r = _api(
                f"/api/v1/media-servers/{sid}",
                method="put",
                json={"name": "cov-plex-renamed", "bindings": []},
            )
            assert r.status_code == 200
            assert r.json()["data"]["name"] == "cov-plex-renamed"
            assert r.json()["data"]["bindings"] == []

            assert _api("/api/v1/media-servers/no-such-id").status_code == 404
            assert _api("/api/v1/media-servers/no-such-id", method="put", json={"name": "x"}).status_code == 404
            assert _api("/api/v1/media-servers/no-such-id", method="delete").status_code == 404
            assert _api("/api/v1/media-servers/no-such-id/test", method="post").status_code == 404
            assert _api("/api/v1/media-servers/no-such-id/scan", method="post").status_code == 404
        finally:
            if server:
                _quiet_delete(f"/api/v1/media-servers/{server['id']}")
            _quiet_delete(f"/api/v1/volumes/{vid}")

    def test_emby_jellyfin_scan_and_test(self):
        """Emby/Jellyfin adapter success paths (mock System/Info + VirtualFolders)."""
        vol = _create_volume()
        vid = vol["id"]
        ids = []
        try:
            for mtype in ("emby", "jellyfin"):
                r = _api(
                    "/api/v1/media-servers",
                    method="post",
                    json={
                        "name": f"cov-{mtype}-{_suffix()}",
                        "type": mtype,
                        "url": f"{TEST_SERVER}/emby",
                        "token": "mock-token",
                        "bindings": [
                            {"server_path_prefix": "/data/tv", "volume_id": vid, "subpath": "tv"},
                            {"server_path_prefix": "/data/movies", "volume_id": vid, "subpath": "movies"},
                        ],
                    },
                )
                assert r.status_code == 201, f"create {mtype} failed: {r.text}"
                mid = r.json()["data"]["id"]
                ids.append(mid)

                r = _api(f"/api/v1/media-servers/{mid}/test", method="post")
                assert r.status_code == 200
                assert r.json()["data"]["ok"] is True
                assert "mock" in r.json()["data"]["server_version"]

                r = _api(f"/api/v1/media-servers/{mid}/scan", method="post")
                assert r.status_code == 200, f"scan {mtype} failed: {r.text}"
                assert r.json()["data"]["created"] == 2
                assert r.json()["data"]["unbound"] == 0
        finally:
            for mid in ids:
                _quiet_delete(f"/api/v1/media-servers/{mid}")
            _quiet_delete(f"/api/v1/volumes/{vid}")

    def test_error_paths(self):
        vol = _create_volume()
        vid = vol["id"]
        try:
            # Binding references a non-existent volume → 404.
            r = _api(
                "/api/v1/media-servers",
                method="post",
                json={
                    "name": f"cov-plex-{_suffix()}",
                    "type": "plex",
                    "url": PLEX_URL,
                    "bindings": [{"server_path_prefix": "/data/tv", "volume_id": "no-such-vol", "subpath": ""}],
                },
            )
            assert r.status_code == 404

            # Invalid type → 422 (Literal).
            r = _api(
                "/api/v1/media-servers",
                method="post",
                json={"name": "x", "type": "kodi", "url": "http://x"},
            )
            assert r.status_code == 422

            # Unreachable server → test ok=False; scan → 502.
            r = _api(
                "/api/v1/media-servers",
                method="post",
                json={"name": f"cov-bad-{_suffix()}", "type": "emby", "url": "http://no-such-host.invalid", "token": "t"},
            )
            assert r.status_code == 201
            bad_id = r.json()["data"]["id"]
            try:
                r = _api(f"/api/v1/media-servers/{bad_id}/test", method="post")
                assert r.status_code == 200
                assert r.json()["data"]["ok"] is False

                r = _api(f"/api/v1/media-servers/{bad_id}/scan", method="post")
                assert r.status_code == 502
            finally:
                _quiet_delete(f"/api/v1/media-servers/{bad_id}")

            # Disabled server scan → 409.
            r = _api(
                "/api/v1/media-servers",
                method="post",
                json={"name": f"cov-off-{_suffix()}", "type": "plex", "url": PLEX_URL, "enabled": False},
            )
            assert r.status_code == 201
            off_id = r.json()["data"]["id"]
            try:
                r = _api(f"/api/v1/media-servers/{off_id}/scan", method="post")
                assert r.status_code == 409
            finally:
                _quiet_delete(f"/api/v1/media-servers/{off_id}")
        finally:
            _quiet_delete(f"/api/v1/volumes/{vid}")


# =========================================================================
# Libraries / organize rules / plans+audit (after a scan)
# =========================================================================


class TestOrganizeTargets:
    """One volume + one media server scanned once; then libraries/rules/plans."""

    def test_libraries_rules_plans_flow(self):
        vol = _create_volume()
        vid = vol["id"]
        server = None
        rule_id = None
        try:
            server = _create_media_server(vid)
            sid = server["id"]
            r = _api(f"/api/v1/media-servers/{sid}/scan", method="post")
            assert r.status_code == 200

            # List libraries (filter to this server's scan — prior tests may
            # have left orphaned libraries whose media_server_id is NULL).
            r = _api("/api/v1/libraries")
            assert r.status_code == 200
            libs = [lib for lib in r.json()["data"] if lib["media_server_id"] == sid]
            assert len(libs) == 3
            by_name = {lib["name"]: lib for lib in libs}
            tv = by_name["TV Shows"]
            movies = by_name["Movies"]
            unbound = by_name["Unbound Shows"]
            assert tv["bound"] is True and tv["root_path"] == "/tmp/tv"
            assert movies["bound"] is True and movies["root_path"] == "/tmp/movies"
            assert unbound["bound"] is False and unbound["root_path"] is None
            assert tv["media_server_id"] == sid

            # Unbound filter (scoped to this server's scan).
            r = _api("/api/v1/libraries", params={"unbound": "true"})
            assert r.status_code == 200
            unbound_names = [
                lib["name"] for lib in r.json()["data"] if lib["media_server_id"] == sid
            ]
            assert unbound_names == ["Unbound Shows"]

            # Get + 404.
            r = _api(f"/api/v1/libraries/{tv['id']}")
            assert r.status_code == 200
            assert _api("/api/v1/libraries/no-such-id").status_code == 404

            # Update (subtitle map) + rebind the unbound library.
            r = _api(
                f"/api/v1/libraries/{tv['id']}",
                method="put",
                json={"subtitle_lang_map": {"zh-CN": "chs"}},
            )
            assert r.status_code == 200
            assert r.json()["data"]["subtitle_lang_map"] == {"zh-CN": "chs"}

            r = _api(
                f"/api/v1/libraries/{unbound['id']}",
                method="put",
                json={"volume_id": vid, "root_subpath": "other"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["bound"] is True
            assert r.json()["data"]["root_path"] == "/tmp/other"

            # Extra field is forbidden (R2 derived-only).
            r = _api(
                f"/api/v1/libraries/{tv['id']}",
                method="put",
                json={"name": "hack"},
            )
            assert r.status_code == 422

            # Update with unknown volume → 404.
            r = _api(
                f"/api/v1/libraries/{tv['id']}",
                method="put",
                json={"volume_id": "no-such-vol"},
            )
            assert r.status_code == 404

            # --- Organize rules ---
            r = _api(
                "/api/v1/organize-rules",
                method="post",
                json={
                    "name": f"cov-rule-{_suffix()}",
                    "priority": 10,
                    "library_id": tv["id"],
                    "path_template": "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}[ - {episode_title}]{ext}",
                    "file_op": "move",
                    "auto_execute": False,
                },
            )
            assert r.status_code == 201, f"create rule failed: {r.text}"
            rule = r.json()["data"]
            rule_id = rule["id"]

            r = _api("/api/v1/organize-rules")
            assert r.status_code == 200
            assert any(x["id"] == rule_id for x in r.json()["data"])

            r = _api(f"/api/v1/organize-rules/{rule_id}")
            assert r.status_code == 200

            r = _api(
                f"/api/v1/organize-rules/{rule_id}",
                method="put",
                json={"priority": 5, "file_op": "hardlink"},
            )
            assert r.status_code == 200
            assert r.json()["data"]["priority"] == 5
            assert r.json()["data"]["file_op"] == "hardlink"

            # Rule validation paths.
            r = _api(
                "/api/v1/organize-rules",
                method="post",
                json={
                    "name": "bad-filter",
                    "library_id": tv["id"],
                    "path_template": "{title}{ext}",
                    "filter": {"field": "series.title", "operator": "eq", "value": ""},
                },
            )
            assert r.status_code == 422

            r = _api(
                "/api/v1/organize-rules",
                method="post",
                json={"name": "bad-template", "library_id": tv["id"], "path_template": "/abs/path"},
            )
            assert r.status_code == 422

            r = _api(
                "/api/v1/organize-rules",
                method="post",
                json={"name": "no-lib", "library_id": "no-such-lib", "path_template": "{title}{ext}"},
            )
            assert r.status_code == 404

            assert _api("/api/v1/organize-rules/no-such-id").status_code == 404
            assert _api("/api/v1/organize-rules/no-such-id", method="put", json={"name": "x"}).status_code == 404
            assert _api("/api/v1/organize-rules/no-such-id", method="delete").status_code == 404

            # --- Plans / audit (listing + validation; no execution) ---
            r = _api("/api/v1/organize/plans")
            assert r.status_code == 200
            assert r.json()["meta"]["total"] == 0

            r = _api("/api/v1/organize/plans", params={"status": "bogus"})
            assert r.status_code == 422

            assert _api("/api/v1/organize/plans/no-such-id").status_code == 404

            r = _api(
                "/api/v1/organize/plans/execute-batch",
                method="post",
                json={"plan_ids": []},
            )
            assert r.status_code == 422

            assert _api("/api/v1/organize/plans/no-such-id/cancel", method="post").status_code == 404
            assert _api("/api/v1/organize/plans/no-such-id/execute", method="post").status_code == 404
            assert _api(
                "/api/v1/organize/plans/no-such-id/classify",
                method="post",
                json={"library_id": tv["id"]},
            ).status_code == 404

            r = _api("/api/v1/organize/audit")
            assert r.status_code == 200
            assert r.json()["meta"]["total"] == 0

            # --- Delete-blocked chains ---
            # Volume still referenced by the media-server bindings → 409.
            r = _api(f"/api/v1/volumes/{vid}", method="delete")
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "DELETE_BLOCKED"

            # Library referenced by the rule → 409.
            r = _api(f"/api/v1/libraries/{tv['id']}", method="delete")
            assert r.status_code == 409

            # Delete rule → then library → then server → then volume.
            r = _api(f"/api/v1/organize-rules/{rule_id}", method="delete")
            assert r.status_code == 200
            rule_id = None

            r = _api(f"/api/v1/libraries/{tv['id']}", method="delete")
            assert r.status_code == 200
        finally:
            if rule_id:
                _quiet_delete(f"/api/v1/organize-rules/{rule_id}")
            if server:
                _quiet_delete(f"/api/v1/media-servers/{server['id']}")
            _quiet_delete(f"/api/v1/volumes/{vid}")
