"""P0-1: real middleware/router/filesystem boundary with authentication enabled.

ASGI transport skips lifespan infrastructure, but does not replace auth,
path resolution or serving. Sentinel bytes are synthetic sensitive content.
"""

import httpx
import pytest

from app import main
from app.config import settings


@pytest.mark.parametrize("path", [
    "/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt",
    "/%2E%2E/secret.txt", "/linked-secret.txt", "/linked-dir/secret.txt",
])
async def test_unauthenticated_static_escape_cannot_read_sibling(tmp_path, monkeypatch, path):
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>test SPA</html>")
    (root / "assets" / "app.js").write_text("console.log('public asset')")
    sentinel = b"PRIVATE-SIBLING-MUST-NOT-BE-SERVED"
    secret = tmp_path / "secret.txt"
    secret.write_bytes(sentinel)
    (root / "linked-secret.txt").symlink_to(secret)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(main, "STATIC_DIR", root)
    assert any(route.path == "/{full_path:path}" for route in main.app.routes), "frontend route must be installed"
    assets = next(route.app for route in main.app.routes if route.name == "static-assets")
    monkeypatch.setattr(assets, "directory", str(root / "assets"))
    monkeypatch.setattr(assets, "all_directories", [str(root / "assets")])
    monkeypatch.setattr(settings, "auth_enabled", True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/v1/channels")).status_code == 401
        response = await client.get(path)
        assert response.status_code in {200, 404}
        assert sentinel not in response.content
        if "%2e" in path.lower():
            assert response.status_code == 404
        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('public asset')"
        spa = await client.get("/works")
        assert spa.status_code == 200 and spa.text == "<html>test SPA</html>"
    assert secret.read_bytes() == sentinel
