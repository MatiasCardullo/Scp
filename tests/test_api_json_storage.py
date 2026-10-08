import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loader
import reader


class ApiJsonStorageTests(unittest.TestCase):
    def test_downloads_api_json_and_compares_it_with_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_series-1.json"
            original = '{"SCP-002":{"title":"Existing"}}'
            path.write_text(original, encoding="utf-8")
            response = SimpleNamespace(
                content=b'{\n  "SCP-002": {"title": "Existing"}\n}',
                json=lambda: {"SCP-002": {"title": "Existing"}},
                raise_for_status=lambda: None,
            )
            with (
                patch.object(loader, "API_FOLDER", directory),
                patch.object(loader.requests, "get", return_value=response) as get,
            ):
                result = loader.download_json_file(path.name)

            self.assertEqual(result, (str(path), False))
            get.assert_called_once()
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_reader_resolves_new_api_json_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            api_folder = Path(directory) / "api"
            api_folder.mkdir()
            (api_folder / "content_series-1.json").write_text(
                '{"SCP-002":{"raw_content":"<p>API source</p>"}}',
                encoding="utf-8",
            )
            archive_reader = reader.SCPReader()
            archive_reader.index = {
                "scp-002": {
                    "scp_id": "SCP-002",
                    "title": "API article",
                    "folder": "series-1",
                    "json_file": "api/content_series-1.json",
                }
            }
            with (
                patch.object(reader, "DATA_FOLDER", directory),
                patch.object(
                    reader, "HTML_EXPORT_FOLDER", str(Path(directory) / "html")
                ),
            ):
                title, body = archive_reader.find_article("scp-002")

        self.assertEqual(title, "API article")
        self.assertIn("API source", body)

    def test_sends_saved_http_validators_and_skips_unchanged_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_series-1.json"
            path.write_text('{"SCP-002":{"title":"Same"}}', encoding="utf-8")
            (Path(directory) / ".http_cache.json").write_text(
                json.dumps(
                    {
                        path.name: {
                            "etag": '"dataset-version"',
                            "last_modified": "Wed, 07 Oct 2026 04:31:14 GMT",
                        }
                    }
                ),
                encoding="utf-8",
            )
            response = SimpleNamespace(
                status_code=304,
                headers={},
                raise_for_status=lambda: None,
            )
            with (
                patch.object(loader, "API_FOLDER", directory),
                patch.object(loader.requests, "get", return_value=response) as get,
            ):
                result = loader.download_json_file(path.name)

            self.assertEqual(result, (str(path), False))
            self.assertEqual(
                get.call_args.kwargs["headers"],
                {
                    "If-None-Match": '"dataset-version"',
                    "If-Modified-Since": "Wed, 07 Oct 2026 04:31:14 GMT",
                },
            )
            self.assertEqual(get.call_count, 1)

    def test_saves_http_validators_after_downloading_json(self):
        with tempfile.TemporaryDirectory() as directory:
            body = b'{"SCP-002":{"title":"New"}}'
            response = SimpleNamespace(
                status_code=200,
                headers={
                    "ETag": '"dataset-version"',
                    "Last-Modified": "Wed, 07 Oct 2026 04:31:14 GMT",
                },
                content=body,
                json=lambda: {"SCP-002": {"title": "New"}},
                raise_for_status=lambda: None,
            )
            with (
                patch.object(loader, "API_FOLDER", directory),
                patch.object(loader.requests, "get", return_value=response),
            ):
                result = loader.download_json_file("content_series-1.json")

            self.assertEqual(result, (str(Path(directory) / "content_series-1.json"), True))
            cache = json.loads((Path(directory) / ".http_cache.json").read_text())
            self.assertEqual(
                cache["content_series-1.json"],
                {
                    "etag": '"dataset-version"',
                    "last_modified": "Wed, 07 Oct 2026 04:31:14 GMT",
                },
            )

    def test_processing_progress_uses_fixed_ten_percent_series_milestones(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for series, article_ids in (
                ("series-1", ["SCP-001", "SCP-002"]),
                ("series-2", ["SCP-1001", "SCP-1002"]),
                ("series-3", ["SCP-2001"]),
            ):
                filename = f"content_{series}.json"
                path = Path(directory) / filename
                path.write_text(
                    json.dumps({article_id: {} for article_id in article_ids}),
                    encoding="utf-8",
                )
                paths[filename] = str(path)

            progress_events = []

            def process_file(*args, article_progress, article_ids, **_):
                for article_id in article_ids:
                    article_progress(article_id)
                return {}

            with (
                patch.object(loader, "API_FOLDER", directory),
                patch.object(
                    loader,
                    "download_json_file",
                    side_effect=lambda filename, **_: (paths[filename], True),
                ),
                patch.object(loader, "build_article_index", return_value=({}, {})),
                patch.object(loader, "process_json_file", side_effect=process_file),
                patch.object(
                    loader,
                    "emit_progress",
                    side_effect=lambda stage, completed, total: progress_events.append(
                        (stage, completed, total)
                    ),
                ),
                patch.object(loader, "emit_article_progress"),
            ):
                loader.download_and_process_files(
                    [(filename, False) for filename in paths],
                    {
                        filename: Path(filename).stem.removeprefix("content_")
                        for filename in paths
                    },
                    {},
                )

            article_events = [
                (completed, total)
                for stage, completed, total in progress_events
                if stage == "Processing articles"
            ]
            self.assertTrue(article_events)
            self.assertTrue(all(total == 100 for _, total in article_events))
            self.assertIn((10, 100), article_events)
            self.assertIn((20, 100), article_events)
            self.assertIn((30, 100), article_events)
            self.assertEqual(article_events[-1], (100, 100))


if __name__ == "__main__":
    unittest.main()
