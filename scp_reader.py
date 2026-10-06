import asyncio
import json
import os
import re
import sys

from bs4 import BeautifulSoup
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import (
    Footer,
    Header,
    Input,
    Markdown,
    ProgressBar,
    Static,
    TabPane,
    TabbedContent,
)

INDEX_PATH = os.path.join("scp_data", "index.json")
HTML_EXPORT_FOLDER = os.path.join("scp_data", "html")
JSON_FOLDER = os.path.join("scp_data", "json")


def html_to_text(html, article_slugs=None):
    """Convert article HTML into readable terminal text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    article_links = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(
            r"(?:^|/)(SCP-\d+[\w-]*)(?:\.html)?(?:[?#].*)?$",
            anchor["href"].strip(),
            re.IGNORECASE,
        )
        if not match:
            continue
        slug = match.group(1).upper()
        if article_slugs is not None and slug not in article_slugs:
            continue
        label = anchor.get_text(" ", strip=True) or slug
        token = f"SCPINTERNALLINK{len(article_links)}END"
        article_links[token] = (label, slug)
        anchor.replace_with(token)

    for image in soup.find_all("img"):
        alt = image.get("alt", "").strip()
        image.replace_with(f"\n[Image: {alt}]\n" if alt else "\n")

    for tag in soup.find_all(["br", "hr"]):
        tag.replace_with("\n")
    for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr", "blockquote"]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    lines = []
    for line in soup.get_text().splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1]:
            lines.append("")
    text = "\n".join(lines).strip()
    text = re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", text)
    for token, (label, slug) in article_links.items():
        escaped_label = re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", label)
        text = text.replace(token, f"[{escaped_label}](scp-article:{slug})")
    return text


class CommandInput(Input):
    """Command input with history navigable using ↑ and ↓."""

    BINDINGS = [
        Binding("up", "history_previous", show=False),
        Binding("down", "history_next", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history = []
        self.history_index = -1
        self.draft = ""

    def remember(self, command):
        if command and (not self.history or self.history[-1] != command):
            self.history.append(command)
        self.history_index = -1
        self.draft = ""

    def action_history_previous(self):
        if not self.history:
            return
        if self.history_index == -1:
            self.draft = self.value
            self.history_index = len(self.history) - 1
        elif self.history_index > 0:
            self.history_index -= 1
        self.value = self.history[self.history_index]
        self.cursor_position = len(self.value)

    def action_history_next(self):
        if self.history_index == -1:
            return
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.value = self.history[self.history_index]
        else:
            self.history_index = -1
            self.value = self.draft
        self.cursor_position = len(self.value)


def scp_sort_key(slug):
    match = re.fullmatch(r"SCP-(\d+)(.*)", slug, re.IGNORECASE)
    if not match:
        return (1, float("inf"), slug.casefold(), slug.casefold())
    number, suffix = match.groups()
    return (bool(suffix), int(number), suffix.casefold(), slug.casefold())


def series_title(folder):
    match = re.fullmatch(r"series-(\d+(?:\.\d+)?)", folder, re.IGNORECASE)
    if match:
        return f"SCP Series {match.group(1)}"
    return {
        "scp-001": "SCP-001 Proposals",
        "decommissioned": "Decommissioned SCPs",
        "explained": "Explained SCPs",
        "international": "International SCPs",
        "joke": "Joke SCPs",
    }.get(folder.casefold(), folder.replace("-", " ").title())


def series_sort_key(folder):
    match = re.fullmatch(r"series-(\d+(?:\.\d+)?)", folder, re.IGNORECASE)
    if match:
        return (0, float(match.group(1)), folder.casefold())
    return (1, float("inf"), series_title(folder).casefold())


class SCPReader(App):
    TITLE = "SCP Terminal"
    SUB_TITLE = "SCP-OS"

    BINDINGS = [Binding("ctrl+c", "close_active_tab", "Close tab", show=True)]

    CSS = """
    Screen {
        background: #050805;
        color: #33ff33;
    }
    Header, Footer {
        background: #0b190b;
        color: #58ff58;
    }
    #output {
        height: 1fr;
        border: round #1f7a1f;
        margin: 0 1;
        padding: 0 1;
        scrollbar-color: #1f7a1f;
    }
    #output > Static, #output > Markdown {
        width: 1fr;
        margin-bottom: 1;
    }
    #terminal-tab {
        height: 1fr;
    }
    .article-scroll {
        height: 1fr;
        border: round #1f7a1f;
        margin: 1;
        padding: 1 2;
    }
    .article-body {
        color: #b6ffb6;
    }
    CommandInput {
        margin: 0 1 1 1;
        border: round #1f7a1f;
    }
    """

    def __init__(self):
        super().__init__()
        self.index_error = None
        self.index = self.load_index()
        self.article_pane_ids = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(id="workspace-tabs"):
            with TabPane("Terminal", id="terminal-tab"):
                yield VerticalScroll(id="output")
                yield Static(id="update-status")
                yield ProgressBar(
                    total=100,
                    show_eta=False,
                    show_percentage=True,
                    id="update-progress",
                )
                yield CommandInput(
                    placeholder="Type help to see available commands",
                    id="command",
                )
        yield Footer()

    def on_mount(self):
        self.query_one("#update-status", Static).display = False
        self.query_one("#update-progress", ProgressBar).display = False
        if self.index:
            self.write_output(
                f"Welcome to the SCP. {len(self.index)} articles indexed. "
                "Type 'help' to get started."
            )
        else:
            self.write_output(
                "Welcome to the SCP. scp_data/index.json was not found. "
                "run scp_loader.py first."
            )
        if self.index_error:
            self.write_output(f"[index.json] Error: {self.index_error}")
        self.query_one("#command", CommandInput).focus()

    def load_index(self):
        if not os.path.exists(INDEX_PATH):
            return None
        try:
            with open(INDEX_PATH, encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            self.index_error = error
            return None

    def write_output(self, text):
        output = self.query_one("#output", VerticalScroll)
        output.mount(Static(text, markup=False))
        output.scroll_end(animate=False)

    def list_scps(self):
        if self.index is not None:
            articles = [
                (slug, entry.get("title") or slug, entry.get("folder", "misc"))
                for slug, entry in self.index.items()
            ]
        else:
            articles = []
            for root, _, files in os.walk("scp_data"):
                for filename in files:
                    if not filename.endswith(".json"):
                        continue
                    path = os.path.join(root, filename)
                    try:
                        with open(path, encoding="utf-8") as file:
                            data = json.load(file)
                        series = os.path.splitext(filename)[0].removeprefix("content_")
                        articles.extend(
                            (slug, entry.get("title") or slug, series)
                            for slug, entry in data.items()
                            if slug.startswith("SCP-")
                        )
                    except (OSError, json.JSONDecodeError) as error:
                        self.write_output(f"[{filename}] Error: {error}")

        if not articles:
            self.write_output("No SCP articles were found.")
            return
        articles.sort(key=lambda article: scp_sort_key(article[0]))
        grouped_articles = {}
        for slug, title, series in articles:
            grouped_articles.setdefault(series, []).append((slug, title))

        output = self.query_one("#output", VerticalScroll)
        for series in sorted(grouped_articles, key=series_sort_key):
            links = []
            for slug, title in grouped_articles[series]:
                label = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
                links.append(f"[{label}](scp-article:{slug})")
            article_lines = [
                "  ".join(links[start:start + 10])
                for start in range(0, len(links), 10)
            ]
            markdown = f"## {series_title(series)}\n\n" + "  \n".join(article_lines)
            output.mount(Markdown(markdown, open_links=False))
        output.scroll_end(animate=False)

    def find_article(self, slug):
        slug = slug.upper()
        if self.index is not None:
            meta = self.index.get(slug)
            if not meta:
                return None

            html_path = os.path.join(
                HTML_EXPORT_FOLDER, meta["folder"], f"{slug}.html"
            )
            if os.path.exists(html_path):
                try:
                    with open(html_path, encoding="utf-8") as file:
                        return meta["title"], html_to_text(
                            file.read(), self.index.keys()
                        )
                except OSError as error:
                    self.write_output(f"Error reading {html_path}: {error}")
                    return None

            json_path = os.path.join(JSON_FOLDER, meta["json_file"])
            try:
                with open(json_path, encoding="utf-8") as file:
                    entry = json.load(file)[slug]
            except (OSError, json.JSONDecodeError, KeyError) as error:
                self.write_output(f"Error reading {meta['json_file']}: {error}")
                return None
            html = entry.get("raw_content") or entry.get("raw_source", "")
            return meta["title"], html_to_text(html, self.index.keys())

        for root, _, files in os.walk("scp_data"):
            for filename in files:
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, encoding="utf-8") as file:
                        data = json.load(file)
                    if slug in data:
                        entry = data[slug]
                        html = entry.get("raw_content") or entry.get("raw_source", "")
                        return entry.get("title", slug), html_to_text(html)
                except (OSError, json.JSONDecodeError) as error:
                    self.write_output(f"[{filename}] Error: {error}")
        return None

    async def show_scp(self, slug):
        slug = slug.upper()
        article = self.find_article(slug)
        if article is None:
            self.write_output("SCP article not found.")
            return
        title, body = article
        tabs = self.query_one("#workspace-tabs", TabbedContent)
        pane_id = self.article_pane_ids.get(slug)
        if pane_id is None:
            pane_id = f"article-{len(self.article_pane_ids) + 1}"
            article_view = VerticalScroll(
                Static(
                    f"{slug} - {title}",
                    classes="article-body",
                    markup=False,
                ),
                Markdown(body, open_links=False, classes="article-body"),
                classes="article-scroll",
            )
            await tabs.add_pane(TabPane(title, article_view, id=pane_id))
            self.article_pane_ids[slug] = pane_id
        self.set_focus(None)
        tabs.active = pane_id

    async def on_input_submitted(self, event: Input.Submitted):
        command = event.value.strip()
        event.input.value = ""
        event.input.remember(command)
        if command:
            self.write_output(f"> {command}")
        await self.handle_command(command)

    async def handle_command(self, command):
        normalized = command.lower()
        if normalized in ("exit", "quit"):
            self.exit()
        elif normalized == "cls":
            await self.query_one("#output", VerticalScroll).remove_children()
        elif normalized == "help":
            self.write_output(
                "Commands:\n"
                "  SCP-###  - open an article\n"
                "  ###      - open an article by number\n"
                "  list     - show series and clickable article titles\n"
                "  update   - run the loader and refresh the archive index\n"
                "  cls      - clear the terminal history\n"
                "  help     - show this help\n"
                "  exit     - quit\n\n"
                "Use the tabs to switch between the terminal and open articles. "
                "Press Ctrl+C to close an article tab or quit from Terminal."
            )
        elif normalized == "list":
            self.list_scps()
        elif normalized == "update":
            await self.update_archive()
        elif command.upper().startswith("SCP-"):
            await self.show_scp(command)
        elif command.isdigit() and 1 <= int(command) <= 9999:
            await self.show_scp(f"SCP-{int(command):03}")
        elif command:
            self.write_output("Unknown command. Type 'help'.")

    async def update_archive(self):
        loader_path = os.path.join(os.path.dirname(__file__), "scp_loader.py")
        self.write_output("Starting SCP archive update...")
        status = self.query_one("#update-status", Static)
        progress = self.query_one("#update-progress", ProgressBar)
        status.display = True
        progress.display = True
        status.update("Starting loader...")
        progress.update(progress=0)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                loader_path,
                "--textual-progress",
                cwd=os.path.dirname(__file__),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            status.display = False
            progress.display = False
            self.write_output(f"Could not start scp_loader.py: {error}")
            return

        if process.stdout is not None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                output = line.decode(errors="replace").strip()
                if output:
                    self.handle_loader_output(output)

        return_code = await process.wait()
        if return_code:
            status.display = False
            progress.display = False
            self.write_output(f"Archive update failed (exit code {return_code}).")
            return

        self.index_error = None
        self.index = self.load_index()
        if self.index is None:
            status.display = False
            progress.display = False
            error = f": {self.index_error}" if self.index_error else ""
            self.write_output(f"Loader finished, but the article index is unavailable{error}.")
            return
        status.display = False
        progress.display = False
        self.write_output(f"Archive updated. {len(self.index)} articles indexed.")

    def handle_loader_output(self, output):
        try:
            event = json.loads(output)
        except json.JSONDecodeError:
            self.write_output(output)
            return
        if not isinstance(event, dict) or event.get("event") != "progress":
            self.write_output(output)
            return

        completed = event.get("completed")
        total = event.get("total")
        stage = event.get("stage")
        if not isinstance(completed, int) or not isinstance(total, int) or not isinstance(stage, str):
            self.write_output(output)
            return

        percent = completed * 100 / total if total > 0 else 100
        self.query_one("#update-status", Static).update(
            f"{stage}: {completed}/{total}"
        )
        self.query_one("#update-progress", ProgressBar).update(progress=percent)

    async def action_close_active_tab(self):
        tabs = self.query_one("#workspace-tabs", TabbedContent)
        pane_id = tabs.active
        if pane_id == "terminal-tab":
            self.exit()
            return

        await tabs.remove_pane(pane_id)
        for slug, article_pane_id in list(self.article_pane_ids.items()):
            if article_pane_id == pane_id:
                del self.article_pane_ids[slug]
                break
        self.query_one("#command", CommandInput).focus()

    async def on_markdown_link_clicked(self, event: Markdown.LinkClicked):
        if event.href.startswith("scp-article:"):
            await self.show_scp(event.href.removeprefix("scp-article:"))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        if (
            event.tabbed_content.id == "workspace-tabs"
            and event.tabbed_content.active == "terminal-tab"
        ):
            self.query_one("#command", CommandInput).focus()


if __name__ == "__main__":
    SCPReader().run()
