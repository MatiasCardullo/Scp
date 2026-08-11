from PyQt5.QtWidgets import QMainWindow, QApplication, QWidget, QVBoxLayout, QPlainTextEdit, QLineEdit
from PyQt5.QtCore import Qt
import sys, os, json
from bs4 import BeautifulSoup
from PyQt5.QtWebEngineWidgets import QWebEngineView

INDEX_PATH = os.path.join("scp_data", "index.json")

class SCPViewer(QMainWindow):
    def __init__(self, title, html):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(800, 600)

        view = QWebEngineView()
        view.setHtml(html)
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
        """)

        self.input = HistoryLineEdit()
        self.output = SCPTextArea(self.input)
        self.output.setReadOnly(True)

        self.input.returnPressed.connect(self.handle_command)

        self.index = self.load_index()

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.output)
        layout.addWidget(self.input)
        self.setLayout(layout)

        self.input.setFocus()  # foco inicial

        if self.index:
            self.append_output(f"Bienvenido al SCP Reader. {len(self.index)} artículos indexados. Escribí 'help' para comenzar.\n>")
        else:
            self.append_output("Bienvenido al SCP Reader. ⚠ No se encontró scp_data/index.json — corré scp_loader.py primero.\n>")

    def load_index(self):
        """Carga el índice liviano generado por scp_loader.py (slug -> title/folder/json_file).
        Si no existe (dataset viejo), devuelve None y se usa el fallback lento."""
        if not os.path.exists(INDEX_PATH):
            return None
        try:
            with open(INDEX_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.append_output(f"[index.json] error: {e}") if hasattr(self, "output") else None
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
            json_path = os.path.join("scp_data", "json", meta["json_file"])
            try:
                with open(json_path, encoding="utf-8") as f:
                    data = json.load(f)
                entry = data[slug]
            except Exception as e:
                self.append_output(f"Error leyendo {meta['json_file']}: {e}")
                return
            title = entry.get("title", slug)
            html = entry.get("raw_content") or entry.get("raw_source", "")
            self.setWindowTitle(f"{slug} - {title}")
            self.web_window = SCPViewer(f"{slug} - {title}", html)
            self.web_window.show()
            self.append_output(f"{slug} abierto en ventana aparte.")
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
                        self.web_window = SCPViewer(f"{slug} - {title}", html)
                        self.web_window.show()
                        self.append_output(f"{slug} abierto en ventana aparte.")
                        return
                except Exception as e:
                    self.append_output(f"[{fname}] error: {e}")
        self.append_output("SCP no encontrado.")
    
    def append_output(self, text):
        self.output.appendPlainText(text)

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
