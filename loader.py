import argparse
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import quote, urljoin, urlparse

BASE_FOLDER = "data"
API_FOLDER = os.path.join(BASE_FOLDER, "api")
JSON_FOLDER = os.path.join(BASE_FOLDER, "json")
HTML_FOLDER = os.path.join(BASE_FOLDER, "html")
IMG_FOLDER = os.path.join(BASE_FOLDER, "images")
BASE_JSON_URL = "https://scp-data.tedivm.com/data/scp/items/"
CONTENT_INDEX_URL = BASE_JSON_URL + "content_index.json"
WIKIDOT_BASE_URL = "https://scp-wiki.wikidot.com/"
SCP_ID_RE = re.compile(r"^SCP-\d+[\w-]*$", re.IGNORECASE)
SERIES_LINK_RE = re.compile(r"^/?(SCP-\d+[\w-]*)$", re.IGNORECASE)

MAX_WORKERS = 1  # Number of files processed/downloaded concurrently.
IMAGE_DOWNLOAD_ATTEMPTS = 10
IMAGE_REQUEST_HEADERS = {
    "User-Agent": "SCP-Terminal-Archive/1.0 (https://github.com/MatiasCardullo/Scp)"
}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
SCP_HREF_RE = re.compile(r'^/scp-(\d+[\w-]*)', re.IGNORECASE)
SCP_MENTION_RE = re.compile(r'\b(SCP-\d{1,4})\b')
SPECIAL_GROUP_ORDER = {
    "content_decommissioned.json": 0,
    "content_explained.json": 1,
    "content_international.json": 2,
    "content_joke.json": 3,
}
SERIES_FILE_RE = re.compile(r"^series-(\d+(?:\.\d+)?)$", re.IGNORECASE)
ARTICLE_ID_RE = re.compile(r"^SCP-(\d+)(.*)$", re.IGNORECASE)
output_lock = Lock()
download_queue = []
queue_lock = Lock()
_enqueued_urls = set()
api_cache_lock = Lock()


def emit_output(*args, **kwargs):
    with output_lock:
        print(*args, **kwargs)


def ensure_folder(path):
    os.makedirs(path, exist_ok=True)


def emit_progress(desc, completed, total):
    emit_output(
        json.dumps(
            {
                "event": "progress",
                "stage": desc,
                "completed": completed,
                "total": total,
            }
        ),
        flush=True,
    )


def emit_article_progress(article_id):
    emit_output(
        json.dumps({"event": "article-progress", "article_id": article_id}),
        flush=True,
    )


def article_sort_key(article_id):
    article_id = str(article_id)
    match = ARTICLE_ID_RE.match(article_id)
    if not match:
        return (2, float("inf"), article_id.casefold())
    number, suffix = match.groups()
    return (bool(suffix), int(number), suffix.casefold())


def article_source_sort_key(source_key):
    if source_key.casefold() == "scp-001":
        return (-1, 0, source_key.casefold())
    match = SERIES_FILE_RE.match(source_key)
    if not match:
        return (1, float("inf"), source_key.casefold())
    return (0, float(match.group(1)), source_key.casefold())


def series_sort_key(source_key):
    match = SERIES_FILE_RE.match(source_key)
    if not match:
        return (1, float("inf"), source_key.casefold())
    return (0, float(match.group(1)), source_key.casefold())


def series_for_scp_number(number):
    if number < 1000:
        return "series-1"
    if number < 5000:
        return f"series-{number // 1000}"
    group = (number - 5000) // 1000
    half = ".0" if (number - 5000) % 1000 <= 500 else ".5"
    return f"series-{6 + group}{half}"


def download_order_key(job):
    filename, source_key = job
    special_order = SPECIAL_GROUP_ORDER.get(os.path.basename(filename).casefold())
    if special_order is not None:
        return (0, special_order, filename.casefold())
    if not isinstance(source_key, str):
        source_key = os.path.splitext(os.path.basename(filename))[0].removeprefix(
            "content_"
        )
    return (1, *series_sort_key(source_key), filename.casefold())


def run_parallel(items, worker_fn, desc):
    """Run worker_fn concurrently and emit progress events for the reader."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_fn, item): item for item in items}
        emit_progress(desc, 0, len(futures))
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            emit_progress(desc, completed, len(futures))
    return results


def download_json_file(filename, force_process=False):
    filepath = os.path.join(API_FOLDER, filename)
    url = BASE_JSON_URL + filename
    try:
        cache = load_api_cache()
        cached_metadata = cache.get(filename, {})
        if not isinstance(cached_metadata, dict):
            cached_metadata = {}
        headers = {}
        if os.path.isfile(filepath):
            if cached_metadata.get("etag"):
                headers["If-None-Match"] = cached_metadata["etag"]
            if cached_metadata.get("last_modified"):
                headers["If-Modified-Since"] = cached_metadata["last_modified"]

        r = requests.get(url, headers=headers, timeout=30)
        if getattr(r, "status_code", 200) == 304:
            if os.path.isfile(filepath):
                return filepath, force_process
            r = requests.get(url, timeout=30)
        r.raise_for_status()
        remote_data = r.json()
        response_headers = getattr(r, "headers", {})
        if response_headers is None:
            response_headers = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, encoding="utf-8") as existing_file:
                    if json.load(existing_file) == remote_data:
                        save_api_cache_entry(
                            filename,
                            response_headers.get("ETag"),
                            response_headers.get("Last-Modified"),
                        )
                        return filepath, force_process
            except (OSError, json.JSONDecodeError):
                pass
        temporary_path = filepath + ".tmp"
        try:
            with open(temporary_path, "wb") as file:
                file.write(r.content)
            os.replace(temporary_path, filepath)
            save_api_cache_entry(
                filename,
                response_headers.get("ETag"),
                response_headers.get("Last-Modified"),
            )
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        return filepath, True
    except Exception as e:
        emit_output(f"Error downloading {filename}: {e}", flush=True)
        return (filepath, False) if os.path.exists(filepath) else None


def load_api_cache():
    cache_path = os.path.join(API_FOLDER, ".http_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as file:
            cache = json.load(file)
        return cache if isinstance(cache, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        emit_output(f"Error reading API cache {cache_path}: {error}", flush=True)
        return {}


def save_api_cache_entry(filename, etag, last_modified):
    cache_path = os.path.join(API_FOLDER, ".http_cache.json")
    temporary_path = cache_path + ".tmp"
    with api_cache_lock:
        cache = load_api_cache()
        if etag or last_modified:
            metadata = {}
            if etag:
                metadata["etag"] = etag
            if last_modified:
                metadata["last_modified"] = last_modified
            cache[filename] = metadata
        elif filename in cache:
            del cache[filename]
        else:
            return
        try:
            with open(temporary_path, "w", encoding="utf-8") as file:
                json.dump(cache, file, ensure_ascii=False)
            os.replace(temporary_path, cache_path)
        except OSError as error:
            emit_output(f"Error saving API cache {cache_path}: {error}", flush=True)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)


def image_filename_from_url(url):
    """Build a local filename from an image URL. The last path segment alone
    is not enough: many dataset images share a generic name (e.g.
    '.../scp-025/025.jpeg/medium.jpg', where 'medium.jpg' appears in dozens
    of different articles). Use the last path segments to make it unique."""
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    tail = parts[-3:] if len(parts) >= 3 else parts
    name = "_".join(tail) if tail else "image"
    return re.sub(r'[^\w\-_.]', '_', name)


def enqueue_image(url):
    if not url.startswith("http"):
        return url
    filename = image_filename_from_url(url)
    local_path = os.path.join(IMG_FOLDER, filename)
    with queue_lock:
        if url not in _enqueued_urls:
            _enqueued_urls.add(url)
            download_queue.append((url, local_path))
    return f"../images/{filename}"


def download_image(item):
    url, path = item
    if os.path.exists(path):
        return
    for attempt in range(1, IMAGE_DOWNLOAD_ATTEMPTS + 1):
        try:
            response = requests.get(
                url,
                headers=IMAGE_REQUEST_HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            with open(path, "wb") as file:
                file.write(response.content)
            return
        except requests.RequestException as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            retryable = status_code == 429 or status_code is None or status_code >= 500
            if retryable and attempt < IMAGE_DOWNLOAD_ATTEMPTS:
                time.sleep(attempt)
                continue
            emit_output(f"Image error for {url}: {error}", flush=True)
            return
        except OSError as error:
            emit_output(f"Image error for {url}: {error}", flush=True)
            return


def build_image_url_map(soup):
    """Map filename to its real URL by checking every <a href> in the document
    that points to an image. The actual <img src> is often a generic relative
    path (e.g. '../images/medium.jpg'), while the real URL only appears in a
    link elsewhere in the article (e.g. the license box) or wrapping the image."""
    url_map = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(IMAGE_EXTS):
            filename = href.rsplit("/", 1)[-1]
            url_map[filename] = href
    return url_map


def resolve_image_url(img, url_map):
    src = img.get("src")
    if not src:
        return None
    if src.startswith("http"):
        # Wikidot avatars (author/history) are decorative and use user-specific
        # query strings, which would otherwise all collide on the same filename.
        return None if "avatar.php" in src.lower() else src
    parent_href = img.parent.get("href") if img.parent.name == "a" else None
    if parent_href and parent_href.lower().startswith("http") and parent_href.lower().endswith(IMAGE_EXTS):
        return parent_href
    filename = src.rsplit("/", 1)[-1]
    real_url = url_map.get(filename)
    if real_url and "avatar.php" in real_url.lower():
        return None
    return real_url


def normalize_link(link):
    """Return a case-insensitive Wikidot page path for comparing API links."""
    path = urlparse(link).path if "://" in link else link
    return path.strip().strip("/").casefold()


def link_filename(link):
    """Build a Windows-safe local filename from the API's Wikidot link."""
    path = urlparse(link).path if "://" in link else link
    path = path.strip("/")
    return quote(path, safe="-_.")


def wikidot_url(link):
    """Build the canonical SCP Wiki URL for a relative API link."""
    path = urlparse(link).path if "://" in link else link
    return WIKIDOT_BASE_URL + path.lstrip("/")


def extract_series_titles(html):
    """Extract titles indexed by both listing URL paths and visible SCP IDs."""
    soup = BeautifulSoup(html, "html.parser")
    titles = {}
    for anchor in soup.select("#page-content a[href]"):
        item = anchor.find_parent("li")
        if item is None:
            continue
        path = urlparse(anchor["href"]).path
        path_match = SERIES_LINK_RE.fullmatch(path)
        anchor_text = anchor.get_text(" ", strip=True)
        article_id = anchor_text.upper()
        if re.fullmatch(r"/scp-series(?:-\d+)?", path, re.IGNORECASE):
            continue
        if not path_match and not SCP_ID_RE.fullmatch(article_id):
            continue

        item_text = item.get_text(" ", strip=True)
        if item_text == anchor_text:
            if SCP_ID_RE.fullmatch(article_id):
                continue
            title = anchor_text
        elif item_text.startswith(anchor_text):
            title = re.sub(
                r"^\s*[-–—]\s*",
                "",
                item_text[len(anchor_text):],
                count=1,
            ).strip()
            if not title and not SCP_ID_RE.fullmatch(article_id):
                title = anchor_text
        else:
            continue
        if title:
            if path_match:
                titles[normalize_link(path)] = title
                titles[path_match.group(1).upper()] = title
            elif SCP_ID_RE.fullmatch(article_id):
                titles[article_id] = title
    return titles


def extract_scp_001_proposal_links(html):
    """Return the proposal page paths listed in the official SCP-001 panel."""
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.select("#page-content .content-panel.standalone.series")
    if not panels:
        raise ValueError("The official SCP-001 proposal panel was not found")

    links = []
    seen = set()
    for panel in panels:
        for anchor in panel.select("a[href]"):
            if not anchor.get_text(" ", strip=True).startswith("CODE NAME:"):
                continue
            path = urlparse(anchor["href"]).path.strip("/")
            if not path or path.casefold() in seen:
                continue
            seen.add(path.casefold())
            links.append(path)
        if links:
            break
    if not links:
        raise ValueError("No proposal links were found on the official SCP-001 page")
    return links


def build_wikidot_article(link, page_html):
    """Convert an official Wikidot page into the dataset's article record shape."""
    page_url = wikidot_url(link)
    soup = BeautifulSoup(page_html, "html.parser")
    content = soup.select_one("#page-content")
    if content is None:
        raise ValueError(f"No article content found at {page_url}")

    title_node = soup.select_one("#page-title")
    title = title_node.get_text(" ", strip=True) if title_node else link
    tags_node = soup.select_one(".page-tags")
    tags = (
        [tag.get_text(" ", strip=True) for tag in tags_node.select("a")]
        if tags_node
        else []
    )
    creator = ""
    licensebox = content.select_one(".licensebox")
    if licensebox:
        match = re.search(
            r"\bby\s+(.+?),\s+from the SCP Wiki",
            licensebox.get_text(" ", strip=True),
            re.IGNORECASE,
        )
        if match:
            creator = match.group(1)
    if not creator:
        creator_node = content.select_one(".printuser")
        creator = creator_node.get_text(" ", strip=True) if creator_node else ""

    rating = None
    rating_node = soup.select_one(".rate-points")
    if rating_node:
        rating_match = re.search(r"[-+]?\d+", rating_node.get_text(" ", strip=True))
        if rating_match:
            rating = int(rating_match.group())

    images = []
    for image in content.select("img[src]"):
        source = urljoin(page_url, image["src"])
        if source not in images:
            images.append(source)

    references = list(dict.fromkeys(SCP_MENTION_RE.findall(content.get_text(" "))))
    for anchor in content.select("a[href]"):
        match = SCP_HREF_RE.match(anchor["href"].strip())
        if match:
            reference = f"SCP-{match.group(1)}"
            if reference not in references:
                references.append(reference)

    return {
        "created_at": None,
        "creator": creator,
        "domain": urlparse(page_url).netloc,
        "history": [],
        "hubs": [],
        "images": images,
        "link": link,
        "page_id": None,
        "rating": rating,
        "raw_content": f"<html><body>{content}</body></html>",
        "raw_source": "",
        "references": references,
        "scp": "SCP-001",
        "scp_number": 1,
        "series": "scp-001",
        "tags": tags,
        "title": title,
        "url": page_url,
    }


def update_scp_001_json(force_process=False):
    """Save the official index page and every proposal into the SCP-001 JSON."""
    index_url = wikidot_url("scp-001")
    response = requests.get(index_url, timeout=30)
    response.raise_for_status()
    proposal_links = extract_scp_001_proposal_links(response.text)

    def fetch_article(link):
        page_response = requests.get(wikidot_url(link), timeout=30)
        page_response.raise_for_status()
        final_link = urlparse(page_response.url).path.strip("/")
        return final_link, build_wikidot_article(final_link, page_response.text)

    proposal_records = run_parallel(
        proposal_links,
        fetch_article,
        "Downloading SCP-001 proposals",
    )
    records = {
        "SCP-001": build_wikidot_article("scp-001", response.text),
    }
    records["SCP-001"]["series"] = "series-1"
    for link, record in proposal_records:
        records[link] = record

    filepath = os.path.join(JSON_FOLDER, "content_scp-001.json")
    if os.path.exists(filepath):
        try:
            with open(filepath, encoding="utf-8") as file:
                if json.load(file) == records:
                    return filepath, force_process
        except (OSError, json.JSONDecodeError):
            pass
    with open(filepath, "w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False)
    return filepath, True


def fetch_series_titles():
    """Fetch titles from the official main-series listing pages 1 through 10."""
    titles = {}
    for series_number in range(1, 11):
        url = f"{WIKIDOT_BASE_URL}scp-series-{series_number}"
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            emit_output(f"Error downloading series {series_number} titles: {error}", flush=True)
            continue

        series_titles = extract_series_titles(response.text)
        if not series_titles:
            emit_output(f"No article titles found on {response.url}", flush=True)
            continue
        titles.update(series_titles)
    return titles


def build_article_index(json_files):
    """Map API links to article metadata and SCP identifiers to API links."""
    articles_by_link = {}
    links_by_id = {}
    for path, key in json_files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for article_id, entry in data.items():
                link = entry.get("link")
                if not isinstance(link, str) or not link.strip():
                    emit_output(
                        f"Skipping {article_id} in {os.path.basename(path)}: "
                        "missing API link"
                    )
                    continue
                link = link.strip()
                filename = link_filename(link)
                normalized_link = normalize_link(link)
                article_scp_id = (
                    entry.get("scp", article_id)
                    if key.casefold() == "scp-001"
                    else article_id
                )
                article_folder = (
                    entry.get("series", key)
                    if key.casefold() == "scp-001"
                    and article_id.casefold() == "scp-001"
                    else key
                )
                article = {
                    "link": link,
                    "url": wikidot_url(link),
                    "scp_id": article_scp_id,
                    "folder": article_folder,
                    "html_file": f"{filename}.html",
                }
                if article_id.casefold() != article_scp_id.casefold():
                    article["json_key"] = article_id
                articles_by_link[normalized_link] = article
                links_by_id[article["scp_id"].upper()] = article
        except (OSError, json.JSONDecodeError) as error:
            emit_output(f"Error reading {path}: {error}", flush=True)
    return articles_by_link, links_by_id


def process_json_file(
    filepath,
    subfolder_name,
    articles_by_link,
    links_by_id,
    titles_by_reference,
    article_progress=None,
    download_media=False,
    article_ids=None,
    additional_articles=None,
):
    """Process a series JSON file: generate HTML for each article (with links
    between SCPs, local images, and no self-references) and return its partial
    index keyed by API link."""
    partial_index = {}
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        emit_output(f"Error reading {filepath}: {e}", flush=True)
        return partial_index

    def find_article_location(mention):
        """Return a linked article and its relative HTML path for a local SCP ID."""
        article = links_by_id.get(mention.upper())
        if not article:
            return None, None
        return article, f"{article['folder']}/{article['html_file']}"

    selected_ids = set(article_ids) if article_ids is not None else None
    article_records = [
        (article_id, entry, filepath, subfolder_name)
        for article_id, entry in data.items()
        if selected_ids is None or article_id in selected_ids
    ]
    article_records.extend(
        (article_id, entry, source_path, source_folder)
        for source_path, source_folder, article_id, entry in (
            additional_articles or []
        )
    )
    article_records.sort(
        key=lambda record: (
            *article_sort_key(record[1].get("scp", record[0])),
            *article_source_sort_key(record[3]),
            str(record[0]).casefold(),
        )
    )

    for article_id, entry, source_path, source_folder in article_records:
        if article_progress is not None:
            article_progress(article_id)
        link = entry.get("link")
        if not isinstance(link, str) or not link.strip():
            emit_output(
                f"Skipping {article_id} in {os.path.basename(source_path)}: "
                "missing API link"
            )
            continue
        link = link.strip()
        normalized_link = normalize_link(link)
        article = articles_by_link.get(normalized_link)
        if article is None:
            emit_output(f"Skipping {article_id}: API link was not indexed", flush=True)
            continue

        if source_folder.casefold() == "scp-001":
            title = entry.get("title") or article_id
        else:
            title = (
                titles_by_reference.get(normalized_link)
                or titles_by_reference.get(article_id.upper())
                or entry.get("title")
                or article_id
            )
        html = entry.get("raw_content") or entry.get("raw_source", "")
        soup = BeautifulSoup(html, "html.parser")
        own_id_upper = article.get("scp_id", article_id).upper()

        # Remove the "‡ Licensing / Citation" box, which is repeated boilerplate;
        # its collapsible links (javascript:;) do not work here.
        for box in soup.find_all("div", class_="licensebox"):
            box.decompose()

        if download_media:
            # --- Images: resolve the real URL (parent <a> or filename match). ---
            url_map = build_image_url_map(soup)
            for img in soup.find_all("img"):
                real_url = resolve_image_url(img, url_map)
                if real_url:
                    img["src"] = enqueue_image(real_url)

        # --- Existing links to other SCPs. ---
        for a in soup.find_all("a", href=True):
            m = SCP_HREF_RE.match(a["href"].strip())
            if not m:
                continue
            mention = f"SCP-{m.group(1)}"
            if mention.upper() == own_id_upper:
                # Self-reference (e.g. citation box): plain, non-clickable text.
                a.replace_with(a.get_text())
                continue
            target_article, rel_path = find_article_location(mention)
            if target_article:
                a["href"] = f"../{rel_path}"
            else:
                # Not available locally: keep it as a real external link.
                a["href"] = wikidot_url(a["href"])

        # --- Standalone mentions in plain text (never wrapped in an <a>). ---
        def link_scp_refs(text):
            def sub(m):
                mention = m.group(1)
                if mention.upper() == own_id_upper:
                    return mention
                target_article, rel_path = find_article_location(mention)
                if target_article:
                    return f'<a href="../{rel_path}">{mention}</a>'
                return mention
            return SCP_MENTION_RE.sub(sub, text)

        for tag in soup.find_all(string=True):
            if tag.parent.name in ("script", "style", "a"):
                continue
            new_html = link_scp_refs(str(tag))
            if new_html != str(tag):
                # Parse as a fragment, not plain text, or the <a> will be escaped.
                tag.replace_with(BeautifulSoup(new_html, "html.parser"))

        filename = article["html_file"]
        folder_path = os.path.join(HTML_FOLDER, article["folder"])
        ensure_folder(folder_path)
        html_path = os.path.join(folder_path, filename)
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
        except Exception as e:
            emit_output(f"Error saving HTML for {article_id}: {e}", flush=True)
            continue

        index_entry = {
            "link": link,
            "url": wikidot_url(link),
            "scp_id": article.get("scp_id", article_id),
            "title": title,
            "folder": article["folder"],
            "json_file": os.path.relpath(source_path, BASE_FOLDER).replace(
                os.sep, "/"
            ),
            "html_file": filename,
        }
        if article_id.casefold() != index_entry["scp_id"].casefold():
            index_entry["json_key"] = article_id
        partial_index[link] = index_entry

    return partial_index


def queue_missing_images(json_files):
    """Queue external images referenced by source JSON whose local copy is absent."""
    for filepath, _ in json_files:
        try:
            with open(filepath, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            emit_output(f"Error reading {filepath} while checking images: {error}", flush=True)
            continue

        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            html = entry.get("raw_content") or entry.get("raw_source", "")
            if not isinstance(html, str):
                continue
            soup = BeautifulSoup(html, "html.parser")
            url_map = build_image_url_map(soup)
            for image in soup.find_all("img"):
                image_url = resolve_image_url(image, url_map)
                if image_url and not os.path.isfile(
                    os.path.join(IMG_FOLDER, image_filename_from_url(image_url))
                ):
                    enqueue_image(image_url)


def index_entries_by_source(previous_index):
    entries_by_source = {}
    if not isinstance(previous_index, dict):
        return entries_by_source

    for metadata in previous_index.values():
        if not isinstance(metadata, dict):
            continue
        source_filename = metadata.get("json_file")
        if isinstance(source_filename, str):
            entries_by_source.setdefault(source_filename, []).append(metadata)
    return entries_by_source


def source_needs_processing(previous_index, source_filename, entries_by_source=None):
    if previous_index is None:
        return True
    if entries_by_source is None:
        entries_by_source = index_entries_by_source(previous_index)
    entries = entries_by_source.get(source_filename, [])
    if not entries:
        return True
    return any(
        not metadata.get("folder")
        or not metadata.get("html_file")
        or not os.path.isfile(
            os.path.join(
                HTML_FOLDER,
                metadata["folder"],
                metadata["html_file"],
            )
        )
        for metadata in entries
    )


def download_and_process_files(
    jobs,
    file_to_key,
    titles_by_reference,
    on_downloads_complete=None,
    download_media=False,
):
    downloaded_files = []
    changed_files = []
    partial_indexes = []
    download_completed = 0
    downloaded_by_filename = {}
    processed_filenames = set()
    process_completed = 0

    def report_source_progress(source_key, completed, total):
        nonlocal process_completed
        match = SERIES_FILE_RE.match(source_key)
        if not match:
            return
        series_number = float(match.group(1))
        start = max(0, (series_number - 1) * 10)
        end = min(100, series_number * 10)
        fraction = min(1, completed / total) if total else 1
        progress = min(100, int(start + (end - start) * fraction))
        if progress > process_completed:
            process_completed = progress
            emit_progress("Processing articles", process_completed, 100)

    cached_files = [
        (os.path.join(API_FOLDER, filename), source_key)
        for filename, source_key in file_to_key.items()
        if os.path.isfile(os.path.join(API_FOLDER, filename))
    ]
    download_jobs = sorted(jobs, key=download_order_key)
    emit_progress("Downloading special groups", 0, len(download_jobs))

    special_jobs = [
        job for job in download_jobs
        if os.path.basename(job[0]).casefold() in SPECIAL_GROUP_ORDER
    ]
    other_jobs = [job for job in download_jobs if job not in special_jobs]
    downloads_complete_notified = False

    def notify_downloads_complete():
        nonlocal downloads_complete_notified
        if downloads_complete_notified or on_downloads_complete is None:
            return
        downloads_complete_notified = True
        result = on_downloads_complete()
        if result is None:
            return
        path, needs_processing = result
        filename = os.path.basename(path)
        downloaded_by_filename[filename] = (path, "scp-001", needs_processing)
        downloaded_files.append((path, "scp-001"))
        if needs_processing:
            changed_files.append((path, "scp-001"))

    for batch_index, batch in enumerate((special_jobs, other_jobs)):
        if not batch:
            continue
        stage = (
            "Downloading special groups"
            if batch_index == 0
            else "Downloading series"
        )
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    download_json_file,
                    filename,
                    force_process=force_process,
                ): filename
                for filename, force_process in batch
            }
            if batch_index == 0:
                for future in as_completed(futures):
                    filename = futures[future]
                    result = future.result()
                    download_completed += 1
                    emit_progress(stage, download_completed, len(download_jobs))
                    if result is not None:
                        path, needs_processing = result
                        key = file_to_key[filename]
                        downloaded_by_filename[filename] = (path, key, needs_processing)
                        downloaded_files.append((path, key))
                        if needs_processing:
                            changed_files.append((path, key))
            else:
                ordered_series = [
                    filename
                    for filename, _ in sorted(batch, key=download_order_key)
                    if SERIES_FILE_RE.match(file_to_key[filename])
                ]
                completed_results = {}
                emit_progress("Processing articles", process_completed, 100)

                def process_ready_series(filename):
                    source = downloaded_by_filename.get(filename)
                    if source is None:
                        return
                    path, key, needs_processing = source
                    if not needs_processing:
                        return
                    try:
                        with open(path, encoding="utf-8") as file:
                            data = json.load(file)
                    except (OSError, json.JSONDecodeError) as error:
                        emit_output(f"Error reading {path}: {error}", flush=True)
                        return

                    article_ids = [
                        article_id
                        for article_id, entry in data.items()
                        if isinstance(entry, dict)
                    ]
                    article_count = len(article_ids)
                    available_files = [
                        (file_path, source_key)
                        for file_path, source_key, _ in downloaded_by_filename.values()
                    ]
                    articles_by_link, links_by_id = build_article_index(
                        list(dict.fromkeys(cached_files + available_files))
                    )
                    articles_processed = 0

                    def report_progress(article_id):
                        nonlocal articles_processed
                        articles_processed += 1
                        emit_article_progress(article_id)
                        report_source_progress(key, articles_processed, article_count)

                    partial_indexes.append(
                        process_json_file(
                            path,
                            key,
                            articles_by_link,
                            links_by_id,
                            titles_by_reference,
                            article_progress=report_progress,
                            download_media=download_media,
                            article_ids=article_ids,
                        )
                    )
                    processed_filenames.add(filename)

                next_series = 0
                for future in as_completed(futures):
                    filename = futures[future]
                    result = future.result()
                    download_completed += 1
                    emit_progress(stage, download_completed, len(download_jobs))
                    completed_results[filename] = result
                    if result is not None:
                        path, needs_processing = result
                        key = file_to_key[filename]
                        downloaded_by_filename[filename] = (path, key, needs_processing)
                        downloaded_files.append((path, key))
                        if needs_processing:
                            changed_files.append((path, key))

                    while (
                        next_series < len(ordered_series)
                        and ordered_series[next_series] in completed_results
                    ):
                        filename = ordered_series[next_series]
                        process_ready_series(filename)
                        report_source_progress(
                            file_to_key[filename], 1, 1
                        )
                        next_series += 1

    notify_downloads_complete()

    available_files = [
        (path, key) for path, key, _ in downloaded_by_filename.values()
    ]
    articles_by_link, links_by_id = build_article_index(
        list(dict.fromkeys(cached_files + available_files))
    )

    regular_batches = {}
    variant_articles = []
    for filename, (path, key, needs_processing) in downloaded_by_filename.items():
        if not needs_processing or filename in processed_filenames:
            continue
        try:
            with open(path, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            emit_output(f"Error reading {path}: {error}", flush=True)
            continue

        is_series = SERIES_FILE_RE.match(key) is not None
        regular_ids = []
        for article_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            resolved_id = entry.get("scp", article_id)
            match = ARTICLE_ID_RE.match(str(resolved_id))
            if not match:
                regular_batches.setdefault("other", []).append(
                    (path, key, article_id, entry)
                )
                continue
            number, suffix = match.groups()
            if suffix:
                variant_articles.append((path, key, article_id, entry))
            elif 1 <= int(number) <= 9999:
                group = series_for_scp_number(int(number))
                if is_series and group == key:
                    regular_ids.append(article_id)
                else:
                    regular_batches.setdefault(group, []).append(
                        (path, key, article_id, entry)
                    )
            else:
                regular_batches.setdefault("other", []).append(
                    (path, key, article_id, entry)
                )

        if regular_ids:
            regular_batches.setdefault(key, [])
            regular_batches[key].insert(
                0,
                (path, key, None, regular_ids),
            )

    ordered_groups = sorted(
        regular_batches,
        key=lambda group: (
            series_sort_key(group) if group != "other" else (2, float("inf"), group)
        ),
    )
    for group in ordered_groups:
        records = regular_batches[group]
        main_record = next(
            (record for record in records if record[2] is None),
            None,
        )
        if main_record is not None:
            path, key, _, article_ids = main_record
            additional_articles = [
                record for record in records if record[2] is not None
            ]
        else:
            path, key, _, _ = records[0]
            article_ids = []
            additional_articles = records

        group_article_count = len(article_ids) + len(additional_articles)
        group_articles_processed = 0

        def report_article_progress(article_id):
            nonlocal group_articles_processed
            group_articles_processed += 1
            emit_article_progress(article_id)
            report_source_progress(
                group, group_articles_processed, group_article_count
            )

        partial_indexes.append(
            process_json_file(
                path,
                key,
                articles_by_link,
                links_by_id,
                titles_by_reference,
                article_progress=report_article_progress,
                download_media=download_media,
                article_ids=article_ids,
                additional_articles=additional_articles,
            )
        )

    if variant_articles:
        variant_articles.sort(
            key=lambda record: (
                *article_sort_key(record[3].get("scp", record[2])),
                *article_source_sort_key(record[1]),
                str(record[2]).casefold(),
            )
        )
        path, key, _, _ = variant_articles[0]

        def report_variant_progress(article_id):
            emit_article_progress(article_id)

        partial_indexes.append(
            process_json_file(
                path,
                key,
                articles_by_link,
                links_by_id,
                titles_by_reference,
                article_progress=report_variant_progress,
                download_media=download_media,
                article_ids=[],
                additional_articles=variant_articles,
            )
        )

    if process_completed < 100:
        process_completed = 100
        emit_progress("Processing articles", process_completed, 100)

    return downloaded_files, changed_files, partial_indexes, processed_filenames


def update_archive(download_media=False):
    ensure_folder(BASE_FOLDER)
    ensure_folder(API_FOLDER)
    ensure_folder(JSON_FOLDER)
    ensure_folder(HTML_FOLDER)
    ensure_folder(IMG_FOLDER)
    download_queue.clear()
    with queue_lock:
        _enqueued_urls.clear()

    emit_output("Downloading content index...")
    try:
        content_index = requests.get(CONTENT_INDEX_URL, timeout=30).json()
    except Exception as e:
        emit_output(f"Error downloading content index: {e}")
        return 1

    file_to_key = {v: k for k, v in content_index.items()}
    emit_output("Loading official SCP titles...")
    titles_by_reference = fetch_series_titles()

    index_path = os.path.join(BASE_FOLDER, "index.json")
    try:
        with open(index_path, encoding="utf-8") as file:
            previous_index = json.load(file)
        if not isinstance(previous_index, dict):
            previous_index = None
    except (OSError, json.JSONDecodeError):
        previous_index = None
    previous_entries_by_source = index_entries_by_source(previous_index)
    force_process_by_file = {
        filename: source_needs_processing(
            previous_index,
            os.path.relpath(os.path.join(API_FOLDER, filename), BASE_FOLDER).replace(
                os.sep, "/"
            ),
            previous_entries_by_source,
        )
        for filename in content_index.values()
    }

    proposal_results = []

    def download_proposals():
        result = update_scp_001_json(
            force_process=source_needs_processing(
                previous_index,
                os.path.relpath(
                    os.path.join(JSON_FOLDER, "content_scp-001.json"),
                    BASE_FOLDER,
                ).replace(os.sep, "/"),
                previous_entries_by_source,
            )
        )
        proposal_results.append(result)
        return result

    try:
        (
            downloaded_files,
            changed_files,
            partial_indexes,
            processed_filenames,
        ) = download_and_process_files(
            [
                (filename, force_process_by_file[filename])
                for filename in content_index.values()
            ],
            file_to_key,
            titles_by_reference,
            on_downloads_complete=download_proposals,
            download_media=download_media,
        )
    except (requests.RequestException, ValueError, OSError) as error:
        emit_output(f"Error downloading SCP-001 proposals: {error}")
        return 1
    scp_001_path, scp_001_changed = proposal_results[0]
    scp_001_file = (scp_001_path, "scp-001")
    if scp_001_file not in downloaded_files:
        downloaded_files.append(scp_001_file)
    if scp_001_changed and "content_scp-001.json" not in processed_filenames:
        current_files = [
            (os.path.join(API_FOLDER, source_filename), source_key)
            for source_filename, source_key in file_to_key.items()
            if os.path.exists(os.path.join(API_FOLDER, source_filename))
        ]
        current_files.append(scp_001_file)
        articles_by_link, links_by_id = build_article_index(current_files)
        partial_indexes.append(
            process_json_file(
                scp_001_path,
                "scp-001",
                articles_by_link,
                links_by_id,
                titles_by_reference,
                article_progress=emit_article_progress,
                download_media=download_media,
            )
        )
        changed_files.append(scp_001_file)

    json_files = list(dict.fromkeys(downloaded_files))
    if download_media:
        queue_missing_images(json_files)

    articles_by_link, links_by_id = build_article_index(json_files)
    emit_output(f"Found {len(articles_by_link)} API links.")

    changed_sources = {
        os.path.relpath(path, BASE_FOLDER).replace(os.sep, "/")
        for path, _ in changed_files
    }
    unchanged_sources = {
        os.path.relpath(path, BASE_FOLDER).replace(os.sep, "/")
        for path, _ in json_files
        if os.path.relpath(path, BASE_FOLDER).replace(os.sep, "/")
        not in changed_sources
    }
    index = {}
    for identity, metadata in (previous_index or {}).items():
        if not isinstance(metadata, dict):
            continue
        json_file = metadata.get("json_file")
        if not isinstance(json_file, str) or json_file not in unchanged_sources:
            continue
        index[identity] = metadata
    for partial in partial_indexes:
        index.update(partial)

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        emit_output(f"Index generated: {index_path} ({len(index)} entries)")
    except Exception as e:
        emit_output(f"Error saving index: {e}")
        return 1

    if download_media:
        emit_output(f"Downloading images ({len(download_queue)} files)...", flush=True)
        run_parallel(
            list(download_queue),
            download_image,
            "Downloading images",
        )
    else:
        emit_output("Skipping image downloads (use --media to include them).")

    emit_output("All articles and resources have been processed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update the local SCP archive.")
    parser.add_argument(
        "--media",
        action="store_true",
        help="download article images during the update",
    )
    raise SystemExit(update_archive(download_media=parser.parse_args().media))
