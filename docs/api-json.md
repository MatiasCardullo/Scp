# API JSON files

The loader uses the public dataset at
[scp-data.tedivm.com](https://scp-data.tedivm.com/), specifically the
`https://scp-data.tedivm.com/data/scp/items/` path.

## Content index

`content_index.json` is the manifest queried by `scp_loader.py`. The code
treats it as a map from series/group names to JSON filenames. For example, an
entry may map `series-1` to `content_series-1.json`. The manifest is queried
during updates; the loader does not save it as a separate file in
`scp_data/`.

The manifest can include numbered series (for example, `series-1` and
`series-6.5`) and special groups such as `decommissioned`, `explained`,
`international`, and `joke`.

## Article files

Each content file is a JSON object. Its keys identify articles, and each value
is an article record. In the copies used by this repository, records include
the following fields:

| Field | Contents |
|---|---|
| `created_at` | Date/time associated with the record, as text. |
| `creator` | Author or creator attributed to the page. |
| `domain` | Source domain, usually Wikidot. |
| `history` | List of revisions; entries may include author, date, comment, and author link. |
| `hubs` | List of related hub identifiers. |
| `images` | List of associated image URLs; these are not the binary files. |
| `link` | Wikidot page path or slug. The project uses it as the article identity in its local index. |
| `page_id` | Wikidot page identifier. |
| `rating` | Numeric rating. |
| `raw_content` | Rendered HTML for the page content. This is the primary source for generating local HTML. |
| `raw_source` | Original Wikidot source when available; it may be empty or have a non-text value in some data. |
| `references` | List of references to other articles. |
| `scp` | Visible article identifier, for example `SCP-002`. |
| `scp_number` | Integer associated with the SCP identifier. |
| `series` | Series or group the record belongs to. |
| `tags` | List of page tags. |
| `title` | Title provided by the dataset. |
| `url` | Canonical page URL. |

Records usually contain metadata, revision history, and extensive HTML
content, so files can be large. Do not assume fields such as `history`,
`images`, or `references` are non-empty, or that HTML content is uniform
across articles.

## SCP-001 exception

This project's `content_scp-001.json` is not one of the JSON files listed by
the API. The loader builds it from the official SCP-001 index page and the
proposal pages linked there. It attempts to preserve a record shape compatible
with other articles. Since this data is extracted from HTML pages, some
metadata normally provided by the API (such as revision history or `page_id`)
may be empty or unavailable.

## What the project preserves

API files are saved under `scp_data/json/`. When the loader downloads an API
JSON file, it preserves the downloaded content; transformations for local
reading are written separately under `scp_data/html/` and in the local index.
See [Local data](scp-data.md).
