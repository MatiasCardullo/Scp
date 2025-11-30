import os, re, json, requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

BASE_FOLDER = "scp_data"
JSON_FOLDER = os.path.join(BASE_FOLDER, "json")
HTML_FOLDER = os.path.join(BASE_FOLDER, "html")
IMG_FOLDER = os.path.join(BASE_FOLDER, "images")
BASE_JSON_URL = "https://scp-data.tedivm.com/data/scp/items/"
CONTENT_INDEX_URL = BASE_JSON_URL + "content_index.json"

download_queue = []
queue_lock = Lock()

def ensure_folder(path):
    os.makedirs(path, exist_ok=True)

def download_json_file(filename):
    filepath = os.path.join(JSON_FOLDER, filename)
    if os.path.exists(filepath):
        print(f"✔ {filename} ya existe. Saltando.")
        return filepath
    url = BASE_JSON_URL + filename
    print(f"⬇ Descargando {filename}...")
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(r.content)
        print(f"✅ Guardado: {filepath}")
        return filepath
    except Exception as e:
        print(f"❌ Error al descargar {filename}: {e}")
        return None

def enqueue_image(url):
    if not url.startswith("http"):
        return url
    filename = re.sub(r'[^\w\-_\.]', '_', url.split("/")[-1])
    local_path = os.path.join(IMG_FOLDER, filename)
    with queue_lock:
        download_queue.append((url, local_path))
    return f"../images/{filename}"

def image_downloader_worker():
    while True:
        with queue_lock:
            if not download_queue:
                return
            url, path = download_queue.pop()
        try:
            if os.path.exists(path):
                continue
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"\r🖼️ Imagen descargada: {url}")
        except Exception as e:
            print(f"❌ Error imagen {url}: {e}\n")

def process_json_file(filepath, subfolder_name, all_slugs):
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Error leyendo {filepath}: {e}")
        return

    for slug, entry in data.items():
        title = entry.get("title", slug)
        html = entry.get("raw_content") or entry.get("raw_source", "")
        soup = BeautifulSoup(html, "html.parser")

        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                new_src = enqueue_image(src)
                img["src"] = new_src

        def link_scp_refs(text):
            return re.sub(
                r'\b(SCP-\d{1,4})\b',
                lambda m: f'<a href="../{find_slug_location(m.group(1).lower())}">{m.group(1)}</a>'
                if m.group(1).lower() in all_slugs else m.group(1),
                text
            )

        for tag in soup.find_all(text=True):
            if tag.parent.name not in ["script", "style"]:
                tag.replace_with(link_scp_refs(tag))

        folder_path = os.path.join(HTML_FOLDER, subfolder_name)
        ensure_folder(folder_path)
        html_path = os.path.join(folder_path, f"{slug}.html")
        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"\r📝 Generado: {html_path}          ", end='', flush=True)
        except Exception as e:
            print(f"❌ Error guardando HTML {slug}: {e}")

def main():
    ensure_folder(BASE_FOLDER)
    ensure_folder(JSON_FOLDER)
    ensure_folder(HTML_FOLDER)
    ensure_folder(IMG_FOLDER)

    print("📥 Descargando índice...")
    try:
        content_index = requests.get(CONTENT_INDEX_URL, timeout=30).json()
    except Exception as e:
        print(f"❌ Error descargando índice: {e}")
        return

    file_to_key = {v: k for k, v in content_index.items()}

    json_files = []
    for filename in content_index.values():
        path = download_json_file(filename)
        if path:
            json_files.append((path, file_to_key.get(filename, "misc")))

    all_slugs = {}
    for path, key in json_files:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for slug in data.keys():
                all_slugs[slug] = key
        except:
            pass

    global find_slug_location
    def find_slug_location(slug):
        serie = all_slugs.get(slug)
        if serie:
            return f"{serie}/{slug}.html"
        return f"{slug}.html"

    print(f"🔍 {len(all_slugs)} slugs encontrados. Procesando artículos...")

    for path, key in json_files:
        process_json_file(path, key, all_slugs)

    print(f"🧵 Iniciando descarga de imágenes ({len(download_queue)} archivos)...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(image_downloader_worker) for _ in range(10)]
        for future in as_completed(futures):
            pass

    print("✅ ¡Todos los artículos y recursos fueron procesados!")

if __name__ == "__main__":
    main()
