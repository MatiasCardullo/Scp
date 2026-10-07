import asyncio
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Markdown,
    OptionList,
    ProgressBar,
    Static,
    TabPane,
    TabbedContent,
)
from textual.widgets.option_list import Option

DATA_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scp_data")
INDEX_PATH = os.path.join(DATA_FOLDER, "index.json")
UPDATE_LOG_PATH = os.path.join(DATA_FOLDER, "update.log")
HTML_EXPORT_FOLDER = os.path.join(DATA_FOLDER, "html")
JSON_FOLDER = os.path.join(DATA_FOLDER, "json")


def normalize_article_reference(reference):
    path = urlparse(reference).path if "://" in reference else reference
    path = unquote(path).replace("\\", "/").rsplit("/", 1)[-1]
    if path.lower().endswith(".html"):
        path = path[:-5]
    return path.strip("/").casefold()


def article_reference_map(index):
    references = {}
    for identity, metadata in index.items():
        references[normalize_article_reference(identity)] = identity
        if metadata.get("link"):
            references[normalize_article_reference(metadata["link"])] = identity
        if metadata.get("url"):
            references[normalize_article_reference(metadata["url"])] = identity
        article_id = metadata.get("scp_id")
        if article_id:
            references[article_id.casefold()] = identity
        html_file = metadata.get("html_file")
        if html_file:
            references[normalize_article_reference(html_file)] = identity
    return references


def html_to_text(html, article_references=None):
    """Convert article HTML into readable terminal text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    article_links = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        article_identity = None
        match = re.search(
            r"(?:^|/)(SCP-\d+[\w-]*)(?:\.html)?(?:[?#].*)?$",
            href,
            re.IGNORECASE,
        )
        if match:
            if article_references is None:
                article_identity = match.group(1).upper()
            else:
                article_identity = article_references.get(match.group(1).casefold())
        if article_identity is None and article_references is not None:
            article_identity = article_references.get(normalize_article_reference(href))
        if article_identity is None:
            continue
        label = anchor.get_text(" ", strip=True) or article_identity
        token = f"SCPINTERNALLINK{len(article_links)}END"
        article_links[token] = (label, article_identity)
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
    for token, (label, identity) in article_links.items():
        escaped_label = re.sub(r"([\\`*_{}\[\]()#+\-.!|>])", r"\\\1", label)
        text = text.replace(
            token,
            f"[{escaped_label}](scp-article:{quote(identity, safe=':/_-')})",
        )
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


def scp_sort_key(article_id):
    match = re.fullmatch(r"SCP-(\d+)(.*)", article_id, re.IGNORECASE)
    if not match:
        return (1, float("inf"), article_id.casefold(), article_id.casefold())
    number, suffix = match.groups()
    return (bool(suffix), int(number), suffix.casefold(), article_id.casefold())


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


class ArticleChoiceScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    CSS = """
    ArticleChoiceScreen {
        align: center middle;
    }
    #article-choice-dialog {
        width: 70%;
        max-width: 90;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round #33ff33;
        background: #050805;
    }
    #article-choice-title {
        height: auto;
        margin-bottom: 1;
    }
    #article-choice-options {
        height: auto;
        max-height: 16;
    }
    """

    def __init__(self, choices):
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="article-choice-dialog"):
            yield Static(
                "¿A cuál artículo te refieres? (Enter para abrir, Esc para cancelar)",
                id="article-choice-title",
            )
            yield OptionList(
                *[
                    Option(
                        f"{article_id} - {title} [{series_title(folder)}]",
                        id=identity,
                    )
                    for article_id, title, folder, identity in self.choices
                ],
                id="article-choice-options",
            )

    def action_cancel(self):
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


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
    #update-panel {
        height: 2;
        width: 1fr;
        margin: 0 1;
        padding: 0 1;
    }
    .update-row {
        height: 1;
        layout: horizontal;
    }
    .update-status {
        width: auto;
        height: 1;
    }
    .update-progress {
        width: 1fr;
        height: 1;
        margin-left: 1;
    }
    .process-article-id {
        width: auto;
        max-width: 24;
        height: 1;
        margin-left: 1;
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
                with Vertical(id="update-panel"):
                    with Horizontal(classes="update-row"):
                        yield Static(
                            "Downloading series",
                            id="download-status",
                            classes="update-status",
                        )
                        yield ProgressBar(
                            total=100,
                            show_eta=False,
                            show_percentage=True,
                            id="download-progress",
                            classes="update-progress",
                        )
                    with Horizontal(classes="update-row"):
                        yield Static(
                            "Processing articles",
                            id="process-status",
                            classes="update-status",
                        )
                        yield ProgressBar(
                            total=100,
                            show_eta=False,
                            show_percentage=True,
                            id="process-progress",
                            classes="update-progress",
                        )
                        yield Static(
                            "",
                            id="process-article-id",
                            classes="process-article-id",
                        )
                yield CommandInput(
                    placeholder="Type help to see available commands",
                    id="command",
                )
        yield Footer()

    async def on_mount(self):
        self.query_one("#update-panel", Vertical).display = False
        if not os.path.exists(INDEX_PATH):
            self.write_output(
                "Welcome to the SCP. Type 'help' to get started."
            )
        elif self.index is not None:
            self.write_output(
                f"Welcome to the SCP. {len(self.index)} articles indexed. "
                "Type 'help' to get started."
            )
        if self.index_error:
            self.write_output(f"[index.json] Error: {self.index_error}")
        self.query_one("#command", CommandInput).focus()
        if not os.path.exists(INDEX_PATH):
            self.call_after_refresh(self.startup_update)

    def startup_update(self):
        self.prepare_command("update")
        self.run_worker(
            self.handle_command("update"),
            name="startup-update",
            group="archive-update",
        )

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

    async def mount_markdown_incrementally(
        self, container, text, classes=None, lines_per_batch=25
    ):
        lines = text.splitlines()
        for start in range(0, len(lines), lines_per_batch):
            markdown = Markdown(
                "\n".join(lines[start:start + lines_per_batch]),
                open_links=False,
                classes=classes,
            )
            await container.mount(markdown)
            container.scroll_end(animate=False)
            await asyncio.sleep(0.01)

    async def list_scps(self, requested_series=None):
        if self.index is not None:
            articles = [
                (
                    entry.get("scp_id", identity),
                    entry.get("title") or entry.get("scp_id", identity),
                    entry.get("folder", "misc"),
                    identity,
                )
                for identity, entry in self.index.items()
            ]
        else:
            articles = []
            for root, _, files in os.walk(DATA_FOLDER):
                for filename in files:
                    if not filename.endswith(".json"):
                        continue
                    path = os.path.join(root, filename)
                    try:
                        with open(path, encoding="utf-8") as file:
                            data = json.load(file)
                        series = os.path.splitext(filename)[0].removeprefix("content_")
                        for article_id, entry in data.items():
                            article_series = series
                            if article_series.casefold() == "scp-001":
                                if article_id.casefold() != "scp-001":
                                    continue
                                article_id = entry.get("scp", article_id)
                                article_series = entry.get("series", "series-1")
                            if entry.get("scp") or article_id.startswith("SCP-"):
                                articles.append(
                                    (
                                        article_id,
                                        entry.get("title") or article_id,
                                        article_series,
                                        entry.get("link", article_id),
                                    )
                                )
                    except (OSError, json.JSONDecodeError) as error:
                        self.write_output(f"[{filename}] Error: {error}")

        articles = [
            article for article in articles if article[2].casefold() != "scp-001"
        ]
        if not articles:
            self.write_output("No SCP articles were found.")
            return
        articles.sort(key=lambda article: scp_sort_key(article[0]))
        grouped_articles = {}
        for article_id, title, series, identity in articles:
            grouped_articles.setdefault(series, []).append((article_id, title, identity))

        output = self.query_one("#output", VerticalScroll)
        ordered_series = sorted(grouped_articles, key=series_sort_key)
        if requested_series is None:
            series_lines = [
                f"- [{series_title(series)} ({len(grouped_articles[series])} articles)]"
                f"(scp-series:{quote(series, safe=':/_-')})"
                for series in ordered_series
            ]
            await self.mount_markdown_incrementally(output, "\n".join(series_lines))
            return

        requested_key = requested_series.casefold()
        series = next(
            (
                candidate
                for candidate in ordered_series
                if candidate.casefold() == requested_key
                or series_title(candidate).casefold() == requested_key
            ),
            None,
        )
        if series is None:
            self.write_output(
                f"Series '{requested_series}' not found. Type 'list' to see available series."
            )
            return

        article_lines = [
            f"## {series_title(series)} ({len(grouped_articles[series])} articles)",
            "",
        ]
        for article_id, title, identity in grouped_articles[series]:
            label = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            article_lines.append(
                f"- {article_id} - "
                f"[{label}](scp-article:{quote(identity, safe=':/_-')})"
            )
        await self.mount_markdown_incrementally(output, "\r".join(article_lines))

    def article_choices(self, number):
        if self.index is None:
            return []
        choices_by_id = {}
        for identity, metadata in self.index.items():
            article_id = metadata.get("scp_id", identity)
            match = re.fullmatch(r"SCP-(\d+)(.*)", article_id, re.IGNORECASE)
            if not match or int(match.group(1)) != number:
                continue
            folder = metadata.get("folder", "misc")
            choice = (
                article_id,
                metadata.get("title") or article_id,
                folder,
                identity,
            )
            key = article_id.casefold()
            existing = choices_by_id.get(key)
            if existing is None or (
                not folder.casefold().startswith("series-"),
                folder.casefold(),
            ) < (
                not existing[2].casefold().startswith("series-"),
                existing[2].casefold(),
            ):
                choices_by_id[key] = choice
        return sorted(
            choices_by_id.values(),
            key=lambda choice: scp_sort_key(choice[0]),
        )

    def resolve_article_identity(self, reference):
        if self.index is not None:
            normalized_reference = normalize_article_reference(reference)
            for identity in self.index:
                if normalize_article_reference(identity) == normalized_reference:
                    return identity
            for identity, metadata in self.index.items():
                if metadata.get("scp_id", "").casefold() == reference.casefold():
                    return identity
        return reference.upper()

    def find_article(self, reference, errors=None):
        def report_error(message):
            if errors is None:
                self.write_output(message)
            else:
                errors.append(message)

        identity = self.resolve_article_identity(reference)
        article_id = identity
        if self.index is not None:
            meta = self.index.get(identity)
            if not meta:
                return None
            article_id = meta.get("scp_id", identity)

            html_path = os.path.join(
                HTML_EXPORT_FOLDER,
                meta["folder"],
                meta.get("html_file", f"{identity}.html"),
            )
            if os.path.exists(html_path):
                try:
                    with open(html_path, encoding="utf-8") as file:
                        return meta["title"], html_to_text(
                            file.read(), article_reference_map(self.index)
                        )
                except OSError as error:
                    report_error(f"Error reading {html_path}: {error}")
                    return None

            json_path = os.path.join(JSON_FOLDER, meta["json_file"])
            try:
                with open(json_path, encoding="utf-8") as file:
                    entry = json.load(file)[meta.get("json_key", article_id)]
            except (OSError, json.JSONDecodeError, KeyError) as error:
                report_error(f"Error reading {meta['json_file']}: {error}")
                return None
            html = entry.get("raw_content") or entry.get("raw_source", "")
            return meta["title"], html_to_text(
                html, article_reference_map(self.index)
            )

        article_id = identity
        for root, _, files in os.walk(DATA_FOLDER):
            for filename in files:
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, encoding="utf-8") as file:
                        data = json.load(file)
                    article_match = next(
                        (
                            (article_id, value)
                            for article_id, value in data.items()
                            if article_id.casefold() == identity.casefold()
                            or value.get("link", "").casefold()
                            == reference.casefold()
                        ),
                        None,
                    )
                    if article_match is not None:
                        article_id, entry = article_match
                        html = entry.get("raw_content") or entry.get("raw_source", "")
                        title = entry.get("title") or entry.get("link") or article_id
                        return title, html_to_text(html)
                except (OSError, json.JSONDecodeError) as error:
                    report_error(f"[{filename}] Error: {error}")
        return None

    async def show_scp(self, reference):
        identity = self.resolve_article_identity(reference)
        tabs = self.query_one("#workspace-tabs", TabbedContent)
        pane_id = self.article_pane_ids.get(identity)
        if pane_id is not None:
            self.set_focus(None)
            tabs.active = pane_id
            return

        errors = []
        article = await asyncio.to_thread(self.find_article, identity, errors)
        for error in errors:
            self.write_output(error)
        if article is None:
            if not errors:
                self.write_output("SCP article not found.")
            return
        title, body = article
        pane_id = f"article-{len(self.article_pane_ids) + 1}"
        article_id = (
            self.index.get(identity, {}).get("scp_id", identity)
            if self.index is not None
            else identity
        )
        article_view = VerticalScroll(
            Static(
                f"{article_id} - {title}",
                classes="article-body",
                markup=False,
            ),
            classes="article-scroll",
        )
        await tabs.add_pane(TabPane(title, article_view, id=pane_id))
        self.article_pane_ids[identity] = pane_id
        self.set_focus(None)
        tabs.active = pane_id
        await self.mount_markdown_incrementally(
            article_view, body, classes="article-body"
        )

    async def on_input_submitted(self, event: Input.Submitted):
        command = event.value.strip()
        await self.execute_command(command, event.input)

    async def execute_command(self, command, command_input=None):
        self.prepare_command(command, command_input)
        await self.handle_command(command)

    def prepare_command(self, command, command_input=None):
        if command_input is None:
            command_input = self.query_one("#command", CommandInput)
        command_input.value = ""
        command_input.remember(command)
        if command:
            self.write_output(f"> {command}")

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
                "  list     - show series titles and article counts\n"
                "  list <series> - show article IDs and titles in a series\n"
                "  update   - run the loader and refresh the archive index\n"
                "  cls      - clear the terminal history\n"
                "  help     - show this help\n"
                "  exit     - quit\n\n"
                "Use the tabs to switch between the terminal and open articles. "
                "Press Ctrl+C to close an article tab or quit from Terminal."
            )
        elif normalized == "list":
            await self.list_scps()
        elif normalized.startswith("list "):
            await self.list_scps(command[5:].strip())
        elif normalized == "update":
            await self.update_archive()
        elif command.upper().startswith("SCP-"):
            await self.show_scp(command)
        elif command.isdigit() and 1 <= int(command) <= 9999:
            choices = self.article_choices(int(command))
            if len(choices) > 1:
                self.push_screen(
                    ArticleChoiceScreen(choices),
                    callback=self.on_article_choice,
                )
            elif choices:
                await self.show_scp(choices[0][3])
            else:
                await self.show_scp(f"SCP-{int(command):03}")
        elif self.index is not None and normalize_article_reference(command) in {
            normalize_article_reference(identity) for identity in self.index
        }:
            await self.show_scp(command)
        elif command:
            self.write_output("Unknown command. Type 'help'.")

    def on_article_choice(self, identity):
        if identity:
            self.run_worker(
                self.show_scp(identity),
                name=f"open-{identity}",
                group="open-article",
            )

    async def update_archive(self):
        loader_path = os.path.join(os.path.dirname(__file__), "scp_loader.py")
        self.write_output("Starting SCP archive update...")
        log_file = None
        try:
            os.makedirs(os.path.dirname(UPDATE_LOG_PATH), exist_ok=True)
            log_file = open(UPDATE_LOG_PATH, "w", encoding="utf-8")
        except OSError as error:
            self.write_output(f"Could not open update log {UPDATE_LOG_PATH}: {error}")

        def log_line(line):
            nonlocal log_file
            if log_file is None:
                return
            try:
                log_file.write(line + "\n")
                log_file.flush()
            except OSError as error:
                log_file.close()
                log_file = None
                self.write_output(f"Could not write update log {UPDATE_LOG_PATH}: {error}")

        log_line(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}]")
        log_line("Starting SCP archive update...")
        panel = self.query_one("#update-panel", Vertical)
        download_status = self.query_one("#download-status", Static)
        process_status = self.query_one("#process-status", Static)
        download_progress = self.query_one("#download-progress", ProgressBar)
        process_progress = self.query_one("#process-progress", ProgressBar)
        process_article_id = self.query_one("#process-article-id", Static)
        panel.display = True
        download_status.update("Downloading series: starting...")
        process_status.update("Processing articles")
        process_article_id.update("")
        download_progress.update(progress=0)
        process_progress.update(progress=0)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                loader_path,
                cwd=os.path.dirname(__file__),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            panel.display = False
            message = f"Could not start scp_loader.py: {error}"
            self.write_output(message)
            log_line(message)
            if log_file is not None:
                log_file.close()
            return

        try:
            if process.stdout is not None:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    output = line.decode(errors="replace").rstrip("\r\n")
                    if output:
                        events = self.parse_loader_events(output)
                        if events is None:
                            log_line(output)
                            self.write_output(output)
                            continue
                        for event in events:
                            if event.get("event") not in {
                                "progress",
                                "article-progress",
                            }:
                                log_line(json.dumps(event, ensure_ascii=False))
                            self.handle_loader_event(event)

            return_code = await process.wait()
            if return_code:
                panel.display = False
                message = f"Archive update failed (exit code {return_code})."
                self.write_output(message)
                log_line(message)
                return

            self.index_error = None
            self.index = self.load_index()
            if self.index is None:
                panel.display = False
                error = f": {self.index_error}" if self.index_error else ""
                message = f"Loader finished, but the article index is unavailable{error}."
                self.write_output(message)
                log_line(message)
                return
            panel.display = False
            message = f"Archive updated. {len(self.index)} articles indexed."
            self.write_output(message)
            log_line(message)
        finally:
            if log_file is not None:
                log_file.close()

    def handle_loader_output(self, output):
        events = self.parse_loader_events(output)
        if events is None:
            self.write_output(output)
            return
        for event in events:
            self.handle_loader_event(event)

    def handle_loader_event(self, event):
        if event.get("event") == "article-progress":
            article_id = event.get("article_id")
            if isinstance(article_id, str):
                self.query_one("#process-article-id", Static).update(f"- {article_id}")
            return
        if event.get("event") != "progress":
            self.write_output(output)
            return

        completed = event.get("completed")
        total = event.get("total")
        stage = event.get("stage")
        if not isinstance(completed, int) or not isinstance(total, int) or not isinstance(stage, str):
            self.write_output(output)
            return

        if stage == "Processing articles":
            status = self.query_one("#process-status", Static)
            progress = self.query_one("#process-progress", ProgressBar)
            status.update("Processing articles")
        else:
            status = self.query_one("#download-status", Static)
            progress = self.query_one("#download-progress", ProgressBar)
            status.update(f"{stage}: {completed}/{total}")
        progress.update(
            total=total if total > 0 else 1,
            progress=completed if total > 0 else 1,
        )

    @staticmethod
    def parse_loader_events(output):
        decoder = json.JSONDecoder()
        events = []
        position = 0
        while position < len(output):
            while position < len(output) and output[position].isspace():
                position += 1
            if position == len(output):
                break
            try:
                event, position = decoder.raw_decode(output, position)
            except json.JSONDecodeError:
                return None
            if not isinstance(event, dict):
                return None
            events.append(event)
        return events or None

    async def action_close_active_tab(self):
        tabs = self.query_one("#workspace-tabs", TabbedContent)
        pane_id = tabs.active
        if pane_id == "terminal-tab":
            self.exit()
            return

        await tabs.remove_pane(pane_id)
        for identity, article_pane_id in list(self.article_pane_ids.items()):
            if article_pane_id == pane_id:
                del self.article_pane_ids[identity]
                break
        self.query_one("#command", CommandInput).focus()

    async def on_markdown_link_clicked(self, event: Markdown.LinkClicked):
        if event.href.startswith("scp-article:"):
            await self.execute_command(
                unquote(event.href.removeprefix("scp-article:"))
            )
        elif event.href.startswith("scp-series:"):
            await self.execute_command(
                f"list {unquote(event.href.removeprefix('scp-series:'))}"
            )

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated):
        if (
            event.tabbed_content.id == "workspace-tabs"
            and event.tabbed_content.active == "terminal-tab"
        ):
            self.query_one("#command", CommandInput).focus()


if __name__ == "__main__":
    SCPReader().run()
