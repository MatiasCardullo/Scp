# Scripts

## `main.py`

This is the application entry point. It creates and runs `SCPReader` from
`scp_reader.py`; the reader's interface and behavior are implemented in that
module.

```powershell
python main.py
```

## `scp_loader.py`

Downloads and prepares the local archive:

1. Fetches `content_index.json` from
   `https://scp-data.tedivm.com/data/scp/items/` to determine which data files
   to download and which series or group each file belongs to.
2. Downloads the series and special-group JSON files. It compares them with
   existing files and avoids reprocessing unchanged content.
3. Fetches the official series pages to fill in article titles.
4. Builds `scp_data/index.json` and generates a local HTML file for each
   article from `raw_content` (or `raw_source` if `raw_content` is missing).
   The generated HTML links to other articles available locally.
5. Rebuilds the SCP-001 records from the official index and its linked
   proposals on Wikidot, rather than relying on the API's SCP-001 group being
   complete.
6. If media downloads are enabled, downloads images and makes the local HTML
   use them. Images are not downloaded otherwise.

It also exposes `update_archive(download_media=False)` for use by other
modules. When run directly, it supports:

```powershell
python scp_loader.py
python scp_loader.py --media
```

## `scp_loader_worker.py`

This small wrapper runs `update_archive` in a process separate from the
reader. It accepts `--media` and propagates the loader's exit code. The reader
starts it as a subprocess so the interface remains responsive during archive
updates; the loader emits progress events for the interface to display.

It can also be run directly:

```powershell
python scp_loader_worker.py
python scp_loader_worker.py --media
```

## `scp_reader.py`

Implements the Textual-based TUI. It reads `scp_data/index.json` to resolve
articles, then loads the processed HTML. If the HTML is unavailable, it tries
to read the content from the source JSON. It can open articles, list series,
navigate tabs, and update the archive using the worker.

Available reader commands: `SCP-173` or `173`, `list`, `list <series>`,
`update`, `update --media`, `help`, `cls`, and `exit`/`quit`. If the local
index does not exist at startup, the reader begins an update.

## Tests

`tests/test_scp_loader.py` contains tests for loader functions and reader
behavior, including reference normalization, series titles, SCP-001, and
collapsible sections.
