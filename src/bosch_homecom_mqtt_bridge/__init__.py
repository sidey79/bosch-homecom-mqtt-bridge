"""Bridge between the Bosch HomeCom Easy cloud and MQTT."""
from pathlib import Path

# VERSION sits next to src/ both in the repository and in the image (/app).
_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"

__version__ = _VERSION_FILE.read_text(encoding="utf-8").strip()
