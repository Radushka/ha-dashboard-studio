"""Constants for Dashboard Studio."""

DOMAIN = "hadash"
# Where the tablets and the studio are served: /hadash/index.html and /hadash/studio.html.
URL_BASE = "/hadash"
# Not "dashboard-studio": that address belongs to the hidden Lovelace dashboard that held layouts before.
PANEL = "hadash-studio"
STORAGE_KEY = "hadash.layout"
STORAGE_VERSION = 1
# The last published versions, so a publish can be taken back from the studio.
HISTORY_STORAGE_KEY = "hadash.history"
HISTORY_KEEP = 20
LICENSE_STORAGE_KEY = "hadash.license"
PRO = "hadash_pro"

# The integration's version, from its manifest. It is read once, when the module is imported.
import json as _json
from pathlib import Path as _Path

VERSION: str = _json.loads((_Path(__file__).parent / "manifest.json").read_text())["version"]
