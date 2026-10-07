# SCP Data API JSON files

The [SCP Data API](https://scp-data.tedivm.com/) is a daily-updated static
dataset of SCP Wiki pages. The API's public documentation lists the JSON files
below; it is a file-based dataset rather than a conventional query API. The
same data is also available from the
[scp-api GitHub repository](https://github.com/scp-data/scp-api).

All paths below are relative to `https://scp-data.tedivm.com/`. The API site
documents three datasets: the main SCP Wiki, tales, and Groups of Interest
(GOI). This inventory includes endpoints the project does not currently use.

## Endpoint inventory

| Dataset | Endpoint | Contents |
|---|---|---|
| SCP Wiki hubs | [`data/scp/hubs/index.json`](https://scp-data.tedivm.com/data/scp/hubs/index.json) | One JSON object keyed by hub `link`; each value contains hub metadata and `raw_content`. |
| SCP item metadata | [`data/scp/items/index.json`](https://scp-data.tedivm.com/data/scp/items/index.json) | Metadata for SCP items, keyed by article `link`. The `content_file` field points to the file containing that item's content. |
| SCP item content manifest | [`data/scp/items/content_index.json`](https://scp-data.tedivm.com/data/scp/items/content_index.json) | Maps item series/group names to content JSON filenames. |
| SCP item content files | `data/scp/items/<filename-from-content_index>` | Article records for each group, including `raw_content` and `raw_source` instead of `content_file`. The manifest determines the current filenames and groups. |
| Tale metadata | [`data/scp/tales/index.json`](https://scp-data.tedivm.com/data/scp/tales/index.json) | Metadata for tales, keyed by article `link`; `content_file` identifies each tale's content file. |
| Tale content manifest | [`data/scp/tales/content_index.json`](https://scp-data.tedivm.com/data/scp/tales/content_index.json) | Maps creation years to tale content JSON files. |
| Tale content files | `data/scp/tales/<filename-from-content_index>` | Tale records with their content fields. The manifest determines the current filenames. |
| GOI metadata | [`data/scp/goi/index.json`](https://scp-data.tedivm.com/data/scp/goi/index.json) | Metadata for Groups of Interest stories, keyed by article `link`; records point to the GOI content file. |
| GOI content | [`data/scp/goi/content_goi.json`](https://scp-data.tedivm.com/data/scp/goi/content_goi.json) | GOI article records, including `raw_content` and `raw_source`. |

The item manifest currently includes regular series as well as groups such as
`decommissioned`, `explained`, `international`, and `joke`. These names and
filenames can change as the dataset is updated; read the manifest rather than
hard-coding its entries. The manifest may also include `scp-001`. The project's
loader has its own SCP-001 handling, described in [Scripts](scripts.md).

The API documentation describes content filenames as relative to the
corresponding manifest or metadata file. Some live manifest values may instead
be absolute build-machine paths. Consumers should treat the manifest as the
source of truth for filenames and normalize paths before forming download URLs.

## Common fields

Article and hub records share some fields. Depending on dataset type and
record, fields may be absent or empty.

| Field | Contents |
|---|---|
| `link` | Primary key generated from the page URL path. |
| `url` | Direct URL to the Wikidot page. |
| `page_id` | Wikidot page ID, usable with the Wikidot API for additional page information. |
| `created_at` | Creation time, typically formatted as `YYYY-MM-DDTHH:MM:SS`. |
| `created_by` | Wikidot username of the first-commit author; may be absent. Records may also expose `creator` as the attributed creator. |
| `history` | Ordered revision objects containing `author`, `author_href` (or `false` when unknown), `comment`, and `date`. |
| `domain` | Source domain, when provided. |

## Dataset-specific fields

### Hubs

Each hub record includes `title`, `references`, `tags`, and `raw_content`, in
addition to common fields. `references` contains article/tale `link` values
belonging to that hub.

### SCP items

Metadata records can include:

- `content_file`: content filename associated with the item.
- `references`: links to referenced articles and tales.
- `tags`: page tags.
- `title`: user-facing article title.
- `hubs`: hub links associated with the item.
- `images`: image URLs found on the page; these are not image files.
- `rating`: Wikidot vote rating.
- `scp` and `scp_number`: displayed SCP identifier and its numeric component.
- `series`: series or group name.

Content records have the item metadata plus `raw_content` (rendered HTML) and
`raw_source` (the source markup), in place of `content_file`.

### Tales

Tale metadata uses the same general article fields, including
`content_file`, `created_at`, `created_by`/`creator`, `link`, `page_id`,
`references`, `tags`, `title`, `url`, `hubs`, `images`, `rating`, and
`history`. Content files add `raw_content` and `raw_source` in place of
`content_file`.

### Groups of Interest (GOI)

GOI metadata has the same general structure as tale metadata. GOI content is
collected in `content_goi.json`; content records contain `raw_content` and
`raw_source` in place of `content_file`. GOI article formats can often be
identified from their tags.

## Content notes and project usage

Content files can be large. `raw_content` contains the page's `#page-content`
HTML (excluding navigation, advertisements, and page headers); `raw_source`
contains the Wikidot source when available. The API says the HTML has only
been run through a simple beautifier. Neither content field should be assumed
to be present, non-empty, or uniform across pages. Metadata files let clients
inspect records without first downloading all article content.

This project currently uses the SCP item metadata/content endpoints when
building its local archive. It does not currently import the API's hubs, tales,
or GOI datasets. It also fetches SCP-001 proposals from the official wiki as
part of its own loading workflow. See [Local data](scp-data.md) for how API
files and generated files are stored.

The dataset is maintained independently and updated daily. Field descriptions
reflect the API's published documentation and observed records, not a guarantee
that every record will always contain every field. SCP Wiki content is subject
to the [wiki's license](https://scp-wiki.wikidot.com/licensing-guide).
