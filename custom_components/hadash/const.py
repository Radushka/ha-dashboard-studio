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
# The studio's unpublished changes, shared with an AI assistant (draft.py).
DRAFT_STORAGE_KEY = "hadash.draft"
DRAFT = "hadash_draft"
PRO = "hadash_pro"
# The screensaver: the photo view (its small copies) and where the clock may go on each photo (spots.py).
PHOTO_VIEW = "hadash_photo_view"
SPOTS = "hadash_spots"
SPOTS_STORAGE_KEY = "hadash.photo_spots"
# Home on TV: the Fire TVs set up from the studio (firetv.py).
TVS = "hadash_tvs"
TVS_STORAGE_KEY = "hadash.tvs"
# News: read feeds and their pictures, in memory (news.py).
NEWS = "hadash_news"

# The integration's version, from its manifest. It is read once, when the module is imported.
import json as _json
from pathlib import Path as _Path

VERSION: str = _json.loads((_Path(__file__).parent / "manifest.json").read_text())["version"]

# The built tablet and studio files.
WWW = _Path(__file__).parent / "www"

# Auto-update: the switch, and what the last night's look found (autoupdate.py).
AUTO = "hadash_auto_update"
AUTO_STORAGE_KEY = "hadash.auto_update"
