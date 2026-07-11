"""API tests for the system-settings endpoint (LLM + external search sources)."""

from __future__ import annotations

from app.services.runtime_config import runtime_config


class TestSystemSettings:
    async def test_get_initial(self, client):
        res = await client.get("/api/v1/system-settings")
        assert res.status_code == 200
        data = res.json()["data"]
        assert "settings" in data
        assert "groups" in data
        assert "exa_effort_levels" in data

        # All recognized keys are present.
        for key in (
            "llm_api_key", "llm_model", "llm_base_url", "llm_enable_thinking",
            "tmdb_api_key", "jina_api_key", "exa_api_key", "exa_effort_level",
            "exa_enabled", "jina_enabled", "tmdb_enabled", "wikipedia_enabled",
        ):
            assert key in data["settings"], key

        # Non-secret defaults from env are returned in plaintext.
        llm_model = data["settings"]["llm_model"]
        assert llm_model["secret"] is False
        assert llm_model["value"]  # has a default model name

        # Secrets are not configured by default in the test env and never
        # expose a raw value.
        tmdb = data["settings"]["tmdb_api_key"]
        assert tmdb["secret"] is True
        assert tmdb["configured"] is False
        assert tmdb["value"] == ""

    async def test_put_updates_non_secret_and_persists(self, client):
        res = await client.put("/api/v1/system-settings", json={"llm_model": "my-model"})
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["settings"]["llm_model"]["value"] == "my-model"

        # Persisted: a fresh GET reflects the new value.
        got = await client.get("/api/v1/system-settings")
        assert got.json()["data"]["settings"]["llm_model"]["value"] == "my-model"

        # The runtime cache picked it up (no restart).
        assert runtime_config.llm_model == "my-model"

    async def test_put_secret_is_masked_and_applied(self, client):
        secret = "sk-test-supersecret-123456"
        res = await client.put("/api/v1/system-settings", json={"tmdb_api_key": secret})
        assert res.status_code == 200
        body = res.json()["data"]
        field = body["settings"]["tmdb_api_key"]
        assert field["configured"] is True
        # Masked: only the mask + last 4 chars; the raw secret is never returned.
        assert field["value"].endswith(secret[-4:])
        assert secret not in res.text

        # The runtime cache holds the real value.
        assert runtime_config.tmdb_api_key == secret

    async def test_put_bool_switch(self, client):
        res = await client.put(
            "/api/v1/system-settings",
            json={"exa_enabled": False, "wikipedia_enabled": False},
        )
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["settings"]["exa_enabled"]["value"] is False
        assert body["settings"]["wikipedia_enabled"]["value"] is False
        assert runtime_config.exa_enabled is False
        assert runtime_config.wikipedia_enabled is False

    async def test_put_exa_effort_level_validated(self, client):
        res = await client.put(
            "/api/v1/system-settings", json={"exa_effort_level": "bogus"}
        )
        assert res.status_code == 400

        ok = await client.put(
            "/api/v1/system-settings", json={"exa_effort_level": "high"}
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["settings"]["exa_effort_level"]["value"] == "high"

    async def test_put_rejects_empty_payload(self, client):
        res = await client.put("/api/v1/system-settings", json={})
        assert res.status_code == 400

    async def test_put_omitted_secret_unchanged(self, client):
        # Set a secret.
        await client.put("/api/v1/system-settings", json={"jina_api_key": "jina-orig"})
        assert runtime_config.jina_api_key == "jina-orig"

        # A PUT that does not mention jina_api_key must not clear it.
        await client.put("/api/v1/system-settings", json={"llm_model": "other"})
        assert runtime_config.jina_api_key == "jina-orig"
        got = await client.get("/api/v1/system-settings")
        assert got.json()["data"]["settings"]["jina_api_key"]["configured"] is True

    async def test_put_empty_secret_clears_override(self, client):
        # Set then clear via an explicit empty string.
        await client.put("/api/v1/system-settings", json={"exa_api_key": "exa-orig"})
        assert runtime_config.exa_api_key == "exa-orig"

        await client.put("/api/v1/system-settings", json={"exa_api_key": ""})
        # Cleared -> reverts to the env default (empty in the test env).
        assert runtime_config.exa_api_key == ""
        got = await client.get("/api/v1/system-settings")
        assert got.json()["data"]["settings"]["exa_api_key"]["configured"] is False

    async def test_put_resets_metadata_agent(self, client):
        # The agent singleton is rebuilt after a settings write so new LLM
        # config takes effect without a restart.
        from app.services import metadata_agent as ma_mod

        first = ma_mod.get_agent()
        await client.put("/api/v1/system-settings", json={"llm_model": "new-model"})
        second = ma_mod.get_agent()
        assert first is not second
