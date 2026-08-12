import os, re, json, requests
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

MAX_WORKERS = 3  # cantidad de archivos que se procesan/descargan en simultaneo
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
SCP_HREF_RE = re.compile(r'^/scp-(\d+[\w-]*)', re.IGNORECASE)
SCP_MENTION_RE = re.compile(r'\b(SCP-\d{1,4})\b')
download_queue = []
queue_lock = Lock()
_enqueued_urls = set()


def ensure_folder(path):
    os.makedirs(path, exist_ok=True)


def run_parallel(items, worker_fn, desc):
    """Corre worker_fn sobre items con MAX_WORKERS threads y barra de progreso tqdm."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_fn, item): item for item in items}
        for future in tqdm(as_completed(futures), total=len(futures), desc=desc):
            results.append(future.result())
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
        tqdm.write(f"Error al descargar {filename}: {e}")
        return None


def image_filename_from_url(url):
    """Nombre de archivo local a partir de una URL de imagen. Usar solo el
    ultimo segmento no alcanza: muchas imagenes del dataset comparten el
    mismo nombre generico (ej. '.../scp-025/025.jpeg/medium.jpg', donde
    'medium.jpg' se repite en decenas de articulos distintos). Se arma el
    nombre con los ultimos segmentos del path para que sea unico."""
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
        tqdm.write(f"Error imagen {url}: {e}")


def build_image_url_map(soup):
    """Mapea nombre-de-archivo -> URL real, revisando TODOS los <a href> del
    documento que apunten a un archivo de imagen. El <img src> real muchas
    veces es una ruta relativa generica (ej. '../images/medium.jpg') y la
    URL real solo aparece en un link a otro lado del articulo (ej. la caja
    de licencia) o en el <a> que envuelve la imagen."""
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
        # avatares de wikidot (autor/historial): decorativos, con query string
        # distinto por usuario -> colisionan todos al mismo nombre de archivo
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
    """all_slugs: slug (con el casing real del dataset) -> carpeta/serie.
    norm_slugs: SLUG-EN-MAYUSCULA -> slug real, para poder matchear
    menciones sin depender de que el casing coincida exactamente."""
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
    """Procesa un archivo json de una serie: genera el HTML de cada articulo
    (con links entre SCPs, imagenes locales, sin auto-referencias) y
    devuelve el indice parcial {slug: {title, folder, json_file}}."""
    partial_index = {}
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        tqdm.write(f"Error leyendo {filepath}: {e}")
        return partial_index

    def find_slug_location(mention):
        """mention: texto tal como aparece (ej. 'SCP-999'). Devuelve
        (slug_real, ruta_relativa) o (None, None) si no lo tenemos local."""
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

        # quitar la caja de "‡ Licensing / Citation": es boilerplate repetido
        # en cada articulo y sus links plegables (javascript:;) no hacen nada aca
        for box in soup.find_all("div", class_="licensebox"):
            box.decompose()

        # --- imagenes: resolver la URL real (parent <a> o match por nombre) ---
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
                # auto-referencia (ej. caja de citado): texto plano, no clickeable
                a.replace_with(a.get_text())
                continue
            real_slug, rel_path = find_slug_location(mention)
            if real_slug:
                a["href"] = f"../{rel_path}"
            else:
                # no lo tenemos descargado localmente: dejar como link externo real
                a["href"] = f"https://scpwiki.com{a['href']}"

        # --- menciones sueltas en texto plano (nunca envueltas en <a>) ---
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
                # fragmento PARSEADO, no texto plano (si no, el <a> queda escapado)
                tag.replace_with(BeautifulSoup(new_html, "html.parser"))

        folder_path = os.path.join(HTML_FOLDER, subfolder_name)
        ensure_folder(folder_path)
        html_path = os.path.join(folder_path, f"{slug}.html")
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
        except Exception as e:
            tqdm.write(f"Error guardando HTML {slug}: {e}")
            continue

        partial_index[slug] = {
            "title": title,
            "folder": subfolder_name,
            "json_file": os.path.basename(filepath),
        }

    return partial_index


def main():
    ensure_folder(BASE_FOLDER)
    ensure_folder(JSON_FOLDER)
    ensure_folder(HTML_FOLDER)
    ensure_folder(IMG_FOLDER)

    print("Descargando indice de contenidos...")
    try:
        content_index = requests.get(CONTENT_INDEX_URL, timeout=30).json()
    except Exception as e:
        print(f"Error descargando indice: {e}")
        return

    file_to_key = {v: k for k, v in content_index.items()}

    downloaded_paths = run_parallel(
        list(content_index.values()), download_json_file, "Descargando series"
    )
    json_files = [
        (path, file_to_key.get(os.path.basename(path), "misc"))
        for path in downloaded_paths if path
    ]

    all_slugs, norm_slugs = build_slug_index(json_files)
    print(f"{len(all_slugs)} slugs encontrados. Procesando articulos...")

    partial_indexes = run_parallel(
        json_files,
        lambda item: process_json_file(item[0], item[1], all_slugs, norm_slugs),
        "Procesando articulos",
    )

    # el indice se genera aca, apenas estan los HTML listos, antes de bajar imagenes
    index = {}
    for partial in partial_indexes:
        index.update(partial)

    index_path = os.path.join(BASE_FOLDER, "index.json")
    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        print(f"Indice generado: {index_path} ({len(index)} entradas)")
    except Exception as e:
        print(f"Error guardando indice: {e}")

    print(f"Descargando imagenes ({len(download_queue)} archivos)...")
    run_parallel(list(download_queue), download_image, "Descargando imagenes")

    print("Todos los articulos y recursos fueron procesados.")


if __name__ == "__main__":
    main()
