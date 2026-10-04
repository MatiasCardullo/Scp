import json
import os
import re

from bs4 import BeautifulSoup
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Markdown, RichLog, Static

INDEX_PATH = os.path.join("scp_data", "index.json")
HTML_EXPORT_FOLDER = os.path.join("scp_data", "html")
JSON_FOLDER = os.path.join("scp_data", "json")


def html_to_text(html):
    """Convert article HTML into readable terminal text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

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
    return "\n".join(lines).strip()


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


class ArticleScreen(Screen):
    """Scrollable terminal view for a single SCP article."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    CSS = """
    Screen {
        background: #050805;
        color: #33ff33;
    }
    #article-scroll {
        height: 1fr;
        border: round #1f7a1f;
        margin: 0 1;
        padding: 1 2;
    }
    #article-body {
        color: #b6ffb6;
    }
    """

    def __init__(self, slug, title, body):
        super().__init__()
        self.slug = slug
        self.title = title
        self.body = body

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(
            Static(f"{self.slug} - {self.title}\n\n{self.body}", id="article-body", markup=False),
            id="article-scroll",
        )
        yield Footer()


class SCPListScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    CSS = """
    Screen {
        background: #050805;
        color: #33ff33;
    }
    #list-scroll {
        height: 1fr;
        border: round #1f7a1f;
        margin: 0 1;
        padding: 1;
    }
    #scp-grid {
        padding: 0 1;
    }
    """

    def __init__(self, articles):
        super().__init__()
        self.articles = articles
        self.link_to_slug = {
            str(index): slug for index, (slug, _) in enumerate(articles)
        }

    def compose(self) -> ComposeResult:
        links = []
        for index, (slug, title) in enumerate(self.articles):
            label = f"{slug} - {title}"
            label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            links.append(f"[{label}](scp-article:{index})")
        rows = [
            "    ".join(links[index:index + 4])
            for index in range(0, len(links), 4)
        ]
        yield Header(show_clock=False)
        yield VerticalScroll(
            Markdown("\n\n".join(rows), id="scp-grid", open_links=False),
            id="list-scroll",
        )
        yield Footer()

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked):
        if not event.href.startswith("scp-article:"):
            return
        slug = self.link_to_slug.get(event.href.removeprefix("scp-article:"))
        if slug:
            self.app.show_scp(slug)


def scp_sort_key(slug):
    match = re.fullmatch(r"SCP-(\d+)(.*)", slug, re.IGNORECASE)
    if not match:
        return (1, float("inf"), slug.casefold(), slug.casefold())
    number, suffix = match.groups()
    return (bool(suffix), int(number), suffix.casefold(), slug.casefold())


class SCPReader(App):
    TITLE = "SCP Terminal Reader"
    SUB_TITLE = "SCP-OS"

    BINDINGS = [Binding("ctrl+c", "quit", "Quit", show=True)]

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
    CommandInput {
        margin: 0 1 1 1;
        border: round #1f7a1f;
    }
    """

    def __init__(self):
        super().__init__()
        self.index_error = None
        self.index = self.load_index()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)
        yield CommandInput(placeholder="Type help to see available commands", id="command")
        yield Footer()

    def on_mount(self):
        if self.index:
            self.write_output(
                f"Welcome to the SCP Reader. {len(self.index)} articles indexed. "
                "Type 'help' to get started."
            )
        else:
            self.write_output(
                "Welcome to the SCP Reader. scp_data/index.json was not found; "
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
        self.query_one("#output", RichLog).write(text)

    def list_scps(self):
        if self.index is not None:
            articles = [
                (slug, entry.get("title", slug))
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
                        articles.extend(
                            (slug, entry["title"])
                            for slug, entry in data.items()
                            if slug.startswith("SCP-") and entry.get("title")
                        )
                    except (OSError, json.JSONDecodeError) as error:
                        self.write_output(f"[{filename}] Error: {error}")

        if not articles:
            self.write_output("No SCP articles were found.")
            return
        articles.sort(key=lambda article: scp_sort_key(article[0]))
        self.push_screen(SCPListScreen(articles))

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
                        return meta["title"], html_to_text(file.read())
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
            return meta["title"], html_to_text(html)

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

    def show_scp(self, slug):
        slug = slug.upper()
        article = self.find_article(slug)
        if article is None:
            self.write_output("SCP article not found.")
            return
        title, body = article
        self.push_screen(ArticleScreen(slug, title, body))

    def on_input_submitted(self, event: Input.Submitted):
        command = event.value.strip()
        event.input.value = ""
        event.input.remember(command)
        if command:
            self.write_output(f"> {command}")
        self.handle_command(command)

    def handle_command(self, command):
        normalized = command.lower()
        if normalized in ("exit", "quit"):
            self.exit()
        elif normalized == "help":
            self.write_output(
                "Commands:\n"
                "  SCP-###  - open an article\n"
                "  ###      - open an article by number\n"
                "  list     - browse articles in a clickable grid\n"
                "  help     - show this help\n"
                "  exit     - quit\n\n"
                "Press Esc in an article or the list to go back."
            )
        elif normalized == "list":
            self.list_scps()
        elif command.upper().startswith("SCP-"):
            self.show_scp(command)
        elif command.isdigit() and 1 <= int(command) <= 9999:
            self.show_scp(f"SCP-{int(command):03}")
        elif command:
            self.write_output("Unknown command. Type 'help'.")


if __name__ == "__main__":
    SCPReader().run()
