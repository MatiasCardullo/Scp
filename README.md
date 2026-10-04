# SCP Terminal Archive

A two-part tool for downloading, cataloging, and reading articles from the [Wikidot SCP Foundation](http://www.scpwiki.com/) locally, with a retro terminal-style interface.

```
📥 scp_loader.py   →  downloads and builds the local archive (JSON + HTML + images)
🖥️  scp_reader.py   →  retro terminal for searching and reading downloaded articles
```

---

## What each script does

### `scp_loader.py`
Downloads the public dataset from [scp-data.tedivm.com](https://scp-data.tedivm.com/) and creates a browsable local archive:

- Downloads the content index and all article `.json` files (organized by series/folder).
- Parses each article's HTML with BeautifulSoup.
- Replaces `SCP-###` references with internal links to the corresponding article.
- Downloads referenced images in parallel (4 workers) and saves them locally, updating their `src` attributes.
- Saves everything under `scp_data/` using this structure:

```
scp_data/
├── json/       # Original downloaded JSON files
├── html/       # Processed HTML, organized by series
│   └── series-1/
│       └── scp-173.html
└── images/     # Downloaded images
```

### `scp_reader.py`
A Textual TUI with a retro terminal look (dark background, green text, and keyboard navigation) for searching and reading the archive created by `scp_loader.py`.

**Available commands:**

| Command | Action |
|---|---|
| `SCP-173` or `173` | Opens an article in a scrollable terminal view |
| `list` | Shows articles in a clickable grid, with numbered SCPs first and special entries at the end |
| `help` | Shows the help message |
| `exit` / `quit` | Closes the TUI |

Click an article in the grid to open it. Press `Esc` in an article or the list to return to the previous screen. Articles are displayed as text; images and the original HTML layout are not rendered in the terminal.

---

## Requirements

```bash
pip install -r requirements.txt
```

## Usage

```bash
# 1. Download and build the local archive (may take several minutes)
python scp_loader.py

# 2. Start the TUI reader
python scp_reader.py
```

---

## Current notes / limitations

- `scp_loader.py` does not automatically retry network failures; if a download fails, that file is skipped and processing continues.
- If `scp_data/index.json` is missing, `scp_reader.py` searches for articles by walking the `.json` files under `scp_data/`; searches are direct when the index is available.
- Internal links between SCPs are created during downloading, not when reading.
