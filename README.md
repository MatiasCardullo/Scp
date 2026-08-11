# SCP Terminal Archive

Un mini-sistema de dos partes para descargar, catalogar y leer artículos de la [Wikidot SCP Foundation](http://www.scpwiki.com/) en formato local, con una interfaz retro tipo terminal.

```
📥 scp_loader.py   →  descarga y arma el archivo local (JSON + HTML + imágenes)
🖥️  scp_reader.py   →  terminal retro para buscar y leer los artículos descargados
```

---

## ¿Qué hace cada script?

### `scp_loader.py`
Descarga el dataset público de [scp-data.tedivm.com](https://scp-data.tedivm.com/) y genera un archivo local navegable:

- Descarga el índice de contenidos y todos los `.json` con los artículos (por serie/carpeta).
- Parsea el HTML de cada artículo con BeautifulSoup.
- Reemplaza las referencias tipo `SCP-###` por links internos hacia el artículo correspondiente.
- Descarga las imágenes referenciadas en paralelo (10 workers) y las guarda localmente, reescribiendo los `src`.
- Guarda todo en `scp_data/` con esta estructura:

```
scp_data/
├── json/       # JSONs originales descargados
├── html/       # HTML procesado, organizado por serie
│   └── series-1/
│       └── scp-173.html
└── images/     # Imágenes descargadas
```

### `scp_reader.py`
Una app de escritorio (PyQt5) con estética de terminal retro (fondo negro, texto verde, fuente monoespaciada) para navegar el archivo generado por `scp_loader.py`.

**Comandos disponibles:**

| Comando | Acción |
|---|---|
| `SCP-173` o `173` | Abre el artículo en una ventana aparte (render HTML) |
| `list` | Lista todos los títulos encontrados en `scp_data/` |
| `help` | Muestra la ayuda |
| `exit` / `quit` | Cierra la app |

---

## Requisitos

```bash
pip install requests beautifulsoup4 PyQt5 PyQtWebEngine
```

## Uso

```bash
# 1. Descargar y armar el archivo local (puede tardar varios minutos)
python scp_loader.py

# 2. Levantar el lector terminal
python scp_reader.py
```

---

## Notas / limitaciones actuales

- `scp_loader.py` no tiene reintentos automáticos ante fallos de red; si una descarga falla, salta ese archivo y sigue.
- `scp_reader.py` recorre todos los `.json` de `scp_data/` en cada búsqueda (`list`, `show_scp`) — funciona bien pero no escala si el archivo crece mucho (ver sugerencias abajo).
- Los links internos entre SCPs se arman durante la descarga, no al momento de leer.
