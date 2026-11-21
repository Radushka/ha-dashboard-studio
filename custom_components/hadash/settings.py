"""Studio settings by name, for Build by talking.

The person says a setting the way the Studio shows it ("turn off Morning hello", "make the text bigger").
Every place that Find a setting knows (renderer/src/studio/find.ts) is listed here with the same name, so an
assistant can always say where a setting lives. The simple ones (switches, sizes and short choices) can also be
changed here. A test keeps this list and find.ts the same.

Where a value goes:
1. "house": the main layout, for every screen (Moments, Language, the feels-like number).
2. "look": the look; the main layout, or the screen itself when it wears its own look.
3. "screen": the screen that was named ("" is the main layout).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

WAKE_WORDS = {
    "okay_nabu": "Okay Nabu",
    "hey_jarvis": "Hey Jarvis",
    "hey_mycroft": "Hey Mycroft",
    "okay_computer": "Okay Computer",
    "hey_home_assistant": "Hey Home Assistant",
    "slushai_dom": "Слушай, дом",
    "privet_dom": "Привет, дом",
    "off": "Off",
}
LANGUAGES = {"auto": "Automatic", "en": "English", "fr": "Français", "de": "Deutsch", "es": "Español"}


@dataclass(frozen=True)
class Setting:
    name: str
    tab: str
    section: str
    words: str = ""
    # Only for the settings that can be changed by talking:
    path: tuple[str, ...] = ()
    scope: str = "screen"
    kind: str = ""  # "switch", "size", "number", "choice", "color"
    default: Any = None
    # A switch stored the other way around ("Show the top" is header.hidden).
    invert: bool = False
    # A switch that is on unless it is false (voice.cards), so "on" removes the value.
    on_unless_false: bool = False
    low: float = 0
    high: float = 0
    choices: dict[str, str] = field(default_factory=dict)
    # It needs another setting first (Screensaver's options need a screensaver).
    needs: tuple[str, ...] = ()
    pro: str = ""
    # A short line for the assistant about this setting.
    note: str = ""

    @property
    def where(self) -> str:
        tab = {"look": "Look", "screen": "Cards", "menu": "Menu", "home": "Home"}[self.tab]
        parts = [tab] + ([self.section] if self.section != self.name else []) + [self.name]
        return " › ".join(parts)

    @property
    def changeable(self) -> bool:
        return bool(self.path)


def _s(name: str, section: str, words: str = "", tab: str = "look", **more: Any) -> Setting:
    return Setting(name=name, tab=tab, section=section, words=words, **more)


def _moment(name: str, key: str, section: str, words: str) -> Setting:
    return _s(name, section, words, path=("theme", "moments", key), scope="house", kind="switch", default=False, pro="moments")


def _voice(name: str, key: str, words: str) -> Setting:
    return _s(name, "Talk to the wall", words, path=("voice", key), kind="switch", default=True, on_unless_false=True, pro="voice")


def _saver(name: str, key: str, words: str, default: bool = True, pro: str = "") -> Setting:
    return _s(name, "Screensaver", words, path=("screensaver", key), kind="switch", default=default,
              on_unless_false=default, needs=("screensaver",), pro=pro)


def _size(name: str, section: str, key: str, words: str, low: float, high: float, default: float = 1, scope: str = "screen") -> Setting:
    path = (key,) if key == "textScale" else ("theme", key)
    return _s(name, section, words, path=path, scope=scope, kind="size", default=default, low=low, high=high)


SETTINGS: list[Setting] = [
    # Look tab
    _s("Look", "Look", "theme looks skin style glass monochrome paper daylight clay midnight bold pill panel neon lantern pearl lucky",
       note="Change it with dashboard_set_screen_look."),
    _s("Own look", "Look for {name}", "own look house look child room different screen", note="Change it with dashboard_set_screen_look."),
    _s("Address for this tablet", "Screen: {name}", "url link fully kiosk start page copy ip"),
    _s("Copy from this screen to another screen", "Screen: {name}", "copy duplicate sizes everything"),
    _s("Top of the screen", "Top of the screen", "header top bar clock date temperature weather"),
    _s("Show the top", "Top of the screen", "header hide", path=("header", "hidden"), kind="switch", default=True, invert=True),
    _s("Placement", "Top of the screen", "header align left center right", path=("header", "align"), kind="choice", default="split",
       choices={"split": "Split", "left": "Left", "center": "Center"}),
    _s("Clock", "Top of the screen", "time 12 24 hours clock size"),
    _s("Date", "Top of the screen", "day month weekday"),
    _s("Feels like", "Top of the screen", "feels like actual temperature apparent large number", path=("mainTemperature",), scope="house",
       kind="choice", default="actual", choices={"actual": "Actual", "feels_like": "Feels like"}),
    _s("Show the weather in words (\"Partly cloudy\")", "Top of the screen", "condition cloudy sunny", path=("header", "condition"),
       kind="switch", default=True, on_unless_false=True),
    _s("Holidays", "Top of the screen", "holiday holidays seasonal christmas halloween easter thanksgiving hanukkah valentine pumpkin festive season fun",
       path=("header", "holiday", "on"), kind="switch", default=False),
    _s("Follow me", "Follow me", "face person who in front recognition each person screen"),
    _size("Text size", "Text", "textScale", "font size bigger smaller larger", 0.7, 1.8),
    _size("Room names", "Text", "roomName", "room name size", 0.6, 2),
    _s("Light tiles (all rooms)", "Light tiles (all rooms)", "light tiles buttons lamps"),
    _size("Tile height", "Light tiles (all rooms)", "tile", "light tile tall", 0.6, 2),
    _s("Tile corners", "Light tiles (all rooms)", "light tile round radius", path=("theme", "tileRadius"), kind="number", default=18, low=0, high=32),
    _s("Tiles per row", "Light tiles (all rooms)", "light tile columns", path=("theme", "tileColumns"), kind="number", default=2, low=1, high=4),
    _size("Scene buttons", "Sizes", "scenes", "scene size", 0.6, 1.8),
    _size("Side menu", "Sizes", "menu", "menu size", 0.6, 1.6),
    _size("Space between cards", "Sizes", "gap", "gap spacing margin", 0.3, 2.5),
    _size("Space inside cards", "Sizes", "pad", "padding spacing", 0.3, 2.5),
    _s("Card corners", "Sizes", "round radius corners", path=("theme", "radius"), kind="number", default=28, low=0, high=48),
    _s("Back to the main screen", "Back to the main screen", "timeout close view return home idle", path=("viewClose",), kind="choice",
       default="60", choices={"30": "30 s", "60": "1 min", "120": "2 min", "300": "5 min", "0": "Never"}),
    _s("Screensaver", "Screensaver", "saver idle clock photos slideshow picture frame", path=("screensaver",), kind="choice", default="off",
       choices={"off": "Off", "clock": "Clock"}, note="Photos need photos chosen in the Studio, so only Off and Clock can be set here."),
    _s("Clock style", "Screensaver", "saver clock face", pro="clocks"),
    _saver("Slow drift", "drift", "saver zoom pan ken burns"),
    _saver("On this day", "memories", "memories years ago saver photos", default=False, pro="moments"),
    _saver("Place and date", "captions", "caption location saver photos"),
    _saver("Night mode", "night", "saver dim warm evening"),
    _saver("Collage", "collage", "saver four photos"),
    _s("Home Assistant Media", "Screensaver", "synology photos immich album folder media"),
    _s("Apple Shared Album", "Screensaver", "apple icloud iphone mac photos shared album iphoto public website"),
    _saver("Next event and weather", "info", "saver calendar event forecast"),
    _saver("Headlines", "news", "saver news", default=False, pro="news"),
    _s("Show now on the tablets", "Screensaver", "test saver try"),
    _s("Rest and wake", "Rest and wake", "sleep screen off dark backlight motion wake presence power", path=("rest",), kind="switch", default=False),
    _s("Sleep at night", "Rest and wake", "night dark bedtime off", path=("rest", "night"), kind="switch", default=True,
       on_unless_false=True, needs=("rest",)),
    _s("Sleep in the day when nobody is near", "Rest and wake", "away idle nobody"),
    _s("The screen's own camera sees someone", "Rest and wake", "camera motion wake front", path=("rest", "camera"), kind="switch",
       default=True, on_unless_false=True, needs=("rest",)),
    _s("Wake when", "Rest and wake", "motion sensor presence occupancy mmwave"),
    _s("Set up Fully Kiosk for this", "Rest and wake", "fully kiosk javascript visual motion"),
    _s("Talk to the wall", "Talk to the wall", "voice assist assistant microphone speech talk", pro="voice"),
    _s("Wake word", "Talk to the wall", "okay nabu hey jarvis mycroft computer hotword listen russian слушай привет дом", path=("voice", "wake"),
       kind="choice", default="okay_nabu", choices=WAKE_WORDS, pro="voice"),
    _s("More languages", "Talk to the wall", "language languages russian second wake word assistant pipeline bilingual translate", pro="voice"),
    _s("Listen with", "Talk to the wall", "tv ears microphone tablet satellite", pro="voice"),
    _s("Look while talking", "Talk to the wall", "iris eclipse aurora horizon orb classic voice look animation", path=("voice", "look"),
       kind="choice", default="iris", choices={"iris": "Iris", "eclipse": "Eclipse", "aurora": "Aurora", "horizon": "Horizon", "orb": "Orb", "simple": "Simple"}),
    _voice("Show answers as cards", "cards", "voice answer card"),
    _voice("Wall commands", "commands", "voice commands timer show me"),
    _voice("Keep listening after an answer", "followOn", "follow up conversation continue"),
    _voice("Speak up when it matters", "speakUp", "proactive announce reminder garage rain"),
    _s("Night voice", "Talk to the wall", "quiet soft whisper night volume", path=("voice", "night"), kind="switch", default=True, on_unless_false=True),
    _s("Voice check", "Talk to the wall", "voice assist expose entities what can voice use rooms names"),
    _s("Sounds", "Talk to the wall", "chime beep sound", path=("voice", "sounds"), kind="switch", default=True, on_unless_false=True),
    _s("Language", "Language", "language langue sprache idioma english french german spanish", path=("theme", "language"), scope="house",
       kind="choice", default="auto", choices=LANGUAGES),
    _s("Moments", "Moments", "moments animation effects", pro="moments"),
    _moment("Weather on the glass", "weather", "Moments", "rain drops snow frost fog storm"),
    _moment("Welcome home", "welcome", "Moments", "arrive greeting name"),
    _moment("Night path", "night", "Moments", "night walk dim path"),
    _moment("Morning hello", "hello", "Moments", "morning greeting briefing day ahead good morning hello"),
    _s("Doorbell moment", "Moments", "doorbell ring door camera"),
    _s("Home moments", "Home moments", "proactive assistant nudge suggest remind leaving night rain"),
    _moment("Leaving home", "leave", "Home moments", "leave door exit lock umbrella"),
    _s("This screen is by the door", "Home moments", "front door entrance exit", path=("door",), kind="switch", default=False),
    _moment("Good night", "goodnight", "Home moments", "bed sleep close up lock night"),
    _moment("Weather change", "rainwatch", "Home moments", "rain frost window awning"),
    _moment("Needs you soon", "soon", "Home moments", "soon urgent fridge freezer door left open cold rain window minutes"),
    _s("Emergencies", "Home moments", "emergency smoke fire carbon monoxide co gas leak water alarm"),
    _s("When everyone has left", "Home moments", "away empty house auto lock lights off garage"),
    _s("Guest mode", "Guest mode", "guest babysitter visitors cleaner hide private"),
    _s("Wi-Fi for guests", "Guest mode", "wifi wireless password qr code network"),
    _s("A note for guests", "Guest mode", "guest note message"),
    _s("Turn on by itself", "Guest mode", "guest calendar automatic"),
    _s("Style", "Style", "minimal colorful color neutral", path=("theme", "style"), scope="look", kind="choice", default="color",
       choices={"color": "Colorful", "minimal": "Minimal"}),
    _s("Fonts and icons", "Fonts and icons", "font typeface typography"),
    _s("Clock and big numbers", "Fonts and icons", "clock font numbers digits"),
    _s("Icons", "Fonts and icons", "icon style thin bold"),
    _s("Moving icons", "Fonts and icons", "animation motion moving alive still fan steam"),
    _size("Glass strength", "Glass", "glass", "blur frosted transparency glass", 0, 2.5, scope="look"),
    _s("Side menu panel", "Glass", "menu background panel"),
    _s("Accent", "Colors", "accent color tint highlight", path=("theme", "accent"), scope="look", kind="color", default="#ff9f0a"),
    _s("Wallpaper", "Colors", "background wallpaper photo image picture own"),
    _s("Wallpaper darkness", "Colors", "dim dark background", path=("theme", "wallpaperDim"), scope="look", kind="size", default=0.35, low=0, high=0.9),
    _s("Mood mirror", "Colors", "lights color wallpaper mood", path=("theme", "mood"), scope="look", kind="choice", default="off",
       choices={"off": "Off", "house": "Lights in the whole house"}, note="One room's lights can be chosen in the Studio."),
    # Cards tab
    _s("Living dashboard", "Living dashboard", "living one picture arrange itself moment sky", tab="screen", pro="living"),
    _s("People under the house", "Living dashboard", "living people family person", tab="screen"),
    _s("Scene buttons by the temperature", "Living dashboard", "living scenes scene buttons", tab="screen"),
    _s("See a moment in the preview", "Living dashboard", "living preview morning evening storm music door", tab="screen"),
    _s("Grid", "Grid", "grid columns rows layout free arrange automatic position", tab="screen", note="Change it with dashboard_save_screen."),
    _s("＋ Add a card…", "Cards", "add new card widget", tab="screen", note="Add cards with dashboard_save_screen."),
    _s("Show this card", "Cards", "conditional only when hide visibility", tab="screen", note="A card's showWhen; use dashboard_save_screen."),
    _s("In guest mode", "Cards", "guest hide card", tab="screen", note="A card's guest value; use dashboard_save_screen."),
    _s("Coming up", "Cards", "coming up soon due bins garbage recycling reminder upcoming calendar to-do todo battery batteries checkup line tile",
       tab="screen", note="A line tile, card type comingup (its settings in soon). Place it with dashboard_save_screen."),
    _s("News line", "Cards", "news line ticker headlines bottom crawl feed scroll tile",
       tab="screen", note="A line tile, card type newsline (seconds per headline in seconds). Place it with dashboard_save_screen."),
    _s("Photo frame", "Cards", "photo frame apple icloud iphone mac shared album iphoto media synology immich uploads on this day", tab="screen",
       note="A photo card's source: photos (uploads), media {id, title} or album {url, title} (an Apple Shared Album link with Public Website on); memories for On this day."),
    _s("Style, rules and gestures", "Cards", "tint rules long press double tap gestures card style font color", tab="screen",
       note="A card's style and rules; use dashboard_save_screen."),
    _s("Show the current temperature line", "Cards", "forecast now line", tab="screen", note="The forecast card's current value."),
    _s("Hours ahead", "Cards", "forecast hours", tab="screen", note="The forecast card's hours value."),
    _s("Live video", "Cards", "camera stream live always tap", tab="screen", note="The camera card's live value."),
    _s("TV pauses for the doorbell", "Cards", "tv television netflix doorbell pause resume remote", tab="screen", pro="tvdoor",
       note="The TV card's doorPause value (true or false). A TV card is type tv with entity (a TV's media player, or firetv:<address>) and optional sound (the player for the volume)."),
    _s("Music card styles", "Cards", "turntable sleeve record vinyl music player", tab="screen", pro="music", note="The media card's music value."),
    _s("Hours of history", "Cards", "graph history chart", tab="screen", note="The graph card's hours value."),
    _s("Smart card", "Cards", "smart house checks outdoors", tab="screen", note="The smart card's topic value."),
    _s("Warning colors", "Cards", "gauge amber red levels", tab="screen", note="The gauge card's levels value."),
    _s("Looks from", "Cards", "home model corner angle view", tab="screen"),
    _s("Use its own light tile style", "Cards", "room tiles", tab="screen"),
    _s("Top of the card", "Cards", "room scenes recent chosen", tab="screen"),
    _s("Next to the name", "Cards", "room badges humidity motion temperature open", tab="screen"),
    _s("Show the \"All off\" button", "Cards", "room all off", tab="screen", note="The room card's hideAllOff value."),
    _s("Lights on the card", "Cards", "room lights hide exclude", tab="screen", note="The room card's exclude list."),
    # Menu and Home tabs
    _s("Side of the screen", "Side menu", "menu left right side", tab="menu", path=("menuSide",), kind="choice", default="left",
       choices={"left": "Left", "right": "Right"}),
    _s("Side menu", "Buttons", "menu buttons views navigation", tab="menu", note="The menu list; use dashboard_save_screen."),
    _s("Home model", "Home model", "home model floor plan rooms 3d house", tab="home", pro="home"),
]

# Words that say what to do, not which setting.
FILLER = {"turn", "on", "off", "set", "the", "a", "an", "to", "please", "setting", "settings", "switch", "enable", "disable",
          "make", "change", "in", "dashboard", "studio", "of", "my", "it", "show", "hide", "stop", "start", "can", "you"}


def fold(text: str) -> str:
    """Lower case, without accents and quotes."""
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower().replace('"', "")


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^\w]+", fold(text)) if w]


def score(query: str, s: Setting) -> float:
    """Like the Studio's Find a setting: every word must be found; the visible name counts most."""
    words = [w for w in _words(query) if w not in FILLER] or _words(query)
    if not words:
        return 0
    name = fold(s.name)
    name_words = _words(s.name)
    more = _words(f"{s.words} {s.section}")
    total = 0.0
    for w in words:
        if name.startswith(w):
            total += 100
        elif w in name_words or any(n.startswith(w) for n in name_words):
            total += 70
        elif w in name:
            total += 40
        elif any(m.startswith(w) for m in more):
            total += 25
        elif len(w) > 3 and any(difflib.SequenceMatcher(None, w, m).ratio() > 0.84 for m in name_words + more):
            total += 15  # a small typo: "mornig"
        else:
            return 0
    return total - len(name) / 100


def find(query: str, limit: int = 5) -> list[Setting]:
    found = sorted(((score(query, s), i, s) for i, s in enumerate(SETTINGS)), key=lambda x: (-x[0], x[1]))
    return [s for sc, _, s in found if sc > 0][:limit]


def closest(query: str, limit: int = 3) -> list[str]:
    """Names that are near what was said, for "Did you mean…?"."""
    names = {fold(s.name): s.name for s in SETTINGS}
    near = difflib.get_close_matches(fold(query), list(names), n=limit, cutoff=0.45)
    return [names[n] for n in near]


def by_name(name: str) -> Setting | None:
    want = fold(name).strip()
    for s in SETTINGS:
        if fold(s.name) == want:
            return s
    hits = find(name, 1)
    return hits[0] if hits else None


# ---------- Values ----------


def _get(doc: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def read(s: Setting, layout: dict[str, Any]) -> Any:
    """The value now, the way the Studio shows it."""
    raw = _get(layout, s.path)
    if s.kind == "switch":
        if s.needs and _get(layout, s.needs) is None:
            return "off (needs " + " and ".join(by_path(n) for n in s.needs) + " first)"
        if s.path == ("rest",):
            return "on" if raw is not None else "off"
        if s.invert:
            return "off" if raw is True else "on"
        if s.on_unless_false:
            return "off" if raw is False else "on"
        return "on" if raw else "off"
    if s.path == ("screensaver",):
        if not raw:
            return "Off"
        return f"Photos, after {raw.get('minutes', 5)} min" if "photos" in raw or "media" in raw else f"Clock, after {raw.get('minutes', 5)} min"
    if raw is None:
        raw = s.default
    if s.kind == "choice":
        return s.choices.get(str(raw), str(raw))
    if s.kind == "size":
        return f"{round(float(raw) * 100)}%"
    return raw


def by_path(path: str) -> str:
    return next((s.name for s in SETTINGS if s.path == (path,)), path)


class SettingProblem(ValueError):
    """A value that does not fit; the message says what fits."""


def parse(s: Setting, value: Any) -> Any:
    """What the person said, as the value to store. None means "back to the default"."""
    text = fold(str(value)).strip()
    if s.kind == "switch":
        if isinstance(value, bool):
            on = value
        elif text in {"on", "yes", "true", "1", "enable", "enabled", "show"}:
            on = True
        elif text in {"off", "no", "false", "0", "disable", "disabled", "hide"}:
            on = False
        else:
            raise SettingProblem(f'{s.name} is a switch: say "on" or "off".')
        return on
    if s.kind == "choice":
        for key, label in s.choices.items():
            if text in {fold(key), fold(label), fold(key).replace("_", " ")}:
                return key
        if s.path == ("viewClose",) and (m := re.fullmatch(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?)?", text)):
            seconds = int(m[1]) * (60 if m[2] and m[2].startswith("m") else 1)
            if str(seconds) in s.choices:
                return str(seconds)
        raise SettingProblem(f"{s.name} can be: {', '.join(s.choices.values())}.")
    if s.kind == "color":
        if text in {"reset", "default"}:
            return None
        if re.fullmatch(r"#[0-9a-f]{6}", text):
            return text
        raise SettingProblem(f'{s.name} is a color, such as "#ff9f0a".')
    if text in {"reset", "default", "normal"}:
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(%|px)?", text)
    if not m:
        raise SettingProblem(f"{s.name} needs a number.")
    n = float(m[1])
    if s.kind == "size" and (m[2] == "%" or n > 3):
        n = n / 100
    if not s.low <= n <= s.high:
        shown = (lambda v: f"{round(v * 100)}%") if s.kind == "size" else (lambda v: f"{v:g}")
        raise SettingProblem(f"{s.name} goes from {shown(s.low)} to {shown(s.high)}.")
    return int(n) if s.kind == "number" else round(n, 2)


def write(s: Setting, layout: dict[str, Any], value: Any) -> None:
    """Puts the parsed value in the layout the way the Studio does: a default value is removed, not stored."""
    if s.path == ("screensaver",):
        if value == "off":
            layout.pop("screensaver", None)
        else:
            old = layout.get("screensaver") or {}
            layout["screensaver"] = {"minutes": old.get("minutes", 5)}
        return
    if s.path == ("rest",):
        if value:
            layout.setdefault("rest", {})
        else:
            layout.pop("rest", None)
        return
    if s.needs and _get(layout, s.needs) is None:
        raise SettingProblem(f"Turn on {' and '.join(by_path(n) for n in s.needs)} first.")
    if s.kind == "switch":
        if s.invert:
            value = not value
            store = True if value else None
        elif s.on_unless_false:
            store = None if value else False
        else:
            store = True if value else None
    elif s.kind == "choice":
        store = None if value == s.default else value
        if s.path == ("viewClose",) and store is not None:
            store = int(store)
    else:
        store = value
    parents = s.path[:-1]
    doc = layout
    chain = []
    for key in parents:
        chain.append((doc, key))
        doc = doc.setdefault(key, {})
    if store is None:
        doc.pop(s.path[-1], None)
    else:
        doc[s.path[-1]] = store
    # Empty objects go away, as in the Studio.
    for parent, key in reversed(chain):
        if parent.get(key) == {}:
            parent.pop(key)
