from PyQt5.QtWidgets import QMainWindow, QApplication, QWidget, QVBoxLayout, QPlainTextEdit, QLineEdit
import sys, os, json
from bs4 import BeautifulSoup
from PyQt5.QtWebEngineWidgets import QWebEngineView

class SCPViewer(QMainWindow):
    def __init__(self, title, html):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(800, 600)

        view = QWebEngineView()
        view.setHtml(html)
        self.setCentralWidget(view)

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

        self.input = QLineEdit()
        self.output = SCPTextArea(self.input)
        self.output.setReadOnly(True)

        self.input.returnPressed.connect(self.handle_command)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.output)
        layout.addWidget(self.input)
        self.setLayout(layout)

        self.input.setFocus()  # foco inicial
        self.append_output("Bienvenido al SCP Reader. Escribí 'help' para comenzar.\n>")

    def list_scps(self):
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
                            title = data[key]["title"]
                            entries.append(title)
                except Exception as e:
                    self.append_output(f"[{fname}] error: {e}")
        if entries:
            self.append_output("\t\t".join(sorted(entries)))
        else:
            self.append_output("No se encontraron SCPs.")

    def show_scp(self, slug):
        found = False
        slug=slug.upper()
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
                        self.setWindowTitle(f"{slug.upper()} - {title}")
                        
                        self.web_window = SCPViewer(f"{slug.upper()} - {title}", html)
                        self.web_window.show()

                        self.append_output(f"{slug} abierto en ventana aparte.")
                        found = True
                        return
                except Exception as e:
                    self.append_output(f"[{fname}] error: {e}")
        if not found:
            self.append_output("SCP no encontrado.")
    
    def append_output(self, text):
        self.output.appendPlainText(text)

    def handle_command(self):
        cmd = self.input.text().strip()
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
