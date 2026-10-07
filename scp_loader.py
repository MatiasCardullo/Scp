import argparse
import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from threading import Lock
from urllib.parse import quote, urljoin, urlparse

BASE_FOLDER = "scp_data"
JSON_FOLDER = os.path.join(BASE_FOLDER, "json")
HTML_FOLDER = os.path.join(BASE_FOLDER, "html")
IMG_FOLDER = os.path.join(BASE_FOLDER, "images")
BASE_JSON_URL = "https://scp-data.tedivm.com/data/scp/items/"
CONTENT_INDEX_URL = BASE_JSON_URL + "content_index.json"
WIKIDOT_BASE_URL = "https://scp-wiki.wikidot.com/"
SCP_ID_RE = re.compile(r"^SCP-\d+[\w-]*$", re.IGNORECASE)
SERIES_LINK_RE = re.compile(r"^/?(SCP-\d+[\w-]*)$", re.IGNORECASE)

MAX_WORKERS = 4  # Number of files processed/downloaded concurrently.
IMAGE_DOWNLOAD_ATTEMPTS = 3
IMAGE_REQUEST_HEADERS = {
    "User-Agent": "SCP-Terminal-Archive/1.0 (https://github.com/MatiasCardullo/Scp)"
}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
SCP_HREF_RE = re.compile(r'^/scp-(\d+[\w-]*)', re.IGNORECASE)
SCP_MENTION_RE = re.compile(r'\b(SCP-\d{1,4})\b')
output_lock = Lock()
download_queue = []
queue_lock = Lock()
_enqueued_urls = set()


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
    filepath = os.path.join(JSON_FOLDER, filename)
    url = BASE_JSON_URL + filename
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        remote_data = r.json()
        if os.path.exists(filepath):
            try:
                with open(filepath, encoding="utf-8") as existing_file:
                    if json.load(existing_file) == remote_data:
                        return filepath, force_process
            except (OSError, json.JSONDecodeError):
                pass
        temporary_path = filepath + ".tmp"
        try:
            with open(temporary_path, "wb") as file:
                file.write(r.content)
            os.replace(temporary_path, filepath)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        return filepath, True
    except Exception as e:
        emit_output(f"Error downloading {filename}: {e}", flush=True)
        return (filepath, False) if os.path.exists(filepath) else None


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

    for article_id, entry in data.items():
        if article_progress is not None:
            article_progress(article_id)
        link = entry.get("link")
        if not isinstance(link, str) or not link.strip():
            emit_output(
                f"Skipping {article_id} in {os.path.basename(filepath)}: "
                "missing API link"
            )
            continue
        link = link.strip()
        normalized_link = normalize_link(link)
        article = articles_by_link.get(normalized_link)
        if article is None:
            emit_output(f"Skipping {article_id}: API link was not indexed", flush=True)
            continue

        if subfolder_name.casefold() == "scp-001":
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
            "json_file": os.path.basename(filepath),
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


def source_needs_processing(previous_index, source_filename):
    if previous_index is None:
        return True
    entries = [
        metadata
        for metadata in previous_index.values()
        if isinstance(metadata, dict)
        and metadata.get("json_file") == source_filename
    ]
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
    process_completed = 0
    download_futures = {}
    process_futures = {}
    cached_files = [
        (os.path.join(JSON_FOLDER, filename), source_key)
        for filename, source_key in file_to_key.items()
        if os.path.isfile(os.path.join(JSON_FOLDER, filename))
    ]
    articles_by_link, links_by_id = build_article_index(cached_files)
    emit_progress("Downloading series", 0, len(jobs))
    emit_progress("Processing articles", 0, len(jobs))

    with (
        ThreadPoolExecutor(max_workers=MAX_WORKERS) as download_pool,
        ThreadPoolExecutor(max_workers=MAX_WORKERS) as process_pool,
    ):
        for filename, force_process in jobs:
            future = download_pool.submit(
                download_json_file, filename, force_process=force_process
            )
            download_futures[future] = filename

        downloads_complete_notified = False
        while download_futures or process_futures:
            completed, _ = wait(
                (*download_futures, *process_futures),
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                if future in download_futures:
                    filename = download_futures.pop(future)
                    result = future.result()
                    download_completed += 1
                    emit_progress("Downloading series", download_completed, len(jobs))

                    if result is None:
                        process_completed += 1
                    else:
                        path, needs_processing = result
                        key = file_to_key[filename]
                        downloaded_files.append((path, key))
                        file_articles, file_links_by_id = build_article_index(
                            [(path, key)]
                        )
                        articles_by_link.update(file_articles)
                        links_by_id.update(file_links_by_id)
                        if needs_processing:
                            process_future = process_pool.submit(
                                process_json_file,
                                path,
                                key,
                                dict(articles_by_link),
                                dict(links_by_id),
                                titles_by_reference,
                                article_progress=emit_article_progress,
                                download_media=download_media,
                            )
                            process_futures[process_future] = (path, key)
                            changed_files.append((path, key))
                        else:
                            process_completed += 1
                else:
                    process_futures.pop(future)
                    partial_indexes.append(future.result())
                    process_completed += 1

                emit_progress("Processing articles", process_completed, len(jobs))

            if not download_futures and not downloads_complete_notified:
                downloads_complete_notified = True
                if on_downloads_complete is not None:
                    on_downloads_complete()

        if not downloads_complete_notified and on_downloads_complete is not None:
            on_downloads_complete()

    return downloaded_files, changed_files, partial_indexes


def main(download_media=False):
    ensure_folder(BASE_FOLDER)
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
    force_process_by_file = {
        filename: source_needs_processing(previous_index, filename)
        for filename in content_index.values()
    }

    proposal_results = []

    def download_proposals():
        proposal_results.append(
            update_scp_001_json(
                force_process=source_needs_processing(
                    previous_index, "content_scp-001.json"
                ),
            )
        )

    try:
        downloaded_files, changed_files, partial_indexes = download_and_process_files(
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
    downloaded_files.append(scp_001_file)
    if scp_001_changed:
        current_files = [
            (os.path.join(JSON_FOLDER, source_filename), source_key)
            for source_filename, source_key in file_to_key.items()
            if os.path.exists(os.path.join(JSON_FOLDER, source_filename))
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

    changed_filenames = {os.path.basename(path) for path, _ in changed_files}
    unchanged_filenames = {
        os.path.basename(path)
        for path, _ in json_files
        if os.path.basename(path) not in changed_filenames
    }
    index = {
        identity: metadata
        for identity, metadata in (previous_index or {}).items()
        if isinstance(metadata, dict)
        and metadata.get("json_file") in unchanged_filenames
    }
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
    raise SystemExit(main(download_media=parser.parse_args().media))
