"""Temporary script: verify LLM config from .env by sending a minimal test request.

Uses httpx directly (bypassing the openai SDK) to avoid openai 1.51 / httpx 0.28
incompatibility (proxies argument removed in httpx 0.28).
"""

import asyncio
import json
import sys
import time


async def main():
    import httpx
    from app.config import settings

    print("=== LLM Config Check ===")
    print(f"  LLM_BASE_URL : {settings.llm_base_url}")
    print(f"  LLM_MODEL    : {settings.llm_model}")
    key = settings.llm_api_key
    print(f"  LLM_API_KEY  : {key[:8]}...{key[-4:] if len(key) > 12 else '(too short)'}")
    print()

    if not settings.llm_api_key:
        print("[FAIL] LLM_API_KEY is not set.")
        sys.exit(1)

    if not settings.llm_base_url:
        print("[FAIL] LLM_BASE_URL is not set.")
        sys.exit(1)

    base = settings.llm_base_url.rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "temperature": 0.0,
        "max_tokens": 16,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    print(f"POST {url}")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            r = await client.post(url, json=payload, headers=headers)
        elapsed = time.monotonic() - t0

        if r.status_code != 200:
            print(f"[FAIL] HTTP {r.status_code} in {elapsed:.2f}s")
            print(f"       {r.text[:400]}")
            sys.exit(1)

        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        # Some reasoning models return output in reasoning_content when content is empty
        reasoning = msg.get("reasoning_content") or ""
        effective = content.strip() or reasoning.strip()
        model_used = data.get("model", "?")
        usage = data.get("usage", {})
        if effective:
            print(f"[OK] Response in {elapsed:.2f}s: {effective[:80]!r}")
        else:
            print(f"[WARN] Response in {elapsed:.2f}s: content is empty. Raw message fields: {list(msg.keys())}")
        print(f"     model used : {model_used}")
        if usage:
            print(f"     tokens     : prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")

    except httpx.ConnectError as e:
        print(f"[FAIL] Cannot connect: {e}")
        sys.exit(1)
    except httpx.TimeoutException:
        print("[FAIL] Request timed out after 30s")
        sys.exit(1)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"[FAIL] Unexpected response format: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
