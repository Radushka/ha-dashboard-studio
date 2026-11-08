"""Build a screen by talking: Dashboard Studio's tools for an AI assistant (Pro).

The tools are offered to Home Assistant as an LLM API named "Dashboard Studio". Any assistant that may use it
can then read the screens, find devices and change the layout:
1. Claude or another AI app, through Home Assistant's MCP Server integration.
2. An AI conversation agent set up in Home Assistant (Anthropic, OpenAI, Google and others), for example from
   the studio's Ask box.

An assistant never changes what the tablets show. It changes the studio's draft (see draft.py); the person sees
it in the studio preview, can undo it, and presses Publish when they like it. Every change is checked first, and
a mistake comes back as a clear message, so the assistant can fix it on its own.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm

from .const import DOMAIN, DRAFT, NEWS, PRO, WWW
from .draft import DraftStore
from .layout import LayoutStore

API_ID = DOMAIN
API_NAME = "Dashboard Studio"
# A whole project is a few hundred kilobytes at most. This stops a runaway answer from filling the disk.
MAX_PROJECT = 2_000_000
MAX_SCREENS = 12
MAX_CARDS = 80

# Cards that show one device, with the kinds of device that suit them.
SINGLE: dict[str, tuple[str, ...]] = {
    "light": ("light",),
    "climate": ("climate",),
    "media": ("media_player",),
    "camera": ("camera",),
    "graph": ("sensor",),
    "forecast": ("weather",),
    "device": ("switch", "input_boolean", "fan", "lock", "vacuum", "script", "button", "input_button", "sensor",
               "binary_sensor", "automation", "siren", "humidifier", "cover", "valve", "water_heater"),
    "todo": ("todo",),
    "gauge": ("sensor",),
    "alarm": ("alarm_control_panel",),
}
OPTIONAL_ENTITY = {"energy": ("sensor",)}
HOUSE = ("scenes", "doors", "shades", "family", "air", "calendar", "timer", "logbook", "smart", "home", "note",
         "photo", "webpage", "network", "custom")
TYPES = {"room", *SINGLE, *OPTIONAL_ENTITY, *HOUSE}
MENU_TYPES = {"climate", "media", "weather", "status", "shades", "family", "scenes", "voice"}
LOOKS = {
    "aurora": ("classic", "northern", "sunset", "slate"),
    "monochrome": ("black", "graphite"),
    "paper": ("ivory", "white"),
    "midnight": ("amber", "red"),
    "bold": ("bright", "soft"),
    "pill": ("dark",),
    "panel": ("classic",),
    "lucky8": ("gold", "words"),
    "neon": ("tubes", "words"),
    "pearl": ("pearl",),
}
# Theme values that make up a look (the same list as LOOK_THEME_KEYS in renderer/src/lib/screens.ts).
LOOK_THEME_KEYS = ("style", "font", "clockFont", "icons", "accent", "wallpaper", "wallpaperDim", "wallpaperImage",
                   "glass", "menuPanel", "look", "lookOption")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

PROMPT = """You can design the user's wall tablet dashboards in Dashboard Studio.
Rules:
1. Start with dashboard_overview. Call dashboard_card_types once before you add cards you have not used yet.
2. Use dashboard_find_devices to get real entity ids and area names. Never guess an entity id.
3. Change a screen with dashboard_get_screen, then dashboard_save_screen with the whole new layout of that
   screen. Keep everything you were not asked to change exactly as it is.
4. Your changes go to the studio's draft. The user sees them in the studio preview at once and can undo them.
   The tablets change only when the user presses Publish in the studio. Never say that the tablets changed.
5. If a save is refused, read the reasons, fix the layout and save again.
6. Good design: few cards, each with room to breathe. Group a room's lights in one room card. Put the
   thermostat, weather and media in the side menu when the screen is small (Fire 7 is 1024 x 600).
7. After a change, tell the user in one or two short sentences what you changed, and that they can press
   Publish in the studio, or Undo."""

CARD_TYPES = """Card types. Every card has type, x, y, w, h (grid cells, starting at 1) and an optional name.
With grid.auto true (the default), cards flow left to right in list order and wrap; x and y are then only an
order hint, and w and h are the size. With grid.auto false, x and y place the card, and cards must not overlap.
grid.columns is usually 12. grid.rows is how many rows fill the screen height.

Cards about one device ("entity" is required, of the kinds given):
- light: one light (light). climate: a thermostat dial (climate). media: a player (media_player);
  music: "record" (a turntable), "sleeve" (the record slides out of its cover) or "notes" (big song title);
  without it, the classic player card.
- camera: a camera (camera); live: "tap" | "always" | "never".
- graph: a sensor history line (sensor); hours: 24. gauge: a ring (sensor); min, max,
  levels: {warn, alert, high}.
- forecast: weather (weather); hours ahead, current: false hides the current conditions.
- device: a switch, plug, fan, lock, vacuum, script, button, sensor value, automation, siren, humidifier or cover.
- todo: a to-do list (todo). alarm: an alarm keypad (alarm_control_panel).
- energy: home energy use; entity is an optional power sensor.

Room card: type "room", area (an area id or name), optional scenes (scene ids, in order), sceneCount (3),
exclude (entity ids to leave out), hideScenes, hideAllOff, temperature (a sensor). It shows the room's lights as
tiles, its scenes and an All off button. This is the main card of most screens; give it w 4 and h 1 on a
12-column grid with rows 1 for three rooms side by side.

Whole-house cards (optional "entities" limits them to those ids; without it they show all of their kind):
- scenes, doors (garage doors, locks, open windows), shades (covers), family (person), air (air quality and UV),
  calendar (calendar), logbook (recent events), timer (a kitchen timer, no devices), network (Home Radar).
- smart: what matters right now in the house (topic: "house" or "outdoors").
- home: the home as a small 3D model, rooms glow in the color of their lights.
- note: text (a line starting with "- " is a list item, **bold**). webpage: url. photo: uploaded photos only.

Optional on any card: style {accent, tint, tintStrength 0-1, glass "clear"|"frosted"|"solid"|"none", radius,
text}, showWhen {entity, state, not}, rules [{entity, is "on"|"off"|"above"|"below"|"equals"|"not", value,
tint, accent, pulse, dim, hide}].
Colors are "#rrggbb".

Side menu: layout.menu is a list of {type, entity, label, name}. type is climate (entity), media (entity),
weather (entity, a weather.* entity), status, shades, family, scenes or voice. menuSide: "left" or "right".
Header: headerEntity (usually an outdoor temperature) and header {hidden, align "split"|"left"|"center"}.

House look (in the main layout, screen ""): theme.look and theme.lookOption.
Looks: aurora (Glass: classic, northern, sunset, slate), monochrome (black, graphite), paper (ivory, white),
midnight (amber, red), bold (bright, soft), pill (dark), panel (classic),
lucky8 (Lucky 8, red lacquer and gold: gold, words), neon (tubes, words), pearl (pearl). In the words option lights show as glowing names, not tiles. theme.accent is the accent color.
Every screen wears the house look, unless it has its own look (own_look in dashboard_overview). To give one
screen its own look (for example a child's room), or to put it back on the house look, use
dashboard_set_screen_look. A screen's own theme can always change sizes such as textScale.

Screens: the main layout (screen "") is for every tablet without its own layout. dashboard_add_screen makes a
tablet its own layout; that tablet then opens /hadash/index.html?screen=<id>."""


class AssistantProblem(HomeAssistantError):
    """A request the assistant can fix. The message says how."""


# ---------- Reading the house ----------


def _areas(hass: HomeAssistant) -> dict[str, str]:
    return {a.id: a.name for a in ar.async_get(hass).async_list_areas()}


def _entity_area(hass: HomeAssistant, entity_id: str) -> str | None:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        return device.area_id
    return None


def find_devices(hass: HomeAssistant, domain: str | None, area: str | None, text: str | None, limit: int) -> dict[str, Any]:
    areas = _areas(hass)
    want_area = None
    if area:
        want_area = next((i for i, n in areas.items() if area.lower() in (i.lower(), n.lower())), None)
        if want_area is None:
            raise AssistantProblem(f"There is no area '{area}'. Areas: {', '.join(sorted(areas.values()))}.")
    registry = er.async_get(hass)
    out = []
    for state in sorted(hass.states.async_all(), key=lambda s: s.entity_id):
        if domain and state.domain != domain:
            continue
        entry = registry.async_get(state.entity_id)
        if entry and (entry.hidden_by or entry.disabled_by):
            continue
        name = str(state.attributes.get("friendly_name") or state.entity_id)
        if text and text.lower() not in name.lower() and text.lower() not in state.entity_id:
            continue
        where = _entity_area(hass, state.entity_id)
        if want_area and where != want_area:
            continue
        item: dict[str, Any] = {"entity_id": state.entity_id, "name": name, "state": state.state}
        if where:
            item["area"] = areas.get(where, where)
        if dc := state.attributes.get("device_class"):
            item["device_class"] = dc
        if unit := state.attributes.get("unit_of_measurement"):
            item["unit"] = unit
        out.append(item)
    more = max(0, len(out) - limit)
    return {"devices": out[:limit], "more": more, "areas": sorted(areas.values())}


# ---------- The project ----------


def _default_project() -> dict[str, Any]:
    try:
        return json.loads((WWW / "dashboard.json").read_text())
    except (OSError, ValueError):
        return {"title": "Home", "grid": {"columns": 12, "rows": 1, "auto": True}, "widgets": []}


def working_project(hass: HomeAssistant) -> tuple[dict[str, Any], int]:
    """What the person sees in the studio: the draft when it is up to date, else the live layout."""
    store: LayoutStore = hass.data[DOMAIN]
    drafts: DraftStore = hass.data[DRAFT]
    revision = int(store.data.get("revision", 0))
    project = drafts.project_for(revision)
    if project is None:
        project = copy.deepcopy(store.layout) if store.layout else _default_project()
    return project, revision


def screen_of(project: dict[str, Any], screen: str) -> dict[str, Any]:
    if not screen:
        return project
    for s in project.get("screens") or []:
        if s.get("id") == screen:
            return s["layout"]
    ids = ", ".join(['"" (main)'] + [f'"{s.get("id")}" ({s.get("name")})' for s in project.get("screens") or []])
    raise AssistantProblem(f"There is no screen '{screen}'. Screens: {ids}.")


def _card_label(hass: HomeAssistant, w: dict[str, Any]) -> str:
    if w.get("name"):
        return str(w["name"])
    if w.get("type") == "room":
        return str(w.get("area"))
    if (e := w.get("entity")) and (state := hass.states.get(e)):
        return str(state.attributes.get("friendly_name") or e)
    return str(w.get("type"))


def overview(hass: HomeAssistant) -> dict[str, Any]:
    project, revision = working_project(hass)
    drafts: DraftStore = hass.data[DRAFT]

    def describe(layout: dict[str, Any]) -> dict[str, Any]:
        return {
            "grid": layout.get("grid"),
            "cards": [
                {"index": i, "type": w.get("type"), "label": _card_label(hass, w), "x": w.get("x"), "y": w.get("y"),
                 "w": w.get("w"), "h": w.get("h")}
                for i, w in enumerate(layout.get("widgets") or [])
            ],
            "menu": [m.get("type") for m in layout.get("menu") or [] if not m.get("hidden")],
        }

    theme = project.get("theme") or {}
    return {
        "look": {"look": theme.get("look", "aurora"), "option": theme.get("lookOption")},
        "unpublished_changes": drafts.project_for(revision) is not None,
        "screens": [{"screen": "", "name": "Main layout (every other tablet)", **describe(project)}]
        + [
            {"screen": s.get("id"), "name": s.get("name"), "device": s.get("device"), **describe(s.get("layout") or {}),
             **({"own_look": {"look": ((s.get("layout") or {}).get("theme") or {}).get("look", "aurora"),
                              "option": ((s.get("layout") or {}).get("theme") or {}).get("lookOption")}}
                if s.get("ownLook") else {})}
            for s in project.get("screens") or []
        ],
    }


# ---------- Checking a layout ----------


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_layout(hass: HomeAssistant, layout: Any, main: bool, own_look: bool = False) -> tuple[list[str], list[str]]:
    """Problems that block the save, and warnings that do not. Written for the assistant to act on."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(layout, dict):
        return ["The layout must be a JSON object."], []
    grid = layout.get("grid")
    if not isinstance(grid, dict) or not _int(grid.get("columns")) or not _int(grid.get("rows")):
        errors.append("grid must be {columns, rows} with whole numbers, such as {columns: 12, rows: 1, auto: true}.")
        grid = {"columns": 12, "rows": 1, "auto": True}
    elif not 1 <= grid["columns"] <= 24 or not 1 <= grid["rows"] <= 24:
        errors.append("grid.columns and grid.rows must be between 1 and 24.")
    widgets = layout.get("widgets")
    if not isinstance(widgets, list):
        return errors + ["widgets must be a list of cards."], warnings
    if len(widgets) > MAX_CARDS:
        errors.append(f"A screen can hold at most {MAX_CARDS} cards.")
    areas = _areas(hass)
    area_names = {n.lower() for n in areas.values()} | {i.lower() for i in areas}
    auto = grid.get("auto", True) is not False
    free = bool(grid.get("free"))
    cells: dict[tuple[int, int], int] = {}

    def known(entity: Any) -> bool:
        return isinstance(entity, str) and hass.states.get(entity) is not None

    for i, w in enumerate(widgets):
        where = f"Card {i}"
        if not isinstance(w, dict):
            errors.append(f"{where} must be a JSON object.")
            continue
        kind = w.get("type")
        where = f"Card {i} ({kind})"
        if kind not in TYPES:
            errors.append(f"{where}: unknown type. Use one of: {', '.join(sorted(TYPES))}.")
            continue
        if not all(_int(w.get(k)) and w[k] >= 1 for k in ("x", "y", "w", "h")):
            errors.append(f"{where}: x, y, w and h must be whole numbers from 1.")
            continue
        if not auto and not free:
            if w["x"] + w["w"] - 1 > grid["columns"] or w["y"] + w["h"] - 1 > grid["rows"]:
                errors.append(f"{where}: it goes past the grid ({grid['columns']} columns, {grid['rows']} rows).")
            for x in range(w["x"], w["x"] + w["w"]):
                for y in range(w["y"], w["y"] + w["h"]):
                    if (x, y) in cells:
                        errors.append(f"{where}: it overlaps card {cells[(x, y)]}. Move one of them.")
                        break
                    cells[(x, y)] = i
                else:
                    continue
                break
        elif w["w"] > grid["columns"]:
            errors.append(f"{where}: w is wider than the grid ({grid['columns']} columns).")
        entity = w.get("entity")
        if kind in SINGLE or (kind in OPTIONAL_ENTITY and entity):
            domains = SINGLE.get(kind) or OPTIONAL_ENTITY[kind]
            if not known(entity):
                errors.append(f"{where}: entity {entity!r} does not exist. Find one with dashboard_find_devices.")
            elif entity.split(".")[0] not in domains:
                errors.append(f"{where}: entity must be one of these kinds: {', '.join(domains)}.")
        if kind == "room":
            if not isinstance(w.get("area"), str) or w["area"].lower() not in area_names:
                errors.append(f"{where}: area {w.get('area')!r} does not exist. Areas: {', '.join(sorted(areas.values()))}.")
        for key in ("entities", "scenes", "exclude"):
            ids = w.get(key)
            if ids is None:
                continue
            if not isinstance(ids, list):
                errors.append(f"{where}: {key} must be a list of entity ids.")
                continue
            missing = [e for e in ids if not known(e)]
            if missing:
                warnings.append(f"{where}: these {key} do not exist and will not show: {', '.join(map(str, missing))}.")
        style = w.get("style") or {}
        for key in ("accent", "tint"):
            if key in style and not (isinstance(style[key], str) and COLOR.match(style[key])):
                errors.append(f"{where}: style.{key} must be a color like #ff9f0a.")
        if kind == "webpage" and not str(w.get("url", "")).startswith(("http://", "https://", "/")):
            errors.append(f"{where}: url must start with https://.")

    for i, m in enumerate(layout.get("menu") or []):
        if not isinstance(m, dict) or m.get("type") not in MENU_TYPES:
            errors.append(f"Menu item {i}: type must be one of {', '.join(sorted(MENU_TYPES))}.")
        elif m["type"] in ("climate", "media", "weather") and not known(m.get("entity")):
            errors.append(f"Menu item {i} ({m['type']}): entity {m.get('entity')!r} does not exist.")
    if layout.get("headerEntity") and not known(layout["headerEntity"]):
        errors.append(f"headerEntity {layout['headerEntity']!r} does not exist.")

    theme = layout.get("theme") or {}
    if not isinstance(theme, dict):
        errors.append("theme must be a JSON object.")
        theme = {}
    if (main or own_look) and "look" in theme:
        look, option = theme.get("look"), theme.get("lookOption")
        if look not in LOOKS:
            errors.append(f"theme.look must be one of: {', '.join(LOOKS)}.")
        elif option is not None and option not in LOOKS[look]:
            errors.append(f"theme.lookOption for {look} must be one of: {', '.join(LOOKS[look])}.")
    if "accent" in theme and not (isinstance(theme["accent"], str) and COLOR.match(theme["accent"])):
        errors.append("theme.accent must be a color like #ff9f0a.")
    if not main and "screens" in layout:
        errors.append("A screen's layout cannot hold screens of its own.")
    return errors, warnings


def _slug(name: str, taken: list[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "screen"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------- Saving ----------


def _pro_or_raise(hass: HomeAssistant) -> None:
    if hass.data[PRO].state()["plan"] == "free":
        raise AssistantProblem(
            "Building screens by talking is part of Dashboard Studio Pro. Tell the user they can buy Pro or enter "
            "their license in the studio (Dashboard Studio in the Home Assistant sidebar)."
        )


def _unnumbered(problem: str) -> str:
    return re.sub(r"^(Card|Menu item) \d+ ", r"\1 ", problem)


def save_screen(hass: HomeAssistant, screen: str, layout: dict[str, Any], summary: str) -> dict[str, Any]:
    _pro_or_raise(hass)
    project, revision = working_project(hass)
    target = screen_of(project, screen)
    main = not screen
    own = any(s.get("id") == screen and s.get("ownLook") for s in project.get("screens") or []) if screen else False
    layout = copy.deepcopy(layout)
    if main:
        # The main layout keeps its screens. They are changed one by one, with their own screen id.
        layout.pop("screens", None)
    errors, warnings = check_layout(hass, layout, main, own)
    # Problems that were in the layout already (a device the person removed from HA, say) do not block the
    # assistant's change; they come back as warnings. Card numbers are left out, since cards may move.
    before, _ = check_layout(hass, target, main, own)
    known = {_unnumbered(e) for e in before}
    warnings += [f"Already in the layout before: {e}" for e in errors if _unnumbered(e) in known]
    errors = [e for e in errors if _unnumbered(e) not in known]
    if errors:
        return {"saved": False, "problems": errors, "warnings": warnings}
    if main:
        screens = project.get("screens")
        project = layout
        if screens:
            project["screens"] = screens
    else:
        target.clear()
        target.update(layout)
    if len(json.dumps(project)) > MAX_PROJECT:
        raise AssistantProblem("The layout is too large to save.")
    hass.data[DRAFT].save_from_assistant(project, revision, summary.strip()[:300])
    return {"saved": True, "warnings": warnings, "next": "The studio preview shows it now. The tablets change only after Publish."}


def add_screen(hass: HomeAssistant, name: str, width: int, height: int, copy_from: str | None) -> dict[str, Any]:
    _pro_or_raise(hass)
    project, revision = working_project(hass)
    screens = project.setdefault("screens", [])
    if len(screens) >= MAX_SCREENS:
        raise AssistantProblem(f"There can be at most {MAX_SCREENS} screens.")
    if copy_from is not None:
        source = copy.deepcopy(screen_of(project, copy_from))
    else:
        source = {"title": name, "grid": {"columns": 12, "rows": 1, "auto": True}, "widgets": []}
    source.pop("screens", None)
    source.pop("sizes", None)
    sid = _slug(name, [s.get("id") for s in screens])
    screens.append({"id": sid, "name": name, "device": {"name": name, "w": width, "h": height, "lite": width <= 1280}, "layout": source})
    hass.data[DRAFT].save_from_assistant(project, revision, f"Added the screen {name}.")
    return {"screen": sid, "address": f"/hadash/index.html?screen={sid}"}


def set_screen_look(hass: HomeAssistant, screen: str, look: str, option: str | None) -> dict[str, Any]:
    """Gives one screen its own look, or ("house") puts it back on the house look. Screen "" sets the house look."""
    _pro_or_raise(hass)
    project, revision = working_project(hass)
    layout = screen_of(project, screen)
    entry = next((s for s in project.get("screens") or [] if s.get("id") == screen), None)
    if look == "house":
        if entry is None:
            raise AssistantProblem('The main layout is the house look. Name a look to change it.')
        entry.pop("ownLook", None)
        theme = layout.setdefault("theme", {})
        house = project.get("theme") or {}
        for k in LOOK_THEME_KEYS:
            if k in house:
                theme[k] = copy.deepcopy(house[k])
            else:
                theme.pop(k, None)
        summary = f"{entry.get('name')} wears the house look again."
    else:
        if look not in LOOKS:
            raise AssistantProblem(f"look must be house or one of: {', '.join(LOOKS)}.")
        if option is not None and option not in LOOKS[look]:
            raise AssistantProblem(f"option for {look} must be one of: {', '.join(LOOKS[look])}.")
        theme = layout.setdefault("theme", {})
        # The studio puts on every value of the look (wallpaper, fonts, glass) when it sees the new name.
        for k in LOOK_THEME_KEYS:
            theme.pop(k, None)
        theme["look"] = look
        if option is not None:
            theme["lookOption"] = option
        if entry is not None:
            entry["ownLook"] = True
        summary = f"{entry.get('name') if entry else 'The house'} now wears the look {look}."
    hass.data[DRAFT].save_from_assistant(project, revision, summary)
    return {"saved": True, "next": "The studio preview shows it now. The tablets change only after Publish."}


# ---------- The LLM API ----------


class _Tool(llm.Tool):
    """A Dashboard Studio tool. It runs only for an admin, since it changes the dashboards."""

    integration = DOMAIN
    read_only = True

    def __init__(self) -> None:
        if hasattr(llm, "ToolAnnotations"):
            self.annotations = llm.ToolAnnotations(
                read_only=self.read_only, destructive=False, idempotent=self.read_only, open_world=False
            )

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> Any:
        user_id = llm_context.context.user_id if llm_context.context else None
        user = await hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            return self._result({"error": "Only a Home Assistant administrator can change Dashboard Studio."}, True)
        try:
            data = self.run(hass, tool_input.tool_args)
        except AssistantProblem as err:
            return self._result({"error": str(err)}, True)
        return self._result(data, False)

    @staticmethod
    def _result(data: dict[str, Any], error: bool) -> Any:
        tool_result = getattr(llm, "ToolResult", None)
        return tool_result(data=data, error=error) if tool_result else data

    def run(self, hass: HomeAssistant, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class OverviewTool(_Tool):
    name = "dashboard_overview"
    description = "List the Dashboard Studio screens, their cards and the look. Call this first."

    def run(self, hass, args):
        return overview(hass)


class CardTypesTool(_Tool):
    name = "dashboard_card_types"
    description = "The card types, their settings, the side menu and the looks, with how to place cards."

    def run(self, hass, args):
        return {"guide": CARD_TYPES}


class FindDevicesTool(_Tool):
    name = "dashboard_find_devices"
    description = "Find devices (entities) for cards, by kind (domain such as light, sensor, camera), area or words in the name."
    parameters = vol.Schema(
        {
            vol.Optional("domain"): str,
            vol.Optional("area"): str,
            vol.Optional("text"): str,
            vol.Optional("limit", default=60): vol.All(int, vol.Range(min=1, max=300)),
        }
    )

    def run(self, hass, args):
        return find_devices(hass, args.get("domain"), args.get("area"), args.get("text"), args.get("limit", 60))


class GetScreenTool(_Tool):
    name = "dashboard_get_screen"
    description = 'The whole layout of one screen as JSON. screen "" is the main layout.'
    parameters = vol.Schema({vol.Optional("screen", default=""): str})

    def run(self, hass, args):
        project, _ = working_project(hass)
        layout = copy.deepcopy(screen_of(project, args.get("screen", "")))
        layout.pop("screens", None)
        return {"screen": args.get("screen", ""), "layout": layout}


class SaveScreenTool(_Tool):
    name = "dashboard_save_screen"
    description = (
        "Replace the whole layout of one screen (as from dashboard_get_screen, with your changes) in the studio's "
        "draft. summary is one short sentence for the user about what changed."
    )
    read_only = False
    parameters = vol.Schema(
        {vol.Optional("screen", default=""): str, vol.Required("layout"): dict, vol.Required("summary"): str}
    )

    def run(self, hass, args):
        return save_screen(hass, args.get("screen", ""), args["layout"], args["summary"])


class AddScreenTool(_Tool):
    name = "dashboard_add_screen"
    description = (
        "Give one tablet a layout of its own. width and height are the tablet's screen in pixels (Fire HD 10 is "
        '1280 x 800, Fire 7 is 1024 x 600). copy_from is a screen to start from ("" is the main layout); '
        "without it the screen starts empty."
    )
    read_only = False
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("width", default=1280): vol.All(int, vol.Range(min=320, max=3840)),
            vol.Optional("height", default=800): vol.All(int, vol.Range(min=320, max=2160)),
            vol.Optional("copy_from"): str,
        }
    )

    def run(self, hass, args):
        return add_screen(hass, args["name"], args.get("width", 1280), args.get("height", 800), args.get("copy_from"))


class SetScreenLookTool(_Tool):
    name = "dashboard_set_screen_look"
    description = (
        'Set the look of one screen. screen "" sets the house look, which every screen without its own look wears. '
        'For another screen, a look name gives that screen its own look (for example a child\'s room), and look '
        '"house" puts it back on the house look. option is one of the look\'s options.'
    )
    read_only = False
    parameters = vol.Schema(
        {vol.Optional("screen", default=""): str, vol.Required("look"): str, vol.Optional("option"): str}
    )

    def run(self, hass, args):
        return set_screen_look(hass, args.get("screen", ""), args["look"], args.get("option"))


# ---------- News, for anyone who asks (a voice speaker has no user)

MAX_NEWS = 20
NEWS_PROMPT = (
    "When the person asks for the news, headlines or what is happening, call news_headlines. It reads the news "
    "feeds of the person's Dashboard Studio News cards. Read only a few headlines aloud, in a short, natural way, "
    "and name the source. Offer more or a summary of one story when it fits."
)


def _age(published: float | None, now: float) -> str:
    if not published:
        return ""
    minutes = max(0, int((now - published) / 60))
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


async def news_headlines(hass: HomeAssistant, topic: str | None, limit: int) -> dict[str, Any]:
    """The newest stories of the News cards' feeds, newest first. topic keeps only stories that mention it."""
    from .news import layout_feeds

    store = hass.data.get(NEWS)
    urls = sorted(layout_feeds(hass))
    if store is None or not urls:
        return {"stories": [], "note": "No news feeds are set up. The person can add a News card in Dashboard Studio and pick feeds."}
    feeds = await asyncio.gather(*(store.async_feed(u) for u in urls[:30]))
    now = time.time()
    words = [w for w in re.findall(r"\w+", (topic or "").lower()) if len(w) > 2]
    stories: list[dict[str, Any]] = []
    sources: list[str] = []
    for f in feeds:
        d = f.data or {}
        # Videos cards use feeds too (YouTube): those are not news to read aloud.
        if d.get("kind") == "video":
            continue
        source = d.get("title") or f.url
        sources.append(source)
        for item in d.get("items", []):
            text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
            if words and not any(w in text for w in words):
                continue
            stories.append({
                "source": source,
                "title": item.get("title", ""),
                "summary": _cut_text(item.get("summary", ""), 240),
                "published": item.get("published") or 0,
            })
    stories.sort(key=lambda s: s["published"], reverse=True)
    out = [{**s, "published": _age(s["published"], now)} for s in stories[:limit]]
    result: dict[str, Any] = {"sources": sources, "stories": out}
    if not out:
        result["note"] = f"No stories about {topic} right now." if words else "The feeds have no stories right now."
    return result


def _cut_text(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


class NewsTool(llm.Tool):
    """The headlines of the News cards. Free, read only, and for anyone: a voice speaker talks without a user."""

    name = "news_headlines"
    description = (
        "The latest news headlines from the feeds on the person's Dashboard Studio News cards, newest first, with "
        "source, short summary and age. topic (optional) keeps only stories that mention it."
    )
    integration = DOMAIN
    parameters = vol.Schema(
        {vol.Optional("topic"): str, vol.Optional("limit", default=8): vol.All(int, vol.Range(min=1, max=MAX_NEWS))}
    )

    def __init__(self) -> None:
        if hasattr(llm, "ToolAnnotations"):
            self.annotations = llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=True)

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> Any:
        args = tool_input.tool_args
        data = await news_headlines(hass, args.get("topic"), args.get("limit", 8))
        return _Tool._result(data, False)


async def _is_admin(hass: HomeAssistant, llm_context: llm.LLMContext) -> bool:
    user_id = llm_context.context.user_id if llm_context.context else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    return bool(user and user.is_admin)


class DashboardStudioAPI(llm.API):
    """The tools, offered to every assistant the person allows to use "Dashboard Studio".

    Not admin-only as a whole: Home Assistant would then refuse the whole assistant for a voice speaker or a
    tablet, which talk without an admin, and the person would get no answer at all. Instead, everyone gets the
    news, and only an admin also gets the tools that change the dashboards (each one checks again).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=API_ID, name=API_NAME)

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        tools: list[llm.Tool] = [NewsTool()]
        prompt = NEWS_PROMPT
        if await _is_admin(self.hass, llm_context):
            tools += [OverviewTool(), CardTypesTool(), FindDevicesTool(), GetScreenTool(), SaveScreenTool(),
                      AddScreenTool(), SetScreenLookTool()]
            prompt = f"{PROMPT}\n\n{NEWS_PROMPT}"
        return llm.APIInstance(api=self, api_prompt=prompt, llm_context=llm_context, tools=tools)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Offers the tools to Home Assistant's assistants. Registered once, when Home Assistant starts."""
    try:
        llm.async_register_api(hass, DashboardStudioAPI(hass))
    except HomeAssistantError:
        # Already there, after a reload.
        pass


# ---------- The studio's Ask box ----------


def conversation_agents(hass: HomeAssistant) -> list[dict[str, Any]]:
    """AI conversation agents, and whether each may use Dashboard Studio (its "Control Home Assistant" choice).

    The setting lives on the agent's config subentry (Anthropic, OpenAI, Google and others since 2025.7), or on
    the config entry's options for older integrations.
    """
    registry = er.async_get(hass)
    out = []
    for state in hass.states.async_all("conversation"):
        entry = registry.async_get(state.entity_id)
        if entry is None or entry.config_entry_id is None:
            continue
        config = hass.config_entries.async_get_entry(entry.config_entry_id)
        if config is None:
            continue
        settings: Any = None
        sub_id = getattr(entry, "config_subentry_id", None)
        if sub_id and (sub := config.subentries.get(sub_id)):
            settings = sub.data.get("llm_hass_api")
        if settings is None:
            settings = config.options.get("llm_hass_api")
        apis = [settings] if isinstance(settings, str) else list(settings or [])
        out.append(
            {
                "entity_id": state.entity_id,
                "name": str(state.attributes.get("friendly_name") or state.entity_id),
                "integration": config.domain,
                "ready": API_ID in apis,
            }
        )
    return sorted(out, key=lambda a: (not a["ready"], a["name"].lower()))
