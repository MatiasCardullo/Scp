# SCP Terminal Archive

A two-part tool for downloading, cataloging, and reading articles from the [Wikidot SCP Foundation](http://www.scpwiki.com/) locally, with a retro terminal-style interface.

```
📥 scp_loader.py   →  downloads and builds the local archive (JSON + HTML + images)
🖥️  main.py         →  retro terminal for searching and reading downloaded articles
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

### Reader (`main.py` / `scp_reader.py`)
A Textual TUI with a retro terminal look (dark background, green text, and keyboard navigation) for searching and reading the archive created by `scp_loader.py`. Article references open in reader tabs.

**Available commands:**

| Command | Action |
|---|---|
| `SCP-173` or `173` | Opens an article in its own scrollable tab |
| `list` | Adds clickable, title-only article links to the terminal history, grouped under series titles |
| `update` | Runs `scp_loader.py`, shows live progress in a single status bar, and reloads the article index when it finishes |
| `cls` | Clears the terminal history |
| `help` | Shows the help message |
| `exit` / `quit` | Closes the TUI |

The Terminal tab keeps command history and clickable article lists. Opened articles appear in separate tabs; switch back to Terminal without losing its history. Reopening an article selects its existing tab. Press `Ctrl+C` to close the active article tab; from the Terminal tab, `Ctrl+C` quits the TUI. Images and the original HTML layout are not rendered in the terminal.

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
python main.py
```

`python scp_reader.py` remains available as an alternative way to start the reader. Run `update` from the reader to execute the loader and refresh the in-memory index. Its progress updates in place instead of filling the terminal history. The loader retains its existing behavior of reusing JSON files that are already present; run it directly to use the regular `tqdm` progress bars.

---

## Current notes / limitations

- `scp_loader.py` does not automatically retry network failures; if a download fails, that file is skipped and processing continues.
- If `scp_data/index.json` is missing, the reader searches for articles by walking the `.json` files under `scp_data/`; searches are direct when the index is available.
- The loader stores internal SCP references in article HTML; the reader makes references to locally indexed articles clickable.
