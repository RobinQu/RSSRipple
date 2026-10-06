"""Administrative destination exceptions cannot accidentally grant a broad scope."""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("value", [
    "file:///tmp/feed", "http://*.internal", "http://user:pass@host",
    "http://host/feed", "http://host?token=secret", "http://host#fragment",
    "http://10.0.0.0/8", "host", "http://host:bad", "http://host\n",
])
def test_private_origin_configuration_rejects_non_origins(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, outbound_private_origins=[value])


def test_private_origins_normalize_scheme_host_and_default_port():
    configured = Settings(_env_file=None, outbound_private_origins=[
        "HTTP://Example.COM:80", "http://example.com", "https://[::1]:8443",
    ])
    assert configured.outbound_private_origins == ["http://example.com", "https://[::1]:8443"]
