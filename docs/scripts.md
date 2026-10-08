# Scripts

## `main.py`

This is the application entry point. It creates and runs `SCPReader` from
`reader.py`; the reader's interface and behavior are implemented in that
module.

```powershell
python main.py
```

## `loader.py`

Downloads and prepares the local archive:

1. Fetches `content_index.json` from
   `https://scp-data.tedivm.com/data/scp/items/` to determine which data files
   to download and which series or group each file belongs to.
2. Downloads the series and special-group JSON files into `data/api/`. It
   sends saved `ETag` and `Last-Modified` validators with conditional requests,
   so unchanged files return `304 Not Modified` without transferring their
   contents. If the server does not provide validators, it falls back to
   comparing the downloaded JSON with the local copy.
3. Fetches the official series pages to fill in article titles.
4. Builds `data/index.json` and generates a local HTML file for each
   article from `raw_content` (or `raw_source` if `raw_content` is missing).
   The generated HTML links to other articles available locally.
5. Rebuilds the SCP-001 records from the official index and its linked
   proposals on Wikidot, rather than relying on the API's SCP-001 group being
   complete.
6. If media downloads are enabled, downloads images and makes the local HTML
   use them. Images are not downloaded otherwise.

The article-processing progress uses fixed series milestones: each full series
advances the bar by 10 percentage points (half-series by 5), instead of
recalculating its total as new JSON files are downloaded.

It also exposes `update_archive(download_media=False)` for use by other
modules. When run directly, it supports:

```powershell
python loader.py
python loader.py --media
```

## `workers.py`

This small wrapper runs `update_archive` in a process separate from the
reader. It accepts `--media` and propagates the loader's exit code. The reader
starts it as a subprocess so the interface remains responsive during archive
updates; the loader emits progress events for the interface to display.

It can also be run directly:

```powershell
python workers.py
python workers.py --media
```

## `reader.py`

Implements the Textual-based TUI. It reads `data/index.json` to resolve
articles, then loads the processed HTML. If the HTML is unavailable, it tries
to read the content from the source JSON. It can open articles, list series,
navigate tabs, and update the archive using the worker.

Available reader commands: `SCP-173` or `173`, `list`, `list <series>`,
`update`, `update --media`, `help`, `cls`, and `exit`/`quit`. If the local
index does not exist at startup, the reader begins an update.

## Tests

`tests/loader.py` contains tests for loader functions and reader behavior.
`tests/test_api_json_storage.py` verifies API validators and fixed
series-based processing progress.
