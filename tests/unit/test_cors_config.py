"""Explicit browser-origin policy, independent from outbound exceptions."""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("value", ["*", "null", "https://*.example.test", "https://u:p@example.test",
                                 "https://example.test/path", "https://example.test/?q=1",
                                 "https://example.test/#fragment", "ftp://example.test", "https://example.test\n"])
def test_invalid_cors_origins_fail_startup(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_allowed_origins=[value])


def test_cors_origin_defaults_and_normalization():
    assert Settings(_env_file=None).cors_allowed_origins == []
    settings = Settings(_env_file=None, cors_allowed_origins=[
        "https://EXAMPLE.test:443/", "https://example.test", "http://[::1]:8080/",
    ])
    assert settings.cors_allowed_origins == ["https://example.test", "http://[::1]:8080"]
    assert settings.outbound_private_origins == []


def test_cors_json_environment(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://ui.example.test"]')
    assert Settings(_env_file=None).cors_allowed_origins == ["https://ui.example.test"]
