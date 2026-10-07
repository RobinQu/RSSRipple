# CORS browser regression

Run only against the fixture below: the script uses a fixed synthetic TOTP secret.
Use the project Python environment, Node.js with Playwright installed, and its Chromium.
No project dependency upgrade or browser download is performed by this test.

1. From the repository root, start `PYTHONPATH=. .venv/bin/python tests/browser/cors_fixture_server.py`.
2. With ports 32901 and 32902 free, run `TEST_PYTHON="$PWD/.venv/bin/python" node tests/browser/cors_policy.cjs`.
3. Stop the Python fixture with SIGINT. Its temporary database and posters are deleted on exit.

If Playwright is installed outside the project, set `NODE_PATH` to its node_modules directory.
`BROWSER_EXECUTABLE` optionally selects an existing Chromium binary; `BROWSER_REPORT` chooses
an output JSON path (default `/tmp/rssripple-cors-browser.json`). Browser and source-page
servers are closed in finally, including assertion failures. The Python fixture disables
application lifespan so scheduler/background jobs cannot start.

Assertions cover production OTP login, same-site allowed reads, rejected same-site and
cross-site reads, rejected same-site logout preserving authentication, and allowed logout.
Recorded `cookieSent` only reflects headers exposed by Playwright; blocked requests may
hide those headers, so false is not proof that no Cookie was transmitted.
