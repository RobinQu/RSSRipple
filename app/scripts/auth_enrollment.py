"""Explicit, terminal-only display of the existing administrator enrollment URI."""

import argparse
import asyncio
import sys

from app import database
from app.services.auth_service import SETTING_TOTP_SECRET, totp_provisioning_uri
from app.services.settings_service import get_setting


async def _read_uri() -> str | None:
    """Read existing credentials; never initialize, rotate, or commit them."""
    async with database.async_session_factory() as db:
        secret = await get_setting(db, SETTING_TOTP_SECRET)
    return totp_provisioning_uri(secret) if secret else None


async def _run() -> int:
    try:
        uri = await _read_uri()
        if uri is None:
            print("Authentication is not initialized. Start the application once first.", file=sys.stderr)
            return 1
        print(uri, flush=True)
        return 0
    finally:
        await database.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="display the existing TOTP enrollment URI")
    args = parser.parse_args(argv)
    if not args.show:
        parser.error("--show is required to reveal the enrollment URI")
    if not sys.stdout.isatty():
        print("Enrollment requires an interactive terminal; redirected output is refused.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run())
    except Exception:
        # Database exceptions can contain connection credentials. Keep operator
        # diagnostics static, especially when the embedded DB is already open.
        print("Cannot read authentication settings. Check database access; stop the app first for Turso.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
