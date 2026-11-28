"""Kitchen helper (Pro): share a recipe from your phone, and the kitchen tablet cooks with you, one step at a time.

Share any recipe page to the Home Assistant app (the same share sheet as Send to screen). Almost every recipe site
describes its recipe for search engines (schema.org "Recipe" in the page), so Home Assistant reads that: the name,
the ingredients and the steps. No account, no app, no AI. The recipe goes to the screen in the kitchen (the screen
whose room is a kitchen, else the one named in the share box, else the first tablet), which shows one step at a
time in large letters (renderer views/CookView.svelte). "Next", "back", a wave of the hand, or a touch moves on;
a step that says "bake for 25 minutes" starts its own timer.

A shared link that is not a recipe is left alone, for the person's own automations.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

LD_JSON = re.compile(r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
TAGS = re.compile(r"<[^>]+>")
DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?)?$", re.I)
KITCHEN = re.compile(r"kitchen|cuisine|küche|kuche|cocina|кухн", re.I)
MAX_BYTES = 3_000_000
MAX_ITEMS = 60
MAX_TEXT = 600

TEXT = {
    "shown": {
        "en": "The recipe is on {where}.",
        "fr": "La recette est sur {where}.",
        "de": "Das Rezept ist auf {where}.",
        "es": "La receta está en {where}.",
    },
    "off": {
        "en": "{where} is not on right now. Turn it on, then share the recipe again.",
        "fr": "{where} n'est pas allumé. Allumez-le, puis partagez la recette à nouveau.",
        "de": "{where} ist gerade nicht an. Schalten Sie es ein und teilen Sie das Rezept noch einmal.",
        "es": "{where} no está encendida. Enciéndala y comparta la receta otra vez.",
    },
    "title": {"en": "Kitchen helper", "fr": "Aide en cuisine", "de": "Küchenhilfe", "es": "Ayuda en la cocina"},
}


def _text(hass: HomeAssistant, key: str, **args: str) -> str:
    lang = (hass.config.language or "en")[:2]
    return TEXT[key].get(lang, TEXT[key]["en"]).format(**args)


def _clean(value: Any) -> str:
    s = html.unescape(TAGS.sub(" ", str(value or "")))
    return re.sub(r"\s+", " ", s).strip()[:MAX_TEXT]


def _minutes(value: Any) -> int | None:
    m = DURATION.match(str(value or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    total = d * 1440 + h * 60 + mi
    return total or None


def _is_recipe(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("@type")
    return kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind)


def _find(data: Any) -> dict[str, Any] | None:
    if _is_recipe(data):
        return data
    if isinstance(data, list):
        for x in data:
            if found := _find(x):
                return found
    if isinstance(data, dict):
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data and (found := _find(data[key])):
                return found
    return None


def _steps(value: Any, section: str = "") -> list[dict[str, str]]:
    """Every step, flat, with the name of its part ("For the sauce") when the recipe has parts."""
    out: list[dict[str, str]] = []
    if isinstance(value, str):
        # One block of text: its lines, or else its sentences in twos.
        lines = [x for x in (_clean(p) for p in re.split(r"\n+|<br\s*/?>|</p>", value)) if x]
        if len(lines) <= 1 and lines:
            sentences = re.split(r"(?<=[.!?])\s+", lines[0])
            lines = [" ".join(sentences[i : i + 2]) for i in range(0, len(sentences), 2)]
        return [{"text": x, **({"part": section} if section else {})} for x in lines]
    if isinstance(value, list):
        for x in value:
            out.extend(_steps(x, section))
        return out
    if isinstance(value, dict):
        kind = value.get("@type")
        if kind == "HowToSection" or (isinstance(kind, list) and "HowToSection" in kind):
            return _steps(value.get("itemListElement") or [], _clean(value.get("name")) or section)
        text = _clean(value.get("text") or value.get("name") or value.get("description"))
        if text:
            out.append({"text": text, **({"part": section} if section else {})})
    return out


def _image(value: Any) -> str:
    if isinstance(value, list) and value:
        return _image(value[0])
    if isinstance(value, dict):
        return str(value.get("url") or "")
    return str(value or "") if str(value or "").startswith("http") else ""


def parse(page: str, url: str = "") -> dict[str, Any] | None:
    """The recipe in a web page (schema.org Recipe in JSON-LD), or None."""
    for block in LD_JSON.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        found = _find(data)
        if not found:
            continue
        steps = _steps(found.get("recipeInstructions"))[:MAX_ITEMS]
        if not steps:
            continue
        raw = found.get("recipeIngredient") or found.get("ingredients") or []
        ingredients = [x for x in (_clean(i) for i in raw) if x][:MAX_ITEMS] if isinstance(raw, list) else []
        servings = found.get("recipeYield")
        if isinstance(servings, list):
            servings = servings[0] if servings else ""
        return {
            "name": _clean(found.get("name")) or "Recipe",
            "image": _image(found.get("image")),
            "servings": _clean(servings),
            "minutes": _minutes(found.get("totalTime")) or None,
            "ingredients": ingredients,
            "steps": steps,
            "url": url,
        }
    return None


async def fetch(hass: HomeAssistant, url: str) -> dict[str, Any] | None:
    """Reads the page and its recipe. None when it is not a recipe, or the site does not answer."""
    if not re.match(r"^https?://", url):
        return None
    try:
        async with async_get_clientsession(hass).get(
            url, timeout=12, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Mobile Safari/537.36", "Accept-Language": hass.config.language or "en"}
        ) as r:
            if r.status != 200 or "html" not in r.headers.get("Content-Type", "html"):
                return None
            raw = await r.content.read(MAX_BYTES)
    except Exception:  # noqa: BLE001 - a page that does not answer is simply not a recipe here
        _LOGGER.debug("Kitchen helper: could not read %s", url, exc_info=True)
        return None
    return parse(raw.decode("utf-8", "replace"), url)


def kitchen_screen(hass: HomeAssistant, screens: list[dict[str, Any]], entered: str, find: Any) -> dict[str, Any] | None:
    """Where the recipe goes: the screen named in the share box, else the kitchen's, else the first tablet that is on."""
    if entered and (named := find(entered)):
        return named
    from homeassistant.helpers import area_registry as ar

    from .follow import FOLLOW

    follow = hass.data.get(FOLLOW)
    areas = ar.async_get(hass)
    for s in screens:
        area = follow.area_of(s["id"]) if follow else None
        found = areas.async_get_area(area) if area else None
        if not s["tv"] and (KITCHEN.search(s["name"]) or (found and KITCHEN.search(found.name))):
            return s
    return next((s for s in screens if s["on"] and not s["tv"]), None) or (screens[0] if screens else None)


async def share(hass: HomeAssistant, sender: Any, link: str, entered: str) -> str | None:
    """A shared link: the reply for the phone when it was a recipe, None when it was not (left alone)."""
    from .const import PRO
    from .send import text as send_text
    from .voice import VOICE

    recipe = await fetch(hass, link)
    if recipe is None:
        return None
    if hass.data[PRO].state()["plan"] == "free":
        return send_text(hass, "pro")
    screen = kitchen_screen(hass, sender.screens(), entered, sender.find)
    if screen is None:
        return None
    voice = hass.data[VOICE]
    walls = [w for w in voice.walls.values() if w.screen == screen["id"]]
    if not walls:
        return _text(hass, "off", where=screen["name"])
    await sender._deliver_command(voice, walls, {"action": "recipe", "recipe": recipe})
    return _text(hass, "shown", where=screen["name"])
