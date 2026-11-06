"""Where the screensaver clock can go on a photo: away from faces, and away from busy or bright areas.

Home Assistant looks at each photo once, on a small copy, and keeps the answer (in .storage), so the
slow tablets do no work for it. The answer is a "calm map": the photo cut into a grid of GRID × GRID
cells, each with a number from 0 (calm and dark enough: white text reads well) to 9 (a face). The tablet
knows how the photo sits on its screen (filled, in a pair, or whole in the middle), so it uses the map
to pick the clock's corner.

Faces are found with the classic Viola-Jones method and OpenCV's trained face cascades (frontal, and
profile both ways), done with numpy on an integral image. numpy comes with Home Assistant's own
image; when it is missing, the map still has the busy and bright areas, only no faces.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from io import BytesIO
import json
import logging
import math
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import PHOTO_VIEW, SPOTS, SPOTS_STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

GRID = 16
# The photo is looked at this many pixels on its long side. A face of 24 pixels is then about 1/20
# of the photo: smaller faces (a crowd far away) do not matter for a clock in a corner.
LOOK_SIDE = 480
FACES = Path(__file__).parent / "faces"
VERSION = 1


@lru_cache(maxsize=4)
def _cascade(name: str) -> dict[str, Any]:
    """A cascade, ready for numpy: per stage, the feature rectangles as flat arrays."""
    import numpy as np  # noqa: PLC0415

    data = json.loads((FACES / f"{name}.json").read_text())
    w, h = data["size"]
    feats = data["features"]
    stages = []
    for s in data["stages"]:
        weak = s["w"]
        # Every feature has 2 or 3 rectangles; a missing third one has weight 0.
        rects = np.zeros((len(weak), 3, 5))
        for i, (fi, _thr, _l, _r) in enumerate(weak):
            for j, r in enumerate(feats[fi]):
                rects[i, j] = r
        stages.append(
            {
                "t": s["t"],
                "rects": rects,
                "thr": np.array([c[1] for c in weak]),
                "left": np.array([c[2] for c in weak]),
                "right": np.array([c[3] for c in weak]),
            }
        )
    return {"w": w, "h": h, "stages": stages}


def _detect_level(np: Any, gray: Any, cascade: dict[str, Any], step: int) -> list[tuple[int, int]]:
    """Window positions on this image where the cascade sees a face (window = cascade size)."""
    H, W = gray.shape
    cw, ch = cascade["w"], cascade["h"]
    if W < cw + 2 or H < ch + 2:
        return []
    ii = np.zeros((H + 1, W + 1))
    ii[1:, 1:] = gray.cumsum(0).cumsum(1)
    sq = np.zeros((H + 1, W + 1))
    sq[1:, 1:] = (gray * gray).cumsum(0).cumsum(1)
    stride = W + 1
    ys, xs = np.mgrid[0 : H - ch : step, 0 : W - cw : step]
    base = (ys * stride + xs).ravel()
    flat, sqf = ii.ravel(), sq.ravel()

    def box(b: Any, x: Any, y: Any, w: Any, h: Any, img: Any) -> Any:
        o = y * stride + x
        return img[b + o] - img[b + o + w] - img[b + o + h * stride] + img[b + o + h * stride + w]

    # Normalize by the window's contrast (the inner window, one pixel in from each side, as OpenCV does).
    n = (cw - 2) * (ch - 2)
    s1 = box(base, 1, 1, cw - 2, ch - 2, flat)
    s2 = box(base, 1, 1, cw - 2, ch - 2, sqf)
    var = n * s2 - s1 * s1
    norm = np.where(var > 0, np.sqrt(np.maximum(var, 1e-9)), 1.0)
    sign = np.array([1.0, -1.0, -1.0, 1.0])
    for st in cascade["stages"]:
        if not base.size:
            return []
        r = st["rects"]
        if base.size * r.shape[0] < 20_000:
            # Few windows left: the whole stage at once (the four corners of every rectangle, every window).
            x, y, w, h, wt = (r[..., k].astype(np.int64) if k < 4 else r[..., k] for k in range(5))
            o = y * stride + x
            corners = np.stack([o, o + w, o + h * stride, o + h * stride + w], -1)
            sums = (flat[base[:, None, None, None] + corners[None]] * sign).sum(-1)
            v = (sums * wt[None]).sum(-1)
            total = np.where(v < st["thr"][None] * norm[:, None], st["left"][None], st["right"][None]).sum(-1)
        else:
            # Many windows: one weak classifier at a time, over all windows.
            total = np.zeros(base.size)
            for i in range(r.shape[0]):
                v = np.zeros(base.size)
                for j in range(3):
                    x, y, w, h, wt = r[i, j]
                    if wt:
                        v += wt * box(base, int(x), int(y), int(w), int(h), flat)
                total += np.where(v < st["thr"][i] * norm, st["left"][i], st["right"][i])
        keep = total >= st["t"]
        base, norm = base[keep], norm[keep]
    return [(int(b % stride), int(b // stride)) for b in base]


def _group(rects: list[tuple[float, float, float, float]], min_neighbors: int = 3, eps: float = 0.2) -> list[tuple[float, float, float, float]]:
    """Many hits around one face become one face; a lone hit is dropped (as OpenCV's groupRectangles)."""
    n = len(rects)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        x1, y1, w1, h1 = rects[i]
        for j in range(i + 1, n):
            x2, y2, w2, h2 = rects[j]
            d = eps * (min(w1, w2) + min(h1, h2)) * 0.5
            if abs(x1 - x2) <= d and abs(y1 - y2) <= d and abs(x1 + w1 - x2 - w2) <= d and abs(y1 + h1 - y2 - h2) <= d:
                parent[find(i)] = find(j)
    groups: dict[int, list[tuple[float, float, float, float]]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rects[i])
    out = []
    for g in groups.values():
        if len(g) <= min_neighbors:
            continue
        k = len(g)
        out.append((sum(r[0] for r in g) / k, sum(r[1] for r in g) / k, sum(r[2] for r in g) / k, sum(r[3] for r in g) / k))
    # A small face inside a bigger one is the same face.
    return [a for a in out if not any(b is not a and b[2] > a[2] and b[0] <= a[0] + a[2] * 0.2 and b[1] <= a[1] + a[3] * 0.2 and b[0] + b[2] >= a[0] + a[2] * 0.8 and b[1] + b[3] >= a[1] + a[3] * 0.8 for b in out)]


def find_faces(img: Any) -> list[tuple[float, float, float, float]]:
    """Faces in a Pillow image, as (x, y, w, h) in fractions of the image (0 to 1)."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image, ImageOps  # noqa: PLC0415

    gray_img = ImageOps.equalize(img.convert("L"))
    W0, H0 = gray_img.size
    found: list[tuple[float, float, float, float]] = []
    # Frontal faces; faces turned to the side (the profile cascade sees one side, so the photo is also
    # mirrored); and big eyes alone, for a close-up where the face is cut by the photo's edge.
    for name, flips, neighbors, min_side in (("frontal", (False,), 3, 0), ("profile", (False, True), 3, 0), ("eye", (False,), 5, 30)):
        cascade = _cascade(name)
        cw, ch = cascade["w"], cascade["h"]
        for flip in flips:
            src = gray_img.transpose(Image.Transpose.FLIP_LEFT_RIGHT) if flip else gray_img
            hits: list[tuple[float, float, float, float]] = []
            scale = max(1.0, min_side / cw)
            while W0 / scale >= cw + 2 and H0 / scale >= ch + 2:
                w, h = round(W0 / scale), round(H0 / scale)
                level = src if scale == 1.0 else src.resize((w, h), Image.Resampling.BILINEAR)
                gray = np.asarray(level, dtype=np.float64)
                for x, y in _detect_level(np, gray, cascade, 1 if scale > 2 else 2):
                    fx = (W0 - (x + cw) * scale) if flip else x * scale
                    hits.append((fx, y * scale, cw * scale, ch * scale))
                scale *= 1.2
            for x, y, w, h in _group(hits, neighbors):
                if name == "eye":
                    # The face around the eye: about three and a half eyes wide.
                    cx, cy, k = x + w / 2, y + h / 2, 3.5
                    x0, y0 = max(0.0, cx - w * k / 2), max(0.0, cy - h * k * 0.4)
                    x, y, w, h = x0, y0, min(W0, cx + w * k / 2) - x0, min(H0, cy + h * k * 0.6) - y0
                found.append((x, y, w, h))
    return [(x / W0, y / H0, w / W0, h / H0) for x, y, w, h in _group(found, 0, 0.3)]


def calm_map(data: bytes) -> dict[str, Any]:
    """The calm map of a photo (JPEG or other bytes Pillow opens), and its faces."""
    from PIL import Image, ImageFilter, ImageOps, ImageStat  # noqa: PLC0415

    with Image.open(BytesIO(data)) as raw:
        img = ImageOps.exif_transpose(raw).convert("RGB")
    k = LOOK_SIDE / max(img.size)
    if k < 1:
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.BILINEAR)
    try:
        faces = find_faces(img)
    except ImportError:
        faces = []
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    W, H = img.size
    cells: list[float] = []
    for gy in range(GRID):
        for gx in range(GRID):
            box = (gx * W // GRID, gy * H // GRID, max(gx * W // GRID + 1, (gx + 1) * W // GRID), max(gy * H // GRID + 1, (gy + 1) * H // GRID))
            detail = ImageStat.Stat(edges.crop(box)).mean[0]
            light = ImageStat.Stat(gray.crop(box)).mean[0]
            # Busy: lots of fine detail makes thin white numbers hard to read. Bright: white on white.
            busy = min(1.0, detail / 40)
            bright = max(0.0, (light - 150) / 105)
            cells.append(min(7.0, busy * 5 + bright * 4))
    # A face, with room around it, is 9. The body below it (and hair above) is a little less.
    for fx, fy, fw, fh in faces:
        for gy in range(GRID):
            for gx in range(GRID):
                cx0, cy0, cx1, cy1 = gx / GRID, gy / GRID, (gx + 1) / GRID, (gy + 1) / GRID
                pad = fw * 0.35
                if cx1 > fx - pad and cx0 < fx + fw + pad and cy1 > fy - fh * 0.4 and cy0 < fy + fh * 1.3:
                    cells[gy * GRID + gx] = 9
                elif cx1 > fx - fw * 0.6 and cx0 < fx + fw * 1.6 and cy0 < fy + fh * 4 and cy1 > fy:
                    cells[gy * GRID + gx] = max(cells[gy * GRID + gx], 7)
    return {
        "v": VERSION,
        "map": "".join(str(min(9, max(0, math.floor(c + 0.5)))) for c in cells),
        "faces": [[round(v, 3) for v in f] for f in faces],
    }


# ---- In Home Assistant: one check per photo, kept in .storage, asked for by the tablets. ----

# About 300 bytes per photo: 5000 photos is 1.5 MB.
KEEP = 5000


class SpotStore:
    """The calm maps of the photos, by media id. A photo is checked once, one photo at a time."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, SPOTS_STORAGE_KEY)
        self._maps: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any] | None]] = {}
        # One photo at a time: the check uses one CPU core for about a second, never all of them.
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._maps = {k: v for k, v in (data.get("maps") or {}).items() if v.get("v") == VERSION}

    def get(self, media_id: str) -> dict[str, Any] | None:
        return self._maps.get(media_id)

    @callback
    def async_prepare(self, media_id: str, body: bytes) -> asyncio.Future[dict[str, Any] | None]:
        """Start the check of a photo (its small copy), unless it is done or under way."""
        if media_id in self._pending:
            return self._pending[media_id]
        fut: asyncio.Future[dict[str, Any] | None] = self.hass.loop.create_future()
        if media_id in self._maps:
            fut.set_result(self._maps[media_id])
            return fut
        self._pending[media_id] = fut
        self.hass.async_create_background_task(self._check(media_id, body, fut), f"hadash photo spot {media_id[-40:]}")
        return fut

    async def _check(self, media_id: str, body: bytes, fut: asyncio.Future[dict[str, Any] | None]) -> None:
        result: dict[str, Any] | None = None
        try:
            async with self._lock:
                result = await self.hass.async_add_executor_job(calm_map, body)
            self._maps[media_id] = result
            while len(self._maps) > KEEP:
                self._maps.pop(next(iter(self._maps)))
            self._store.async_delay_save(lambda: {"maps": self._maps}, 30)
        except Exception as err:  # noqa: BLE001 - the clock then keeps its usual corner
            _LOGGER.debug("Calm map for %s failed: %s", media_id, err)
        finally:
            self._pending.pop(media_id, None)
            if not fut.done():
                fut.set_result(result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "hadash/photo_spot",
        vol.Required("media"): str,
        vol.Optional("width", default=1280): vol.All(int, vol.Range(min=64, max=2560)),
        vol.Optional("height", default=800): vol.All(int, vol.Range(min=64, max=2560)),
    }
)
@websocket_api.async_response
async def ws_photo_spot(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The calm map of one screensaver photo. It makes the photo's small copy first when needed,
    so asking for the next photo early also gets its copy ready."""
    media_id = msg["media"]
    store: SpotStore | None = hass.data.get(SPOTS)
    view = hass.data.get(PHOTO_VIEW)
    if store is None or view is None or not media_id.startswith("media-source://"):
        connection.send_result(msg["id"], None)
        return
    found = store.get(media_id)
    if found is None:
        try:
            await view.async_copy(hass, media_id, msg["width"], msg["height"])
            found = await store.async_prepare(media_id, b"")
        except Exception as err:  # noqa: BLE001 - no map: the clock keeps its usual corner
            _LOGGER.debug("Photo spot %s failed: %s", media_id, err)
    connection.send_result(msg["id"], found)
