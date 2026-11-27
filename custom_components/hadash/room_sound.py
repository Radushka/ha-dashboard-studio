"""The sound of a room: "louder" goes to what plays there now.

Radu (27 Nov 2026): when music plays on the kitchen Bose, "louder" said to the kitchen tablet should change the
Bose, without naming it. So the player that plays in the room of the tablet (or speaker, or TV) the person talks
to wins. When nothing plays there, the TV keeps the volume, as before (tvremote.tv_do).

Rules, the same for every room and every device (Fire, Android, iPad, a voice speaker, a hand gesture):
1. Only players that play now, and that take a volume. The talking device's own players are left out: the tablet
   speaks its answer there.
2. Speakers that play together (a Bose SoundTouch zone) all step together, so their balance stays the same. A
   partner plays the same title from the same integration, as in the renderer's lib/mediaGroup.ts. The partner
   need not have a room of its own (Radu's "Indoors" Bose has none).
3. When two different things play in the room, the one that started last wins: that is what the person just
   put on.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

PLAYING = "playing"
VOLUME_SET = 4  # MediaPlayerEntityFeature.VOLUME_SET
VOLUME_STEP = 1024  # MediaPlayerEntityFeature.VOLUME_STEP
STEP = 0.06  # one "louder": about the same as three presses on most remotes


def _area_of(hass: HomeAssistant, entity_id: str) -> str | None:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    return device.area_id if device else None


def area_of_device(hass: HomeAssistant, device_id: str | None) -> str | None:
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    return device.area_id if device else None


def _takes_volume(state: State) -> bool:
    features = state.attributes.get("supported_features") or 0
    return bool(features & (VOLUME_SET | VOLUME_STEP)) or isinstance(state.attributes.get("volume_level"), (int, float))


def _partners(hass: HomeAssistant, lead: State) -> list[str]:
    """Players that play exactly the same thing as this one, from the same integration (one group)."""
    members = lead.attributes.get("group_members")
    if isinstance(members, list) and len(members) > 1:
        # The integration knows its group (Sonos and others): every member keeps its own volume.
        return [m for m in members if isinstance(m, str) and m != lead.entity_id and hass.states.get(m)]
    title = lead.attributes.get("media_title")
    if not title:
        return []
    reg = er.async_get(hass)
    entry = reg.async_get(lead.entity_id)
    platform = entry.platform if entry else None
    found = []
    for other in hass.states.async_all("media_player"):
        if other.entity_id == lead.entity_id or other.state != lead.state:
            continue
        o = reg.async_get(other.entity_id)
        if platform and o and o.platform != platform:
            continue
        a, b = lead.attributes, other.attributes
        if b.get("media_title") == title and (b.get("media_artist") or "") == (a.get("media_artist") or "") and (
            b.get("source") or ""
        ) == (a.get("source") or ""):
            found.append(other.entity_id)
    return found


def playing_here(hass: HomeAssistant, area_id: str | None, device_id: str | None = None) -> list[str]:
    """The players to change in this room: what plays there (started last wins) and its partners. Empty when
    nothing plays there."""
    if not area_id:
        return []
    reg = er.async_get(hass)
    mine = {e.entity_id for e in er.async_entries_for_device(reg, device_id)} if device_id else set()
    found: list[State] = []
    for state in hass.states.async_all("media_player"):
        if state.state != PLAYING or state.entity_id in mine or not _takes_volume(state):
            continue
        if _area_of(hass, state.entity_id) == area_id:
            found.append(state)
    if not found:
        return []
    lead = max(found, key=lambda s: s.last_changed)
    group = [lead.entity_id, *_partners(hass, lead)]
    return [p for p in dict.fromkeys(group) if p not in mine and (s := hass.states.get(p)) and _takes_volume(s)]


def name_of(hass: HomeAssistant, players: list[str]) -> str:
    """The first player's name, for the answer."""
    state = hass.states.get(players[0]) if players else None
    return str(state.attributes.get("friendly_name") or state.entity_id) if state else ""


async def change(hass: HomeAssistant, players: list[str], action: str, level: int | None = None) -> None:
    """louder, quieter, mute, unmute, or volume (level 0 to 100), on each player of the group."""
    call = hass.services.async_call
    for player in players:
        state = hass.states.get(player)
        if state is None:
            continue
        data = {"entity_id": player}
        features = state.attributes.get("supported_features") or 0
        now = state.attributes.get("volume_level")
        if action in ("mute", "unmute"):
            await call("media_player", "volume_mute", {**data, "is_volume_muted": action == "mute"}, blocking=True)
        elif action == "volume" and level is not None:
            await call("media_player", "volume_set", {**data, "volume_level": round(max(0, min(100, level)) / 100, 2)}, blocking=True)
        elif isinstance(now, (int, float)) and features & VOLUME_SET:
            step = STEP if action == "louder" else -STEP
            await call("media_player", "volume_set", {**data, "volume_level": round(max(0.0, min(1.0, now + step)), 2)}, blocking=True)
        else:
            for _ in range(3):
                await call("media_player", "volume_up" if action == "louder" else "volume_down", data, blocking=True)


# ---- The spoken words: Assist's sentence triggers (hassil templates), so they answer at once, even without an
# AI agent. {level} is free text; a number in it is the level. ----

SENTENCES: dict[str, list[str]] = {
    "louder": [
        "louder", "[a] (bit|little) louder", "make it louder", "turn it up", "turn [the] (volume|sound|music) up",
        "turn up [the] (volume|sound|music)", "volume up",
        "plus fort", "monte le (son|volume)", "lauter", "mach [es] lauter", "(más|mas) (alto|fuerte)",
        "sube el (volumen|sonido)",
    ],
    "quieter": [
        "quieter", "softer", "[a] (bit|little) quieter", "make it quieter", "turn it down",
        "turn [the] (volume|sound|music) down", "turn down [the] (volume|sound|music)", "volume down",
        "moins fort", "baisse le (son|volume)", "leiser", "mach [es] leiser", "(más|mas) (bajo|suave)",
        "baja el (volumen|sonido)",
    ],
    "volume": [
        "[set|turn] [the] volume [to] {level}", "(mets|règle|regle) le volume (à|a) {level}", "le volume (à|a) {level}",
        "lautstärke [auf] {level}", "(pon|sube|baja) el volumen (a|al) {level}", "volumen (a|al) {level}",
    ],
}

_FULL = {"max", "maximum", "full", "100", "à fond", "a fond", "voll", "al máximo", "al maximo", "máximo"}


def level_of(text: str) -> int | None:
    """30 from "30", "30 %", "30 percent"; 100 from "max"; None when there is no level from 0 to 100."""
    said = text.casefold().strip(" .!?")
    if said in _FULL:
        return 100
    m = re.match(r"^(\d{1,3})(?:\s*%|\s*(?:percent|pour ?cent|prozent|por ?ciento))?$", said)
    return int(m.group(1)) if m and int(m.group(1)) <= 100 else None


TEXT = {
    "nothing": {
        "en": "Nothing is playing here.",
        "fr": "Rien ne joue ici.",
        "de": "Hier spielt gerade nichts.",
        "es": "Aquí no suena nada.",
    },
    "level": {
        "en": "Say a volume from 0 to 100.",
        "fr": "Dites un volume de 0 à 100.",
        "de": "Sag eine Lautstärke von 0 bis 100.",
        "es": "Di un volumen de 0 a 100.",
    },
}


def text(key: str, language: str | None) -> str:
    lang = (language or "en").split("-")[0].lower()
    return TEXT[key].get(lang, TEXT[key]["en"])
