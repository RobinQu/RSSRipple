"""HTTP-level integration tests against the dockerized RSSRipple app.

These tests talk to the live app (default http://app:9001) over HTTP and require
the full docker-compose test stack (app + test-server [+ transmission]). Shared
HTTP helpers live in :mod:`tests.integration.http._http`.
"""
