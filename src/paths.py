"""Single source of truth for repo-root assets.

The Python modules live in ``src/`` but the assets they read/write (the corpus,
the bundled web files, model weights, the stub index, the .env) live at the REPO
ROOT (``src/``'s parent). Anchor every such path here so a move of the code never
silently breaks an asset lookup.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent   # repo root = src/'s parent
DATA = REPO / "data"
WEB = REPO / "web"
MODELS = REPO / "models"
INDEX_JSON = REPO / "index.json"
ENV_FILE = REPO / ".env"
