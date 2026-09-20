"""Isolated integration entrypoint: real application with test source HTTP routing."""
from tests.integration.server.source_redirect import install

install()

from app.main import app  # noqa: E402,F401
