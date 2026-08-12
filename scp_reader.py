from PyQt5.QtWidgets import (QMainWindow, QApplication, QWidget, QVBoxLayout,
                              QHBoxLayout, QPlainTextEdit, QLineEdit, QLabel)
from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QPainter, QColor
import re, sys, os, json
from bs4 import BeautifulSoup
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage

INDEX_PATH = os.path.join("scp_data", "index.json")
HTML_EXPORT_FOLDER = os.path.join("scp_data", "html")
LINK_SCHEME = "scp-ref:"  # esquema custom para interceptar clicks internos

# matchea el formato de href que genera scp_loader.py para links entre
# articulos ya procesados: '../carpeta/SLUG.html'
INTERNAL_HREF_RE = re.compile(r'^\.\./([^/]+)/([^/]+)\.html$', re.IGNORECASE)

# CSS inyectado en cada artículo para que la ventana del visor mantenga
# la estética terminal (fondo negro, texto verde) en vez del blanco por defecto.
ARTICLE_THEME = """
<style>
  body {
    background:#0a0a0a;
    color:#33ff33;
    font-family:'Courier New', monospace;
    line-height:1.6;
    padding:24px;
  }
  a { color:#8dff8d; cursor:pointer; }
  a:visited { color:#5fcf5f; }
  a.scp-ref { text-decoration: underline dotted; }
  img { max-width:100%; filter:saturate(0.7); }
  hr { border-color:#1f4d1f; }
  ::selection { background:#145214; color:#eaffea; }
  table, td, th { border-color:#1f4d1f; }
</style>
"""


def rewrite_internal_links(html):
    """Convierte los href relativos '../carpeta/SLUG.html' que ya vienen
    armados por scp_loader.py al esquema scp-ref: para que SCPLinkPage
    los intercepte y abra el articulo dentro de la app."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        m = INTERNAL_HREF_RE.match(a["href"].strip())
        if m:
            slug = m.group(2)
            a["href"] = f"{LINK_SCHEME}{slug}"
    return str(soup)


def linkify_scp_refs(html, known_slugs, current_slug=None):
    """Fallback para cuando todavia no existe el HTML pre-procesado por el
    loader: busca menciones tipo 'SCP-173' en el texto y las convierte en
    links clickeables (esquema scp-ref:) hacia artículos que SÍ tenemos
    indexados localmente. No linkea menciones al propio artículo leído."""
    soup = BeautifulSoup(html, "html.parser")

    def replace(text):
        def sub(m):
            slug = m.group(1).upper()
            if slug in known_slugs and slug != current_slug:
                return f'<a class="scp-ref" href="{LINK_SCHEME}{slug}">{m.group(1)}</a>'
            return m.group(1)
        return re.sub(r'\b(SCP-\d{1,4})\b', sub, text)

    for tag in soup.find_all(string=True):
        if tag.parent.name in ("script", "style", "a"):
            continue
        new_html = replace(str(tag))
        if new_html != str(tag):
            tag.replace_with(BeautifulSoup(new_html, "html.parser"))

    return str(soup)


class SCPLinkPage(QWebEnginePage):
    """QWebEnginePage que intercepta clicks a links internos (scp-ref:SLUG)
    y los redirige a un callback en vez de intentar navegar de verdad."""
    def __init__(self, open_callback, parent=None):
        super().__init__(parent)
        self.open_callback = open_callback

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        url_str = url.toString()
        if url_str.startswith(LINK_SCHEME):
            slug = url_str[len(LINK_SCHEME):]
            if self.open_callback:
                self.open_callback(slug)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class SCPViewer(QMainWindow):
    def __init__(self, title, html, known_slugs=None, open_callback=None,
                 current_slug=None, base_dir=None):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(800, 600)

        if known_slugs:
            # fallback en vivo: no habia HTML pre-procesado
            html = linkify_scp_refs(html, known_slugs, current_slug=current_slug)

        view = QWebEngineView()
        if open_callback:
            view.setPage(SCPLinkPage(open_callback, view))

        # base_dir: para que las imagenes relativas ('../images/x.jpg') del
        # HTML pre-procesado por el loader carguen solas desde disco
        base_url = QUrl.fromLocalFile(base_dir + os.sep) if base_dir else QUrl()
        view.setHtml(ARTICLE_THEME + html, baseUrl=base_url)
        self.setCentralWidget(view)

class HistoryLineEdit(QLineEdit):
    """QLineEdit con historial de comandos navegable con ↑ / ↓, estilo shell."""
    def __init__(self):
        super().__init__()
        self.history = []
        self.history_index = -1  # -1 = no estamos navegando historial
        self._draft = ""  # lo que se estaba tipeando antes de subir al historial

    def push_history(self, cmd):
        if cmd and (not self.history or self.history[-1] != cmd):
            self.history.append(cmd)
        self.history_index = -1
        self._draft = ""

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Up:
            if self.history:
                if self.history_index == -1:
                    self._draft = self.text()
                    self.history_index = len(self.history) - 1
                elif self.history_index > 0:
                    self.history_index -= 1
                self.setText(self.history[self.history_index])
                self.end(False)
            return
        elif event.key() == Qt.Key_Down:
            if self.history_index != -1:
                if self.history_index < len(self.history) - 1:
                    self.history_index += 1
                    self.setText(self.history[self.history_index])
                else:
                    self.history_index = -1
                    self.setText(self._draft)
                self.end(False)
            return
        super().keyPressEvent(event)


class ScanlineOverlay(QWidget):
    """Overlay transparente con líneas horizontales tenues, tipo monitor CRT.
    No intercepta clicks ni foco: solo se dibuja encima."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)

    def paintEvent(self, event):
        painter = QPainter(self)
        line_color = QColor(0, 0, 0, 45)
        painter.setPen(line_color)
        for y in range(0, self.height(), 3):
            painter.drawLine(0, y, self.width(), y)
        # leve viñeta en los bordes
        vignette = QColor(0, 0, 0, 60)
        painter.setPen(vignette)
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)


class SCPTextArea(QPlainTextEdit):
    def __init__(self, input_field):
        super().__init__()
        self.input_field = input_field

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        cursor = self.textCursor()
        if not cursor.hasSelection():
            # si no hay selección, devolver foco al input
            self.input_field.setFocus()

class TerminalEmu(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SCP Terminal Reader")
        self.resize(800, 600)

        # Estilos retro
        self.setStyleSheet("""
            QWidget {
                background-color: black;
                color: lime;
                font-family: 'Courier New', monospace;
                font-size: 14px;
            }
            QPlainTextEdit, QLineEdit {
                background-color: black;
                color: lime;
                border: none;
                padding: 0px;
                margin: 0px;
            }
            QLabel#prompt {
                color: lime;
                padding-right: 4px;
            }
        """)

        self.prompt_label = QLabel("SCP-OS>")
        self.prompt_label.setObjectName("prompt")

        self.input = HistoryLineEdit()
        self.output = SCPTextArea(self.input)
        self.output.setReadOnly(True)

        self.input.returnPressed.connect(self.handle_command)

        # --- efecto de tipeo letra por letra (debe existir antes de cualquier append_output) ---
        self.output_queue = []
        self.pending_text = ""
        self.chars_per_tick = 1
        self.type_timer = QTimer(self)
        self.type_timer.setInterval(12)
        self.type_timer.timeout.connect(self._type_tick)

        self.index = self.load_index()
        self.open_windows = []  # mantiene vivas las ventanas de artículos abiertas

        input_row = QHBoxLayout()
        input_row.setContentsMargins(4, 2, 4, 4)
        input_row.setSpacing(0)
        input_row.addWidget(self.prompt_label)
        input_row.addWidget(self.input)

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.output)
        layout.addLayout(input_row)
        self.setLayout(layout)

        # Overlay de scanlines: se crea al final, encima de todo el layout
        self.scanlines = ScanlineOverlay(self)
        self.scanlines.setGeometry(self.rect())
        self.scanlines.raise_()
        self.scanlines.show()

        self.input.setFocus()  # foco inicial

        if self.index:
            self.append_output(f"Bienvenido al SCP Reader. {len(self.index)} artículos indexados. Escribí 'help' para comenzar.")
        else:
            self.append_output("Bienvenido al SCP Reader. ⚠ No se encontró scp_data/index.json — corré scp_loader.py primero.")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "scanlines"):
            self.scanlines.setGeometry(self.rect())

    def load_index(self):
        """Carga el índice liviano generado por scp_loader.py (slug -> title/folder/json_file).
        Si no existe (dataset viejo), devuelve None y se usa el fallback lento."""
        if not os.path.exists(INDEX_PATH):
            return None
        try:
            with open(INDEX_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.append_output(f"[index.json] error: {e}")
            return None

    def list_scps(self):
        if self.index:
            titles = sorted(v["title"] for v in self.index.values())
            self.append_output("\t\t".join(titles) if titles else "No se encontraron SCPs.")
            return

        # Fallback sin índice: recorre todos los json (lento)
        entries = []
        for root, dirs, files in os.walk("scp_data"):
            for fname in files:
                if not fname.endswith(".json"): continue
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    for key in data.keys():
                        if key.startswith("SCP-") and data[key].get("title"):
                            entries.append(data[key]["title"])
                except Exception as e:
                    self.append_output(f"[{fname}] error: {e}")
        self.append_output("\t\t".join(sorted(entries)) if entries else "No se encontraron SCPs.")

    def show_scp(self, slug):
        slug = slug.upper()

        if self.index:
            meta = self.index.get(slug)
            if not meta:
                self.append_output("SCP no encontrado.")
                return
            title = meta["title"]
            html_path = os.path.join(HTML_EXPORT_FOLDER, meta["folder"], f"{slug}.html")

            # preferir el HTML ya procesado por el loader (links e imagenes resueltos)
            if os.path.exists(html_path):
                try:
                    with open(html_path, encoding="utf-8") as f:
                        html = f.read()
                except Exception as e:
                    self.append_output(f"Error leyendo {html_path}: {e}")
                    return
                html = rewrite_internal_links(html)
                base_dir = os.path.dirname(os.path.abspath(html_path))
                self.setWindowTitle(f"{slug} - {title}")
                viewer = SCPViewer(
                    f"{slug} - {title}", html,
                    open_callback=self.show_scp,
                    base_dir=base_dir,
                )
                self.open_windows.append(viewer)
                viewer.show()
                self.append_output(f"{slug} abierto en ventana aparte.")
                return

            # fallback: todavia no se genero el HTML estatico para este articulo
            json_path = os.path.join("scp_data", "json", meta["json_file"])
            try:
                with open(json_path, encoding="utf-8") as f:
                    data = json.load(f)
                entry = data[slug]
            except Exception as e:
                self.append_output(f"Error leyendo {meta['json_file']}: {e}")
                return
            html = entry.get("raw_content") or entry.get("raw_source", "")
            self.setWindowTitle(f"{slug} - {title}")
            viewer = SCPViewer(
                f"{slug} - {title}", html,
                known_slugs=self.index.keys(),
                open_callback=self.show_scp,
                current_slug=slug,
            )
            self.open_windows.append(viewer)
            viewer.show()
            self.append_output(f"{slug} abierto en ventana aparte (sin HTML pre-procesado, corré scp_loader.py).")
            return

        # Fallback sin índice: recorre todos los json (lento)
        for root, dirs, files in os.walk("scp_data"):
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if slug in data:
                        entry = data[slug]
                        title = entry.get("title", slug)
                        html = entry.get("raw_content") or entry.get("raw_source", "")
                        self.setWindowTitle(f"{slug} - {title}")
                        viewer = SCPViewer(f"{slug} - {title}", html)
                        self.open_windows.append(viewer)
                        viewer.show()
                        self.append_output(f"{slug} abierto en ventana aparte.")
                        return
                except Exception as e:
                    self.append_output(f"[{fname}] error: {e}")
        self.append_output("SCP no encontrado.")
    
    def append_output(self, text):
        """Encola el texto para que se muestre con efecto de tipeo letra por letra."""
        self.output_queue.append(text)
        if not self.type_timer.isActive():
            self._start_next_typing()

    def _start_next_typing(self):
        if not self.output_queue:
            return
        text = self.output_queue.pop(0)
        if self.output.toPlainText():
            self.output.insertPlainText("\n")
        self.pending_text = text
        # velocidad adaptativa: los textos largos (ej. 'list') tipean varios
        # caracteres por tick para no volverse eterno, sin perder el efecto
        self.chars_per_tick = max(1, len(text) // 60)
        self.type_timer.start()

    def _type_tick(self):
        if not self.pending_text:
            self.type_timer.stop()
            self._start_next_typing()
            return
        chunk, self.pending_text = (
            self.pending_text[: self.chars_per_tick],
            self.pending_text[self.chars_per_tick :],
        )
        self.output.insertPlainText(chunk)
        scrollbar = self.output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def handle_command(self):
        cmd = self.input.text().strip()
        self.input.push_history(cmd)
        self.input.clear()
        self.append_output(f"> {cmd}")

        if cmd.lower() in ("exit", "quit"):
            self.append_output("Saliendo...")
            QApplication.quit()
        elif cmd.lower() == "help":
            self.append_output("Comandos:\n  SCP-###  - ver entrada\n  list     - listar archivos\n  exit     - salir")
        elif cmd.lower() == "list":
            self.list_scps()
        elif cmd.upper().startswith("SCP-"):
            self.show_scp(cmd)
        elif cmd.isdigit() and 1 <= int(cmd) <= 9999:
            self.show_scp(f"SCP-{int(cmd):03}")
        else:
            self.append_output("Comando no reconocido. Escribí 'help'.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TerminalEmu()
    window.show()
    sys.exit(app.exec_())
