# RSSRipple Web UI Functional Cases

## Dashboard

1. Open `/`.
2. Assert the Dashboard heading is visible.
3. Assert active Agent, active channel, downloading, and pending decision summary cards are visible.
4. Assert shortcut buttons for adding a channel and adding an Agent are visible.

## Channel Create, Preview, Fetch, And Resources

1. Open `/channels/new`.
2. Enter a channel name and one supported RSS URL, for example `https://dmhy.org/topics/rss/rss.xml`.
3. Turn off automatic metadata search and choose no title cleaning for a non-LLM smoke run.
4. Assert the field mapping JSON textarea is visible and prefilled.
5. Click validate.
6. Assert a valid RSS message appears and the AI analysis button does not start loading when field mapping JSON is already present.
7. Click preview.
8. Assert the preview panel shows entries and parsed fields.
9. Submit the form.
10. Assert the channel list contains the new channel with local matching metadata mode.
11. Click manual fetch for the new channel.
12. Open the channel detail page.
13. Assert the resource total is positive, the resource table renders, and pagination limits the current page to the configured page size.
14. Click a resource row.
15. Assert the resource detail drawer opens with metadata correction actions.

## Downloaders

1. Open `/downloaders`.
2. Assert the downloader list table and add button are visible.
3. Open the add downloader form.
4. Enter a Transmission RPC URL, for example `http://transmission:9091/transmission/rpc`, and default download directory `/downloads`.
5. Submit the form.
6. Assert the downloader detail page opens.
7. Click test connection.
8. Assert the page remains usable and the Transmission torrent list can be refreshed.

## Agents

1. Open `/agents`.
2. Assert the Agent list table and new Agent button are visible.
3. Assert each row shows channel, downloader, status, scope, conflict handling, LLM state, and run/delete actions.

## Works Repository

1. Open `/works`.
2. Assert the repository heading, all/tv/movie segmented control, and search box are visible.
3. Enter a known title fragment.
4. Assert matching work cards remain visible.

## Metadata Eval Labeling Tool

Base URL: `http://localhost:9002`.

### Eval Smoke Load

1. Open `/`.
2. Assert the `Metadata Eval` heading is visible.
3. Assert feed checkboxes for `mikanani`, `kisssub`, `eztv`, and `dmhy` are visible.
4. Assert `Load Titles`, `Run All`, `Run Selected`, dataset selector, `New`, and `Delete` controls are visible after initial title load.
5. Assert at least one title card is rendered.
6. Assert stats for Total, Pending, Draft, Accepted, Skipped, TMDB, Exa, and Wiki are visible.

### Dataset Create And Search Method Selection

1. Open the eval tool.
2. Click `New`.
3. Assert the `New Dataset` modal opens.
4. Assert `Metadata Search Method` defaults to `Exo Agent Search`.
5. Select `TMDB Search`.
6. Click `Create`.
7. Assert the dataset selector changes to a dataset name beginning with `tmdb-eval-`.
8. Click `New` again.
9. Leave `Exo Agent Search` selected and click `Create`.
10. Assert the dataset selector changes to a dataset name beginning with `exa-eval-`.

### Dataset Switch Auto Save

1. Open the eval tool and create or select a dataset.
2. Edit the first title card.
3. Change `Clean Title` to a recognizable marker value.
4. Click `Save Changes`.
5. Select another dataset from the dataset selector.
6. Select the original dataset again.
7. Assert the edited card remains in `Draft` status and the marker value is retained.

### Label Edit, Accept, And Skip

1. Open the eval tool with loaded titles.
2. Click `Edit` on a pending or draft card.
3. Assert the edit modal opens with Raw Title, Clean Title, Content Type, Episode, Season, Title CN, and Title EN fields.
4. Fill `Clean Title`, choose a content type, and click `Save Changes`.
5. Assert the card status becomes `Draft`.
6. Click `Accept`.
7. Assert the card status becomes `Accepted`.
8. Click `Un-accept`.
9. Assert the card returns to `Draft`.
10. Click `Skip`.
11. Assert the card status becomes `Skipped`.

### Selection And Bulk Controls

1. Open the eval tool with loaded titles.
2. Select the checkbox on the first card.
3. Assert the selection hint shows `1 selected` and `Run Selected` is enabled.
4. Click `Select All`.
5. Assert all visible cards are selected and the button changes to `Deselect All`.
6. Click `Deselect All`.
7. Assert no card is selected and `Run Selected` is disabled.

### Dataset Delete

1. Create a dataset whose name starts with `exa-eval-` or `tmdb-eval-`.
2. Click `Delete`.
3. Confirm the browser dialog.
4. Assert the dataset selector no longer contains the deleted dataset.
5. Assert another dataset is selected or a fresh default dataset is created.

## UI Execution Record

Record each manual or automated UI validation run with:

- Date/time
- Browser or tool
- Base URL
- Test cases executed
- Result: pass/fail/blocked
- Evidence: screenshots, console/network errors, or relevant command output
- Notes and follow-up defects
