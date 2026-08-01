# RSSRipple E2E Tests (Midscene.js)

AI-driven end-to-end UI tests powered by [Midscene.js](https://midscenejs.com/).
Each YAML suite drives a real browser against a running RSSRipple instance and
uses a multimodal LLM to locate elements and assert page states.

## Suites

| File | Scope |
|------|-------|
| `dashboard.yaml` | Dashboard stats/cards, sidebar navigation, sidebar collapse |
| `settings.yaml` | System Settings page (read-only: LLM API card, data sources) |
| `channels-lifecycle.yaml` | Channel CRUD: create (validate + preview), detail, fetch, edit, delete |
| `downloaders-lifecycle.yaml` | Downloader CRUD using the deterministic **Mock** downloader type |
| `agents-lifecycle.yaml` | Agent CRUD + all detail tabs (works/tasks/decisions/filter/run); creates its own channel + Mock downloader prerequisites |
| `resources-metadata.yaml` | Channel resources tabs, resource detail drawer, metadata correction modal |
| `works-repository.yaml` | Unified `/works` Repository page (filter/search/detail) + legacy `/series` & `/movies` redirects |

Every suite is self-contained: it creates the entities it needs and deletes them
at the end. Suites run sequentially (`concurrent: 1`).

## Model configuration

The Midscene CLI reads model credentials from `.env` in the **current working
directory**. Copy `.env.example` to `.env` and fill in your key (a real `.env`
already exists locally but is gitignored):

```bash
MIDSCENE_MODEL_BASE_URL="https://api.kimi.com/coding/v1"  # OpenAI-compatible endpoint
MIDSCENE_MODEL_API_KEY="sk-..."
MIDSCENE_MODEL_NAME="k3"
MIDSCENE_MODEL_FAMILY="kimi3"   # Kimi K3 series — see https://midscenejs.com/model-common-config
```

Midscene requires a **multimodal (vision)** model for UI grounding. Verify
connectivity before a full run:

```bash
cd tests/midscene
npx @midscene/cli@latest model verify
```

## Running

```bash
# 1. Start RSSRipple (backend serves the built frontend on :9001)
docker compose up -d app        # or: uv run uvicorn app.main:app --port 9001

# 2. Run all suites
cd tests/midscene
npx @midscene/cli@latest --config midscene-config.yaml

# Or a single suite
npx @midscene/cli@latest ./dashboard.yaml
```

Environment overrides:

- `RSSRIPPLE_URL` — target instance (default `http://localhost:9001`)
- `RSSRIPPLE_TEST_FEED_URL` — live RSS feed used by channel/resource suites
  (default `https://rss.art19.com/apology-line`); must be reachable from the
  machine running the tests. Channel validation and manual fetch need outbound
  network access from the RSSRipple **server**.

Reports (HTML, with screenshots per step) are written to `midscene_run/report/`
after each run.

## Writing / maintaining suites

- The UI defaults to **zh-CN**; every suite's first task switches the sidebar
  language to English, and prompts then use the English labels. If you add a
  suite, keep that first task.
- Many list-page action buttons are icon-only with tooltips (e.g. "Fetch Now",
  "Delete") — reference them by tooltip/position in prompts.
- Success signals are usually antd toast messages; navigations after create/save
  are called out in each suite.
- Saving an agent may pop the "Newly matching resources" backfill modal —
  always handle it (the agent suite uses "Save without backfill").
- See `tests/web-ui-functional-cases.md` for the manual case checklist these
  suites are derived from.
