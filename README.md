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
- Refreshes `content_scp-001.json` from the official SCP-001 index page and each proposal linked from it, so that the local archive includes all current proposals.
- Parses each article's HTML with BeautifulSoup.
- Uses each record's API `link` value as its local article identity and builds canonical `https://scp-wiki.wikidot.com/` URLs from it; raw downloaded API JSON is left unchanged.
- Reads complete SCP titles from the official main-series listing pages 1–10, matching articles by their SCP identifier.
- Replaces `SCP-###` references with internal links to the corresponding article.
- With `--media`, downloads referenced images in parallel (4 workers) and saves them locally, updating their `src` attributes. Image downloads are skipped by default.
- Saves everything under `scp_data/` using this structure:

```
scp_data/
├── json/       # Original downloaded JSON files
├── html/       # Processed HTML, organized by series
│   └── series-1/
│       └── scp-173.html
└── images/     # Downloaded images
```

The generated `scp_data/index.json` is keyed by the API `link` values. Each entry also stores the SCP identifier used by reader commands, the resolved title, source JSON filename, and generated HTML filename. Local HTML filenames are derived safely from the API link. The reader continues to accept SCP identifiers such as `SCP-173` or `173`.

SCP-001 pages are read from their rendered Wikidot HTML because the dataset API only includes one proposal. Their records use the same JSON fields as the API articles; unavailable source markup and revision history are left empty.

If a series listing has no title for an article, the loader keeps the API title without reporting each missing title.

### Reader (`main.py` / `scp_reader.py`)
A Textual TUI with a retro terminal look (dark background, green text, and keyboard navigation) for searching and reading the archive created by `scp_loader.py`. Article references open in reader tabs.

**Available commands:**

| Command | Action |
|---|---|
| `SCP-173` or `173` | Opens an article in its own scrollable tab |
| `list` | Shows clickable series titles and article counts |
| `list <series>` | Shows that series' article IDs and clickable titles, one per line |
| `update` | Runs `scp_loader.py` without downloading images, shows live progress, and reloads the article index when it finishes |
| `update --media` | Also downloads article images during the update |
| `cls` | Clears the terminal history |
| `help` | Shows the help message |
| `exit` / `quit` | Closes the TUI |

The Terminal tab keeps command history and clickable article lists. Clicking a series or article echoes and runs the equivalent command in the terminal (for example, `list joke` or `scp-012`). Long series lists and article text are rendered in small batches so the interface remains responsive. Entering a number shared by multiple article variants opens a chooser, with the regular-series article selected by default. The SCP-001 index is listed in Series 1; individual proposals remain archived but are omitted from the series overview. Opened articles appear in separate tabs; switch back to Terminal without losing its history. Reopening an article selects its existing tab. Press `Ctrl+C` to close the active article tab; from the Terminal tab, `Ctrl+C` quits the TUI. Images and the original HTML layout are not rendered in the terminal.

---

## Requirements

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Start the TUI reader; it downloads the archive automatically if needed.
python main.py
```

Run the loader directly with `python scp_loader.py --media` to include image downloads.

---

## Current notes / limitations

- Image downloads are opt-in with `update --media` (or `python scp_loader.py --media`); they retry temporary network, rate-limit, and server errors, and permanently unavailable images are logged and retried on a later media update.
- If `scp_data/index.json` is missing, the reader automatically runs the loader when it starts.
- The loader stores internal SCP references in article HTML; the reader makes references to locally indexed articles clickable.
