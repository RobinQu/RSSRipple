"""Regression tests for the SPA static-file catch-all path-traversal fix.

``serve_spa`` previously did ``STATIC_DIR / full_path`` and served any file
that existed, so a decoded ``/%2e%2e/<secret>`` path escaped the static root
without authentication. ``_resolve_static_file`` must contain every candidate
inside the static root.
"""

from __future__ import annotations

from pathlib import Path

from app.main import _resolve_static_file


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    # Secret sibling outside the static root — the traversal target.
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return root


def test_resolve_static_file_serves_asset(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    resolved = _resolve_static_file("assets/app.js", base=root)
    assert resolved is not None
    assert resolved == (root / "assets" / "app.js").resolve()


def test_resolve_static_file_serves_index(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    resolved = _resolve_static_file("index.html", base=root)
    assert resolved is not None
    assert resolved.name == "index.html"


def test_resolve_static_file_rejects_parent_traversal(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    assert _resolve_static_file("../secret.txt", base=root) is None


def test_resolve_static_file_rejects_nested_traversal(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    assert _resolve_static_file("foo/../../secret.txt", base=root) is None


def test_resolve_static_file_rejects_absolute_escape(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    outside = tmp_path / "secret.txt"
    # A leading slash must not turn the join into an absolute host path.
    assert _resolve_static_file(f"/{outside}", base=root) is None


def test_resolve_static_file_missing_returns_none(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    # SPA client routes are not files — the caller falls back to index.
    assert _resolve_static_file("works", base=root) is None


def test_serve_spa_does_not_leak_outside_static_root() -> None:
    """End-to-end: an encoded traversal must not return the secret file."""
    from fastapi.testclient import TestClient

    from app.main import app

    # ``app/main.py`` is a sibling of ``app/static``; this path is only
    # reachable if the traversal succeeds. No ``with`` block: the test must
    # not run the app lifespan (which would start the scheduler/DB).
    client = TestClient(app)
    resp = client.get("/%2e%2e/main.py")
    assert resp.status_code == 404
    assert "STATIC_DIR" not in resp.text
