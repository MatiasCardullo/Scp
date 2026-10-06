import argparse
import json
import os
import re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urlparse
from tqdm import tqdm

BASE_FOLDER = "scp_data"
JSON_FOLDER = os.path.join(BASE_FOLDER, "json")
HTML_FOLDER = os.path.join(BASE_FOLDER, "html")
IMG_FOLDER = os.path.join(BASE_FOLDER, "images")
BASE_JSON_URL = "https://scp-data.tedivm.com/data/scp/items/"
CONTENT_INDEX_URL = BASE_JSON_URL + "content_index.json"

MAX_WORKERS = 4  # Number of files processed/downloaded concurrently.
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
SCP_HREF_RE = re.compile(r'^/scp-(\d+[\w-]*)', re.IGNORECASE)
SCP_MENTION_RE = re.compile(r'\b(SCP-\d{1,4})\b')
download_queue = []
queue_lock = Lock()
_enqueued_urls = set()


def ensure_folder(path):
    os.makedirs(path, exist_ok=True)


def emit_progress(desc, completed, total):
    print(
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


def run_parallel(items, worker_fn, desc, line_progress=False):
    """Run worker_fn concurrently, with tqdm or line-oriented progress output."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_fn, item): item for item in items}
        completed = 0
        last_percent = -1
        if line_progress:
            emit_progress(desc, 0, len(futures))
            futures_iter = as_completed(futures)
        else:
            futures_iter = tqdm(as_completed(futures), total=len(futures), desc=desc)
        for future in futures_iter:
            results.append(future.result())
            if line_progress:
                completed += 1
                percent = int(completed * 100 / len(futures)) if futures else 100
                if percent != last_percent:
                    emit_progress(desc, completed, len(futures))
                    last_percent = percent
    return results


def download_json_file(filename):
    filepath = os.path.join(JSON_FOLDER, filename)
    if os.path.exists(filepath):
        return filepath
    url = BASE_JSON_URL + filename
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(r.content)
        return filepath
    except Exception as e:
        tqdm.write(f"Error downloading {filename}: {e}")
        return None


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
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    except Exception as e:
        tqdm.write(f"Image error for {url}: {e}")


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


def build_slug_index(json_files):
    """all_slugs maps each slug (with its original dataset casing) to its
    folder/series. norm_slugs maps uppercase slugs to their original casing so
    mentions can be matched regardless of their capitalization."""
    all_slugs = {}
    for path, key in json_files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for slug in data.keys():
                all_slugs[slug] = key
        except Exception:
            pass
    norm_slugs = {s.upper(): s for s in all_slugs}
    return all_slugs, norm_slugs


def process_json_file(filepath, subfolder_name, all_slugs, norm_slugs):
    """Process a series JSON file: generate HTML for each article (with links
    between SCPs, local images, and no self-references) and return its partial
    index in the form {slug: {title, folder, json_file}}."""
    partial_index = {}
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        tqdm.write(f"Error reading {filepath}: {e}")
        return partial_index

    def find_slug_location(mention):
        """Given a mention as it appears in the text (e.g. 'SCP-999'), return
        (real_slug, relative_path), or (None, None) if it is not available locally."""
        real_slug = norm_slugs.get(mention.upper())
        if not real_slug:
            return None, None
        folder = all_slugs.get(real_slug)
        return real_slug, f"{folder}/{real_slug}.html"

    for slug, entry in data.items():
        title = entry.get("title", slug)
        html = entry.get("raw_content") or entry.get("raw_source", "")
        soup = BeautifulSoup(html, "html.parser")
        own_slug_upper = slug.upper()

        # Remove the "‡ Licensing / Citation" box, which is repeated boilerplate;
        # its collapsible links (javascript:;) do not work here.
        for box in soup.find_all("div", class_="licensebox"):
            box.decompose()

        # --- Images: resolve the real URL (parent <a> or filename match). ---
        url_map = build_image_url_map(soup)
        for img in soup.find_all("img"):
            real_url = resolve_image_url(img, url_map)
            if real_url:
                img["src"] = enqueue_image(real_url)

        # --- links ya existentes hacia otros SCP (<a href="/scp-025">) ---
        for a in soup.find_all("a", href=True):
            m = SCP_HREF_RE.match(a["href"].strip())
            if not m:
                continue
            mention = f"SCP-{m.group(1)}"
            if mention.upper() == own_slug_upper:
                # Self-reference (e.g. citation box): plain, non-clickable text.
                a.replace_with(a.get_text())
                continue
            real_slug, rel_path = find_slug_location(mention)
            if real_slug:
                a["href"] = f"../{rel_path}"
            else:
                # Not available locally: keep it as a real external link.
                a["href"] = f"https://scpwiki.com{a['href']}"

        # --- Standalone mentions in plain text (never wrapped in an <a>). ---
        def link_scp_refs(text):
            def sub(m):
                mention = m.group(1)
                if mention.upper() == own_slug_upper:
                    return mention
                real_slug, rel_path = find_slug_location(mention)
                if real_slug:
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

        folder_path = os.path.join(HTML_FOLDER, subfolder_name)
        ensure_folder(folder_path)
        html_path = os.path.join(folder_path, f"{slug}.html")
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
        except Exception as e:
            tqdm.write(f"Error saving HTML for {slug}: {e}")
            continue

        partial_index[slug] = {
            "title": title,
            "folder": subfolder_name,
            "json_file": os.path.basename(filepath),
        }

    return partial_index


def main(textual_progress=False):
    ensure_folder(BASE_FOLDER)
    ensure_folder(JSON_FOLDER)
    ensure_folder(HTML_FOLDER)
    ensure_folder(IMG_FOLDER)

    print("Downloading content index...")
    try:
        content_index = requests.get(CONTENT_INDEX_URL, timeout=30).json()
    except Exception as e:
        print(f"Error downloading content index: {e}")
        return 1

    file_to_key = {v: k for k, v in content_index.items()}

    downloaded_paths = run_parallel(
        list(content_index.values()),
        download_json_file,
        "Downloading series",
        line_progress=textual_progress,
    )
    json_files = [
        (path, file_to_key.get(os.path.basename(path), "misc"))
        for path in downloaded_paths if path
    ]

    all_slugs, norm_slugs = build_slug_index(json_files)
    print(f"Found {len(all_slugs)} slugs. Processing articles...")

    partial_indexes = run_parallel(
        json_files,
        lambda item: process_json_file(item[0], item[1], all_slugs, norm_slugs),
        "Processing articles",
        line_progress=textual_progress,
    )

    # Generate the index as soon as the HTML is ready, before downloading images.
    index = {}
    for partial in partial_indexes:
        index.update(partial)

    index_path = os.path.join(BASE_FOLDER, "index.json")
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        print(f"Index generated: {index_path} ({len(index)} entries)")
    except Exception as e:
        print(f"Error saving index: {e}")
        return 1

    print(f"Downloading images ({len(download_queue)} files)...", flush=True)
    run_parallel(
        list(download_queue),
        download_image,
        "Downloading images",
        line_progress=textual_progress,
    )

    print("All articles and resources have been processed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--textual-progress",
        action="store_true",
        help="Emit line-oriented JSON progress events for a Textual interface.",
    )
    args = parser.parse_args()
    raise SystemExit(main(textual_progress=args.textual_progress))
