"""Direct-Python integration tests hitting real external APIs.

These tests import app modules directly (no HTTP app fixture) and call real
LLM / TMDB / Exa / Wikipedia services. They do NOT require the docker-compose
test stack - only the relevant API keys (LLM_API_KEY / TMDB_API_KEY).
"""
