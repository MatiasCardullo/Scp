import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Input, Markdown, OptionList, TabbedContent

from scp_loader import (
    BASE_JSON_URL,
    CONTENT_INDEX_URL,
    build_article_index,
    build_wikidot_article,
    download_and_process_files,
    download_image,
    download_json_file,
    extract_scp_001_proposal_links,
    extract_series_titles,
    link_filename,
    normalize_link,
    process_json_file,
    queue_missing_images,
    run_parallel,
    main as run_loader,
    wikidot_url,
)
from scp_reader import (
    CommandInput,
    SCPReader,
    CollapsibleSection,
    article_reference_map,
    html_to_article_blocks,
    html_to_text,
)


class LinkTests(unittest.TestCase):
    def test_api_link_builds_canonical_url_and_safe_filename(self):
        link = "protected:scp-2721"

        self.assertEqual(
            wikidot_url(link),
            "https://scp-wiki.wikidot.com/protected:scp-2721",
        )
        self.assertEqual(link_filename(link), "protected%3Ascp-2721")

    def test_link_comparison_ignores_outer_slashes_and_case(self):
        self.assertEqual(normalize_link("/SCP-173/"), "scp-173")

    def test_download_skips_processing_when_remote_json_matches_local_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_series-1.json"
            path.write_text('{"SCP-002": {"title": "The Living Room"}}', encoding="utf-8")
            response = SimpleNamespace(
                content=b'{\n  "SCP-002": {"title": "The Living Room"}\n}',
                json=lambda: {"SCP-002": {"title": "The Living Room"}},
                raise_for_status=lambda: None,
            )
            with (
                patch("scp_loader.JSON_FOLDER", directory),
                patch("scp_loader.requests.get", return_value=response),
            ):
                result = download_json_file(path.name)

            self.assertEqual(result, (str(path), False))
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                '{"SCP-002": {"title": "The Living Room"}}',
            )

    def test_download_marks_changed_json_for_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_series-1.json"
            path.write_text('{"SCP-002": {"title": "Old title"}}', encoding="utf-8")
            response = SimpleNamespace(
                content=b'{"SCP-002": {"title": "New title"}}',
                json=lambda: {"SCP-002": {"title": "New title"}},
                raise_for_status=lambda: None,
            )
            with (
                patch("scp_loader.JSON_FOLDER", directory),
                patch("scp_loader.requests.get", return_value=response),
            ):
                result = download_json_file(path.name)

            self.assertEqual(result, (str(path), True))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), response.json())

    def test_parallel_worker_emits_line_progress(self):
        progress = []

        with patch(
            "scp_loader.emit_progress",
            side_effect=lambda stage, completed, total: progress.append(
                (stage, completed, total)
            ),
        ):
            results = run_parallel([1, 2], lambda item: item * 2, "test")

        self.assertEqual(sorted(results), [2, 4])
        self.assertEqual(progress[0], ("test", 0, 2))
        self.assertCountEqual(progress[1:], [("test", 1, 2), ("test", 2, 2)])

    def test_image_download_retries_transient_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            busy_response = SimpleNamespace(status_code=503)
            responses = [
                SimpleNamespace(
                    raise_for_status=Mock(
                        side_effect=requests.HTTPError(
                            "Service unavailable",
                            response=busy_response,
                        )
                    )
                ),
                SimpleNamespace(
                    raise_for_status=lambda: None,
                    content=b"image data",
                ),
            ]
            with (
                patch("scp_loader.requests.get", side_effect=responses) as get,
                patch("scp_loader.time.sleep") as sleep,
            ):
                download_image(("https://images.example/image.jpg", str(path)))

            self.assertEqual(get.call_count, 2)
            self.assertEqual(
                get.call_args.kwargs["headers"],
                {
                    "User-Agent": (
                        "SCP-Terminal-Archive/1.0 "
                        "(https://github.com/MatiasCardullo/Scp)"
                    )
                },
            )
            sleep.assert_called_once_with(1)
            self.assertEqual(path.read_bytes(), b"image data")

    def test_queues_missing_local_images_from_saved_source_json(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            image_directory = Path(directory) / "images"
            image_directory.mkdir()
            url = "https://images.example/articles/image.jpg"
            source.write_text(
                json.dumps(
                    {
                        "SCP-002": {
                            "raw_content": (
                                f'<a href="{url}"><img src="image.jpg"></a>'
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            queued = []
            with (
                patch("scp_loader.IMG_FOLDER", str(image_directory)),
                patch("scp_loader.download_queue", queued),
                patch("scp_loader._enqueued_urls", set()),
            ):
                queue_missing_images([(str(source), "series-1")])

            self.assertEqual(
                queued,
                [(url, str(image_directory / "articles_image.jpg"))],
            )

    def test_article_processing_downloads_images_only_when_media_is_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            html_directory = Path(directory) / "html"
            image_directory = Path(directory) / "images"
            html_directory.mkdir()
            image_directory.mkdir()
            url = "https://images.example/articles/image.jpg"
            source.write_text(
                json.dumps(
                    {
                        "SCP-002": {
                            "link": "scp-002",
                            "raw_content": f'<img src="{url}">',
                        }
                    }
                ),
                encoding="utf-8",
            )
            article = {
                "scp_id": "SCP-002",
                "folder": "series-1",
                "html_file": "scp-002.html",
            }
            queued = []
            with (
                patch("scp_loader.HTML_FOLDER", str(html_directory)),
                patch("scp_loader.IMG_FOLDER", str(image_directory)),
                patch("scp_loader.download_queue", queued),
                patch("scp_loader._enqueued_urls", set()),
            ):
                process_json_file(source, "series-1", {"scp-002": article}, {}, {})
                default_html = (
                    html_directory / "series-1" / "scp-002.html"
                ).read_text(encoding="utf-8")
                self.assertEqual(queued, [])
                self.assertIn(url, default_html)

                process_json_file(
                    source,
                    "series-1",
                    {"scp-002": article},
                    {},
                    {},
                    download_media=True,
                )
                media_html = (
                    html_directory / "series-1" / "scp-002.html"
                ).read_text(encoding="utf-8")

            self.assertEqual(
                queued,
                [(url, str(image_directory / "articles_image.jpg"))],
            )
            self.assertIn("../images/articles_image.jpg", media_html)

    def test_article_processing_starts_before_all_downloads_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = str(Path(directory) / "first.json")
            second_path = str(Path(directory) / "second.json")
            for path in (first_path, second_path):
                Path(path).write_text("{}", encoding="utf-8")
            processing_started = threading.Event()
            processing_finished = threading.Event()
            finish_processing = threading.Event()
            proposals_started = threading.Event()
            progress_stages = []

            def fake_download(filename, force_process=False):
                if filename == "second.json":
                    self.assertTrue(processing_started.wait(timeout=2))
                path = first_path if filename == "first.json" else second_path
                return path, filename == "first.json"

            def fake_process(*_, **__):
                processing_started.set()
                self.assertTrue(finish_processing.wait(timeout=2))
                processing_finished.set()
                return {}

            def download_proposals():
                self.assertFalse(processing_finished.is_set())
                proposals_started.set()
                finish_processing.set()

            with (
                patch("scp_loader.JSON_FOLDER", directory),
                patch("scp_loader.download_json_file", side_effect=fake_download),
                patch("scp_loader.build_article_index", return_value=({}, {})),
                patch("scp_loader.process_json_file", side_effect=fake_process),
                patch(
                    "scp_loader.emit_progress",
                    side_effect=lambda stage, *_: progress_stages.append(stage),
                ),
            ):
                downloaded, changed, partial_indexes = download_and_process_files(
                    [("first.json", False), ("second.json", False)],
                    {"first.json": "series-1", "second.json": "series-2"},
                    {},
                    on_downloads_complete=download_proposals,
                )

            self.assertTrue(processing_started.is_set())
            self.assertTrue(proposals_started.is_set())
            self.assertTrue(processing_finished.is_set())
            self.assertEqual(len(downloaded), 2)
            self.assertEqual(changed, [(first_path, "series-1")])
            self.assertEqual(partial_indexes, [{}])
            self.assertIn("Downloading series", progress_stages)
            self.assertIn("Processing articles", progress_stages)

    def test_loader_only_builds_changed_json_files_and_keeps_unchanged_index_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            json_folder = base / "json"
            json_folder.mkdir()
            (base / "html").mkdir()
            (base / "images").mkdir()
            first_name = "content_series-1.json"
            changed_name = "content_series-2.json"
            (base / "html" / "series-1").mkdir()
            (base / "html" / "series-2").mkdir()
            (base / "html" / "series-1" / "scp-001.html").write_text(
                "<p>Same</p>", encoding="utf-8"
            )
            (base / "html" / "series-2" / "scp-002.html").write_text(
                "<p>Old</p>", encoding="utf-8"
            )
            first_data = {
                "SCP-001": {"link": "scp-001", "title": "Same"},
            }
            old_changed_data = {
                "SCP-002": {"link": "scp-002", "title": "Old"},
            }
            new_changed_data = {
                "SCP-002": {"link": "scp-002", "title": "Updated"},
            }
            (json_folder / first_name).write_text(
                json.dumps(first_data), encoding="utf-8"
            )
            (json_folder / changed_name).write_text(
                json.dumps(old_changed_data), encoding="utf-8"
            )
            scp_001_name = "content_scp-001.json"
            (json_folder / scp_001_name).write_text("{}", encoding="utf-8")
            previous_index = {
                "scp-001": {
                    "json_file": first_name,
                    "title": "Same",
                    "folder": "series-1",
                    "html_file": "scp-001.html",
                },
                "scp-002": {
                    "json_file": changed_name,
                    "title": "Old",
                    "folder": "series-2",
                    "html_file": "scp-002.html",
                },
            }
            (base / "index.json").write_text(
                json.dumps(previous_index), encoding="utf-8"
            )

            def make_response(data):
                return SimpleNamespace(
                    content=json.dumps(data).encode("utf-8"),
                    json=lambda: data,
                    raise_for_status=lambda: None,
                )

            response_by_url = {
                BASE_JSON_URL + first_name: make_response(first_data),
                BASE_JSON_URL + changed_name: make_response(new_changed_data),
            }
            content_index = {
                "series-1": first_name,
                "series-2": changed_name,
            }
            process_calls = []
            call_order = []

            def process_file(path, folder, *_, **__):
                process_calls.append(Path(path).name)
                return {
                    Path(path).stem: {
                        "json_file": Path(path).name,
                        "folder": folder,
                    }
                }

            def get(url, **_):
                if url == CONTENT_INDEX_URL:
                    return make_response(content_index)
                call_order.append(url)
                return response_by_url[url]

            def fetch_titles():
                call_order.append("titles")
                return {}

            with (
                patch("scp_loader.BASE_FOLDER", str(base)),
                patch("scp_loader.JSON_FOLDER", str(json_folder)),
                patch("scp_loader.HTML_FOLDER", str(base / "html")),
                patch("scp_loader.IMG_FOLDER", str(base / "images")),
                patch("scp_loader.requests.get", side_effect=get),
                patch("scp_loader.fetch_series_titles", side_effect=fetch_titles),
                patch(
                    "scp_loader.update_scp_001_json",
                    return_value=(str(json_folder / scp_001_name), False),
                ),
                patch("scp_loader.process_json_file", side_effect=process_file),
                patch("scp_loader.queue_missing_images") as queue_images,
            ):
                self.assertEqual(run_loader(), 0)

            self.assertEqual(process_calls, [changed_name])
            queue_images.assert_not_called()
            self.assertEqual(call_order[0], "titles")
            updated_index = json.loads((base / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(updated_index["scp-001"], previous_index["scp-001"])
            self.assertIn(Path(changed_name).stem, updated_index)


class SeriesTitleTests(unittest.TestCase):
    def test_extracts_article_titles_from_listing_rows(self):
        html = """
        <div id="page-content">
          <ul>
            <li><a href="/scp-002">SCP-002</a> - The "Living" Room</li>
            <li><a href="/scp-003">SCP-003</a> - Biological Motherboard</li>
          </ul>
        </div>
        """

        titles = extract_series_titles(html)
        self.assertEqual(titles["scp-002"], 'The "Living" Room')
        self.assertEqual(titles["SCP-002"], 'The "Living" Room')
        self.assertEqual(titles["scp-003"], "Biological Motherboard")
        self.assertEqual(titles["SCP-003"], "Biological Motherboard")

    def test_ignores_non_article_links_and_rows_without_titles(self):
        html = """
        <div id="page-content">
          <ul>
            <li><a href="/scp-series-2">SCP-002</a> - Not an article link</li>
            <li><a href="/scp-003">SCP-003</a></li>
          </ul>
        </div>
        """

        self.assertEqual(extract_series_titles(html), {})

    def test_indexes_alternate_visible_titles_by_api_link(self):
        html = """
        <div id="page-content">
          <ul>
            <li><a href="/scp-213">PDG-213</a> - Anti-Matter Parasites</li>
            <li><a href="/scp-9089">Alleyway</a></li>
            <li><a href="/scp-3183">dissociation</a> -</li>
          </ul>
        </div>
        """

        titles = extract_series_titles(html)
        self.assertEqual(titles["scp-213"], "Anti-Matter Parasites")
        self.assertEqual(titles["scp-9089"], "Alleyway")
        self.assertEqual(titles["scp-3183"], "dissociation")


class SCP001Tests(unittest.TestCase):
    def test_scp_001_index_is_routed_to_series_1_but_proposals_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_scp-001.json"
            path.write_text(
                json.dumps(
                    {
                        "SCP-001": {
                            "link": "scp-001",
                            "scp": "SCP-001",
                            "series": "series-1",
                        },
                        "proposal-one": {
                            "link": "proposal-one",
                            "scp": "SCP-001",
                        },
                    }
                ),
                encoding="utf-8",
            )

            articles, _ = build_article_index([(str(path), "scp-001")])

        self.assertEqual(articles["scp-001"]["folder"], "series-1")
        self.assertEqual(articles["proposal-one"]["folder"], "scp-001")

    def test_extracts_only_proposal_links_from_the_official_panel(self):
        html = """
        <div id="page-content">
          <div class="content-panel standalone series">
            <a href="/proposal-one">CODE NAME: One</a>
            <a href="/proposal-one">CODE NAME: Duplicate</a>
            <a href="/proposal-two">CODE NAME: Two</a>
            <a href="/licensing-guide">Licensing Guide</a>
          </div>
        </div>
        """

        self.assertEqual(
            extract_scp_001_proposal_links(html),
            ["proposal-one", "proposal-two"],
        )

    def test_builds_api_shaped_record_from_an_official_page(self):
        html = """
        <html><head><title>Proposal</title></head><body>
          <div id="page-title">Proposal One</div>
          <div class="page-tags"><a>001-proposal</a><a>scp</a></div>
          <div class="rate-points">rating: +12</div>
          <div id="page-content">
            <p>Author: Alice</p><a href="/scp-173">SCP-173</a>
            <img src="/local--files/proposal-one/image.png"/>
            <div class="licensebox">Cite as "Proposal One" by Alice, from the SCP Wiki.</div>
          </div>
        </body></html>
        """

        record = build_wikidot_article("proposal-one", html)
        self.assertEqual(record["title"], "Proposal One")
        self.assertEqual(record["creator"], "Alice")
        self.assertEqual(record["scp"], "SCP-001")
        self.assertEqual(record["rating"], 12)
        self.assertEqual(record["tags"], ["001-proposal", "scp"])
        self.assertEqual(record["references"], ["SCP-173"])
        self.assertEqual(
            record["images"],
            ["https://scp-wiki.wikidot.com/local--files/proposal-one/image.png"],
        )
        self.assertIn('id="page-content"', record["raw_content"])
        self.assertEqual(record["raw_source"], "")


class ReaderReferenceTests(unittest.TestCase):
    def test_article_identity_takes_precedence_over_duplicate_scp_id_aliases(self):
        reader = SCPReader()
        reader.index = {
            "shaggydredlocks-proposal": {"scp_id": "SCP-001"},
            "scp-001": {"scp_id": "SCP-001"},
        }

        self.assertEqual(reader.resolve_article_identity("SCP-001"), "scp-001")

    def test_proposal_json_uses_its_link_key_when_html_is_not_available(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "content_scp-001.json"
            json_path.write_text(
                '{"proposal-slug":{"raw_content":"<p>Full proposal text</p>"}}',
                encoding="utf-8",
            )
            reader = SCPReader()
            reader.index = {
                "proposal-slug": {
                    "scp_id": "SCP-001",
                    "title": "Proposal",
                    "folder": "scp-001",
                    "json_file": json_path.name,
                    "json_key": "proposal-slug",
                }
            }

            with (
                patch("scp_reader.JSON_FOLDER", directory),
                patch("scp_reader.HTML_EXPORT_FOLDER", str(Path(directory) / "html")),
            ):
                title, body = reader.find_article("proposal-slug")

        self.assertEqual(title, "Proposal")
        self.assertIn("Full proposal text", body)

    def test_local_html_and_scp_id_links_resolve_to_api_link_identity(self):
        index = {
            "taboo": {
                "link": "taboo",
                "url": "https://scp-wiki.wikidot.com/taboo",
                "scp_id": "SCP-4000",
                "html_file": "taboo.html",
            }
        }
        html = """
        <a href="../series-10.0/taboo.html">SCP-4000</a>
        <a href="/scp-4000">SCP-4000</a>
        """

        text = html_to_text(html, article_reference_map(index))

        self.assertEqual(text.count("(scp-article:taboo)"), 2)

    def test_without_index_still_converts_scp_links(self):
        text = html_to_text('<a href="/scp-002">The Living Room</a>')

        self.assertIn("(scp-article:SCP-002)", text)


class CollapsibleArticleTests(unittest.TestCase):
    def test_converts_nested_sections_and_keeps_internal_article_links(self):
        index = {
            "scp-002": {
                "scp_id": "SCP-002",
                "html_file": "scp-002.html",
            }
        }
        html = """
        <div id="page-content">
          <p>Intro</p>
          <div class="collapsible-block">
            <div class="collapsible-block-folded">
              <a class="collapsible-block-link" href="javascript:;">+ Interview A</a>
            </div>
            <div class="collapsible-block-unfolded" style="display:none">
              <div class="collapsible-block-content">
                <p>See <a href="../series-1/scp-002.html">SCP-002</a>.</p>
                <div class="collapsible-block">
                  <div class="collapsible-block-folded">
                    <a class="collapsible-block-link" href="javascript:;">+ Nested file</a>
                  </div>
                  <div class="collapsible-block-unfolded">
                    <div class="collapsible-block-content"><p>Nested text.</p></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <p>Outro</p>
        </div>
        """

        blocks = html_to_article_blocks(html, article_reference_map(index))

        self.assertEqual(len(blocks), 3)
        self.assertIn("Intro", blocks[0])
        self.assertIsInstance(blocks[1], CollapsibleSection)
        self.assertEqual(blocks[1].title, "Interview A")
        self.assertIn("(scp-article:scp-002)", blocks[1].content[0])
        nested = next(
            block
            for block in blocks[1].content
            if isinstance(block, CollapsibleSection)
        )
        self.assertEqual(nested.title, "Nested file")
        self.assertIn("Nested text", nested.content[0])
        self.assertIn("Outro", blocks[2])


class ReaderListTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_shows_series_counts_and_id_before_titles(self):
        index = {
            "scp-002": {
                "scp_id": "SCP-002",
                "title": "The Living Room",
                "folder": "series-1",
            },
            "scp-003": {
                "scp_id": "SCP-003",
                "title": "Biological Motherboard",
                "folder": "series-1",
            },
            "scp-001": {
                "scp_id": "SCP-001",
                "title": "The Foundation",
                "folder": "series-1",
            },
            "shaggydredlocks-proposal": {
                "scp_id": "SCP-001",
                "title": "S. D. Locke's Proposal",
                "folder": "scp-001",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            missing_index = str(Path(directory) / "index.json")
            with (
                patch("scp_reader.INDEX_PATH", missing_index),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = index
                async with reader.run_test() as pilot:
                    await reader.handle_command("list")
                    await pilot.pause()
                    overview = reader.query("#output Markdown")[-1]._markdown
                    self.assertIn("SCP Series 1 (3 articles)", overview)
                    self.assertNotIn("SCP-001 Proposals", overview)
                    self.assertNotIn("The Living Room", overview)

                    await reader.handle_command("list SCP Series 1")
                    await pilot.pause()
                    detail = reader.query("#output Markdown")[-1]._markdown
                    self.assertIn(
                        "- SCP-001 - [The Foundation](scp-article:scp-001)",
                        detail,
                    )
                    self.assertIn(
                        "- SCP-002 - [The Living Room](scp-article:scp-002)",
                        detail,
                    )
                    self.assertIn(
                        "- SCP-003 - [Biological Motherboard](scp-article:scp-003)",
                        detail,
                    )

    async def test_series_click_is_echoed_and_runs_the_list_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {
                    "scp-012": {
                        "scp_id": "SCP-012",
                        "title": "A Bad Composition",
                        "folder": "joke",
                    }
                }
                async with reader.run_test() as pilot:
                    await pilot.pause()
                    await reader.on_markdown_link_clicked(
                        SimpleNamespace(href="scp-series:joke")
                    )
                    await pilot.pause()

                    command_input = reader.query_one("#command", Input)
                    output = reader.query("#output Static")
                    self.assertEqual(command_input.history, ["update", "list joke"])
                    self.assertIn("> list joke", [str(widget.render()) for widget in output])
                    markdown = reader.query("#output Markdown")[-1]._markdown
                    self.assertIn("Joke SCPs (1 articles)", markdown)

    async def test_article_click_is_echoed_and_runs_the_article_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {"scp-012": {"scp_id": "SCP-012"}}
                with patch.object(
                    reader, "show_scp", new_callable=AsyncMock
                ) as show_scp:
                    async with reader.run_test() as pilot:
                        await pilot.pause()
                        await reader.on_markdown_link_clicked(
                            SimpleNamespace(href="scp-article:scp-012")
                        )
                        await pilot.pause()

                        command_input = reader.query_one("#command", Input)
                        output = reader.query("#output Static")
                        self.assertEqual(command_input.history, ["update", "scp-012"])
                        self.assertIn("> scp-012", [str(widget.render()) for widget in output])
                        show_scp.assert_awaited_once_with("scp-012")

    async def test_numeric_command_shows_variants_with_normal_scp_selected_first(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {
                    "scp-173-j": {
                        "scp_id": "SCP-173-J",
                        "title": "The Sculpture - Joke",
                        "folder": "joke",
                    },
                    "scp-173": {
                        "scp_id": "SCP-173",
                        "title": "The Sculpture",
                        "folder": "series-1",
                    },
                }
                with patch.object(
                    reader, "show_scp", new_callable=AsyncMock
                ) as show_scp:
                    async with reader.run_test() as pilot:
                        command = asyncio.create_task(reader.handle_command("173"))
                        await pilot.pause()

                        options = reader.screen.query_one(
                            "#article-choice-options", OptionList
                        )
                        self.assertEqual(
                            [option.id for option in options._options],
                            ["scp-173", "scp-173-j"],
                        )
                        self.assertEqual(options.highlighted, 0)

                        await pilot.press("down", "enter")
                        await command
                        await pilot.pause()
                        show_scp.assert_awaited_once_with("scp-173-j")

    async def test_numeric_command_without_variants_opens_article_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {"scp-173": {"scp_id": "SCP-173"}}
                with patch.object(
                    reader, "show_scp", new_callable=AsyncMock
                ) as show_scp:
                    async with reader.run_test():
                        await reader.handle_command("173")
                        show_scp.assert_awaited_once_with("scp-173")

    async def test_numeric_command_opens_a_single_suffix_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {
                    "scp-173-j": {
                        "scp_id": "SCP-173-J",
                        "title": "The Sculpture - Joke",
                        "folder": "joke",
                    }
                }
                with patch.object(
                    reader, "show_scp", new_callable=AsyncMock
                ) as show_scp:
                    async with reader.run_test():
                        await reader.handle_command("173")
                        show_scp.assert_awaited_once_with("scp-173-j")

    async def test_large_markdown_is_mounted_in_incremental_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                async with reader.run_test():
                    output = reader.query_one("#output", VerticalScroll)
                    await reader.mount_markdown_incrementally(
                        output, "\n".join(f"line {number}" for number in range(60))
                    )

                    self.assertEqual(len(output.query(Markdown)), 3)

    async def test_large_article_is_rendered_incrementally_in_its_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            html_directory = Path(directory) / "html" / "series-1"
            html_directory.mkdir(parents=True)
            (html_directory / "scp-012.html").write_text(
                "<html><body>"
                + "".join(f"<p>Article line {number}</p>" for number in range(60))
                + "</body></html>",
                encoding="utf-8",
            )
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch("scp_reader.HTML_EXPORT_FOLDER", str(Path(directory) / "html")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {
                    "scp-012": {
                        "scp_id": "SCP-012",
                        "title": "A Bad Composition",
                        "folder": "series-1",
                        "html_file": "scp-012.html",
                    }
                }
                async with reader.run_test():
                    await reader.show_scp("scp-012")
                    article_lines = reader.query("#article-1 Markdown")

                    self.assertGreater(len(article_lines), 1)
                    rendered = "\n".join(widget._markdown for widget in article_lines)
                    self.assertIn("Article line 0", rendered)
                    self.assertIn("Article line 59", rendered)

    async def test_article_collapsible_is_interactive_and_closed_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            html_directory = Path(directory) / "html" / "series-1"
            html_directory.mkdir(parents=True)
            (html_directory / "scp-003.html").write_text(
                """
                <p>Visible text.</p>
                <div class="collapsible-block">
                  <div class="collapsible-block-folded">
                    <a class="collapsible-block-link" href="javascript:;">+ Access file</a>
                  </div>
                  <div class="collapsible-block-unfolded" style="display:none">
                    <div class="collapsible-block-content"><p>Hidden report.</p></div>
                  </div>
                </div>
                """,
                encoding="utf-8",
            )
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch("scp_reader.HTML_EXPORT_FOLDER", str(Path(directory) / "html")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {
                    "scp-003": {
                        "scp_id": "SCP-003",
                        "title": "Biological Motherboard",
                        "folder": "series-1",
                        "html_file": "scp-003.html",
                    }
                }
                async with reader.run_test() as pilot:
                    await reader.show_scp("scp-003")
                    section = reader.query_one("#article-1 Collapsible", Collapsible)

                    self.assertTrue(section.collapsed)
                    self.assertEqual(section.title, "Access file")
                    self.assertIn("Hidden report", section.query_one(Markdown)._markdown)
                    title = section.query_one("CollapsibleTitle")
                    title.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertFalse(section.collapsed)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(section.collapsed)

    async def test_missing_index_starts_archive_update(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_index = str(Path(directory) / "index.json")
            update_started = asyncio.Event()
            finish_update = asyncio.Event()

            async def hold_update_open():
                update_started.set()
                await finish_update.wait()

            with (
                patch("scp_reader.INDEX_PATH", missing_index),
                patch.object(
                    SCPReader,
                    "update_archive",
                    new_callable=AsyncMock,
                    side_effect=hold_update_open,
                ) as update_archive,
            ):
                reader = SCPReader()
                try:
                    async with reader.run_test() as pilot:
                        await asyncio.wait_for(update_started.wait(), timeout=1)

                        output = reader.query_one("#output", VerticalScroll)
                        rendered_output = [
                            str(widget.render()) for widget in output.children
                        ]
                        command_input = reader.query_one("#command", CommandInput)
                        self.assertIn("Welcome to the SCP.", rendered_output[0])
                        self.assertIn("> update", rendered_output)
                        self.assertEqual(command_input.history, ["update"])
                        self.assertEqual(
                            reader.query_one("#workspace-tabs", TabbedContent).active,
                            "terminal-tab",
                        )
                        update_archive.assert_awaited_once()
                        finish_update.set()
                        await pilot.pause()
                finally:
                    finish_update.set()

    async def test_update_progress_panel_aligns_with_terminal_and_input(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("scp_reader.INDEX_PATH", str(Path(directory) / "index.json")),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock),
            ):
                reader = SCPReader()
                reader.index = {}
                async with reader.run_test() as pilot:
                    panel = reader.query_one("#update-panel")
                    panel.display = True
                    await pilot.pause()

                    output = reader.query_one("#output")
                    command = reader.query_one("#command")
                    status = reader.query_one("#download-status")
                    progress = reader.query_one("#download-progress")
                    self.assertEqual(panel.region.x, output.region.x)
                    self.assertEqual(panel.region.x, command.region.x)
                    self.assertGreater(progress.region.x, status.region.x)
                    self.assertLessEqual(
                        progress.region.right,
                        panel.region.right,
                    )
                    self.assertGreater(progress.region.width, 24)
                    self.assertLessEqual(status.region.right, progress.region.x)
                    self.assertGreater(
                        reader.query_one("#process-progress").region.x,
                        reader.query_one("#process-status").region.x,
                    )

                    reader.handle_loader_output(
                        json.dumps(
                            {
                                "event": "progress",
                                "stage": "Downloading series",
                                "completed": 2,
                                "total": 4,
                            }
                        )
                        + json.dumps(
                            {
                                "event": "article-progress",
                                "article_id": "SCP-173",
                            }
                        )
                        + json.dumps(
                            {
                                "event": "progress",
                                "stage": "Processing articles",
                                "completed": 16,
                                "total": 20,
                            }
                        )
                    )
                    self.assertIn(
                        "Downloading series: 2/4",
                        str(reader.query_one("#download-status").render()),
                    )
                    self.assertIn(
                        "Processing articles",
                        str(reader.query_one("#process-status").render()),
                    )
                    self.assertNotIn(
                        "16/20",
                        str(reader.query_one("#process-status").render()),
                    )
                    process_progress = reader.query_one("#process-progress")
                    self.assertEqual(process_progress.total, 20)
                    self.assertEqual(process_progress.progress, 16)
                    self.assertIn(
                        "- SCP-173",
                        str(reader.query_one("#process-article-id").render()),
                    )

    async def test_update_command_accepts_optional_media_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "index.json"
            index_path.write_text("{}", encoding="utf-8")
            with (
                patch("scp_reader.INDEX_PATH", str(index_path)),
                patch.object(SCPReader, "update_archive", new_callable=AsyncMock) as update,
            ):
                reader = SCPReader()
                reader.index = {}
                async with reader.run_test():
                    await reader.handle_command("update")
                    await reader.handle_command("UPDATE --MEDIA")

            self.assertEqual(update.await_count, 2)
            self.assertEqual(update.await_args_list[0].kwargs, {})
            self.assertEqual(
                update.await_args_list[1].kwargs,
                {"download_media": True},
            )

    async def test_update_archive_forwards_media_flag_to_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "index.json"
            log_path = Path(directory) / "update.log"
            index_path.write_text("{}", encoding="utf-8")
            stdout = asyncio.StreamReader()
            stdout.feed_eof()
            process = SimpleNamespace(
                stdout=stdout,
                wait=AsyncMock(return_value=1),
            )

            with (
                patch("scp_reader.INDEX_PATH", str(index_path)),
                patch("scp_reader.UPDATE_LOG_PATH", str(log_path)),
            ):
                reader = SCPReader()
                reader.index = {}
                async with reader.run_test():
                    with patch(
                        "scp_reader.asyncio.create_subprocess_exec",
                        new_callable=AsyncMock,
                        return_value=process,
                    ) as create_process:
                        await reader.update_archive(download_media=True)

            self.assertEqual(create_process.await_args.args[-1], "--media")

    async def test_update_archive_saves_loader_output_and_errors_to_log(self):
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "index.json"
            log_path = Path(directory) / "update.log"
            index_path.write_text('{"scp-173": {}}', encoding="utf-8")
            log_path.write_text("older update output\n", encoding="utf-8")
            stdout = asyncio.StreamReader()
            stdout.feed_data(b"Downloading content index...\n")
            stdout.feed_data(
                b'{"event":"progress","stage":"Downloading series",'
                b'"completed":1,"total":1}'
                b'{"event":"article-progress","article_id":"SCP-173"}\n'
            )
            stdout.feed_data(b"Error downloading file: network unavailable\n")
            stdout.feed_eof()
            process = SimpleNamespace(
                stdout=stdout,
                wait=AsyncMock(return_value=1),
            )

            with (
                patch("scp_reader.INDEX_PATH", str(index_path)),
                patch("scp_reader.UPDATE_LOG_PATH", str(log_path)),
            ):
                reader = SCPReader()
                reader.index = {}
                async with reader.run_test():
                    with patch(
                        "scp_reader.asyncio.create_subprocess_exec",
                        new_callable=AsyncMock,
                        return_value=process,
                    ):
                        await reader.update_archive()

            log = log_path.read_text(encoding="utf-8")
            self.assertIn("Starting SCP archive update...", log)
            self.assertIn("Downloading content index...", log)
            self.assertNotIn('"event":"progress"', log)
            self.assertNotIn("older update output", log)
            self.assertIn("Error downloading file: network unavailable", log)
            self.assertIn("Archive update failed (exit code 1).", log)
            self.assertNotIn(
                "{",
                "\n".join(
                    str(widget.render())
                    for widget in reader.query("#output Static")
                ),
            )


if __name__ == "__main__":
    unittest.main()
