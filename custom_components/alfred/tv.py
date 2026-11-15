"""See and drive an Android TV in one step, over the Android Debug Bridge integration.

Home Assistant's own TV screenshot is cached (a new one every few minutes), and remote
buttons are pressed blind. The ``alfred.tv`` action instead presses the keys, types or
opens an app or link, waits for the screen to settle, then takes a fresh screenshot and
reads the on-screen text (``uiautomator dump``), including which item has focus. So an
assistant can see the result of every step before taking the next one.

It reuses the ADB connection of the androidtv integration's config entry, so there is
no second connection or key for the TV to authorise.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from io import BytesIO
import re
from typing import Any
import xml.etree.ElementTree as ET

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN, LOGGER

SERVICE_TV = "tv"
ADB_DOMAIN = "androidtv"

MAX_KEYS = 30
KEYS_PER_CALL = 8
MAX_WAIT = 10.0
MAX_ITEMS = 120
MAX_TEXT = 60
PICTURE_WIDTH = 1280
UI_FILE = "/sdcard/.alfred_ui.xml"
MARK = "@@ALFRED@@"

# Friendly names for Android key codes; any KEYCODE_* name or a number works too.
KEYS = {
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "select": "KEYCODE_DPAD_CENTER",
    "ok": "KEYCODE_DPAD_CENTER",
    "center": "KEYCODE_DPAD_CENTER",
    "enter": "KEYCODE_ENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "settings": "KEYCODE_SETTINGS",
    "search": "KEYCODE_SEARCH",
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "stop": "KEYCODE_MEDIA_STOP",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "fast_forward": "KEYCODE_MEDIA_FAST_FORWARD",
    "rewind": "KEYCODE_MEDIA_REWIND",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
    "channel_up": "KEYCODE_CHANNEL_UP",
    "channel_down": "KEYCODE_CHANNEL_DOWN",
    "delete": "KEYCODE_DEL",
    "wakeup": "KEYCODE_WAKEUP",
    "sleep": "KEYCODE_SLEEP",
    "power": "KEYCODE_POWER",
    "info": "KEYCODE_INFO",
    "guide": "KEYCODE_GUIDE",
    "captions": "KEYCODE_CAPTIONS",
}
KEY_PATTERN = re.compile(r"^(?P<key>[A-Za-z0-9_]+)(?:\s*[*x]\s*(?P<times>\d+))?$")
PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
LINK = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)
BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

TV_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("keys", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("text"): cv.string,
        vol.Optional("open"): cv.string,
        vol.Optional("wait"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=MAX_WAIT)
        ),
        vol.Optional("picture", default=True): cv.boolean,
        vol.Optional("screen_text", default=True): cv.boolean,
    }
)


def key_codes(keys: list[str]) -> list[str]:
    """Turn ["down*3", "select"] into Android key codes, refusing anything unknown."""
    codes: list[str] = []
    for raw in keys:
        match = KEY_PATTERN.match(raw.strip())
        if not match:
            raise ServiceValidationError(f'"{raw}" is not a key.')
        name = match["key"]
        times = int(match["times"] or 1)
        code = KEYS.get(name.lower())
        if code is None:
            upper = name.upper()
            if upper.startswith("KEYCODE_") or name.isdigit():
                code = upper
            else:
                code = f"KEYCODE_{upper}"
        codes.extend([code] * times)
    if len(codes) > MAX_KEYS:
        raise ServiceValidationError(
            f"That is {len(codes)} key presses; send at most {MAX_KEYS} at a time "
            "and look between steps."
        )
    return codes


def _quote(value: str) -> str:
    """Single-quote for the Android shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def text_command(text: str) -> str:
    """`input text` for [text]; it needs spaces written as %s."""
    return "input text " + _quote(text.replace(" ", "%s"))


def open_command(target: str) -> str | None:
    """A shell command that opens a link; None for a package (use launch_app)."""
    if LINK.match(target):
        return f"am start -a android.intent.action.VIEW -d {_quote(target)}"
    if PACKAGE.match(target):
        return None
    raise ServiceValidationError(
        f'"{target}" is neither a link (https://...) nor an app package '
        "(such as com.netflix.ninja)."
    )


def _center(bounds: str) -> tuple[int, int] | None:
    if not (m := BOUNDS.match(bounds)):
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _label(node: ET.Element) -> str:
    text = (node.get("text") or "").strip()
    desc = (node.get("content-desc") or "").strip()
    if text and desc and desc != text:
        text = f"{text} ({desc})"
    label = " ".join((text or desc).split())
    return label[:MAX_TEXT] + ("…" if len(label) > MAX_TEXT else "")


def _inner_label(node: ET.Element) -> str:
    """Its own label, else the labels inside it (focus is often on a card)."""
    if own := _label(node):
        return own
    parts = [label for child in node.iter() if (label := _label(child))]
    joined = " / ".join(dict.fromkeys(parts))
    return joined[: MAX_TEXT * 2]


def read_screen(xml: str) -> dict[str, Any]:
    """Shorten a uiautomator dump: what has focus, and what is shown."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise ValueError(f"the screen text could not be read ({err})") from err
    focused = ""
    items: list[str] = []
    seen: set[str] = set()
    for node in root.iter("node"):
        is_focused = node.get("focused") == "true"
        if is_focused and not focused:
            focused = _inner_label(node) or "(an item with no text)"
        label = _label(node)
        if not label and not is_focused:
            continue
        if is_focused and not label:
            label = _inner_label(node) or "(no text)"
        center = _center(node.get("bounds", ""))
        where = f" @{center[0]},{center[1]}" if center else ""
        flags = ""
        if is_focused:
            flags += " [FOCUSED]"
        if node.get("selected") == "true":
            flags += " [selected]"
        if node.get("checked") == "true":
            flags += " [on]"
        line = f"{label}{flags}{where}"
        if line in seen:
            continue
        seen.add(line)
        items.append(line)
    more = len(items) - MAX_ITEMS
    if more > 0:
        items = [*items[:MAX_ITEMS], f"... and {more} more"]
    return {"focused": focused or None, "on_screen": items}


def _parse_status(output: str) -> tuple[bool | None, str | None]:
    """Whether the screen is awake, and which app is in front, from dumpsys lines."""
    awake: bool | None = None
    app: str | None = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("mWakefulness="):
            awake = line.split("=", 1)[1].strip() == "Awake"
        elif "mCurrentFocus=" in line or "mFocusedApp=" in line:
            if app is None and (m := re.search(r"\s([A-Za-z0-9_.]+)/", line)):
                app = m.group(1)
    return awake, app


def shrink_picture(png: bytes) -> tuple[bytes, str]:
    """A 1280-wide JPEG of the screenshot: much smaller, and plenty for Claude."""
    try:
        from PIL import Image  # noqa: PLC0415  (Pillow ships with Home Assistant)
    except ImportError:
        return png, "image/png"
    with Image.open(BytesIO(png)) as img:
        img = img.convert("RGB")
        if img.width > PICTURE_WIDTH:
            height = round(img.height * PICTURE_WIDTH / img.width)
            img = img.resize((PICTURE_WIDTH, height), Image.Resampling.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue(), "image/jpeg"


def _find_adb(hass: HomeAssistant, entity_id: str) -> Any:
    """The androidtv library object behind [entity_id], or a clear error."""
    entry_id = None
    if registry_entry := er.async_get(hass).async_get(entity_id):
        if registry_entry.platform == ADB_DOMAIN:
            entry_id = registry_entry.config_entry_id
    if entry_id is None:
        choices = [
            f"{e.entity_id} ({e.name or e.original_name or e.entity_id})"
            for e in er.async_get(hass).entities.values()
            if e.platform == ADB_DOMAIN and e.domain == "media_player"
        ]
        hint = (
            f" Use one of: {', '.join(choices)}."
            if choices
            else " Add Home Assistant's Android Debug Bridge integration for the TV."
        )
        raise ServiceValidationError(
            f"{entity_id} is not a TV from the Android Debug Bridge integration.{hint}"
        )
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise HomeAssistantError(
            f"The Android Debug Bridge for {entity_id} isn't loaded."
        )
    runtime = getattr(entry, "runtime_data", None)
    aftv = getattr(runtime, "aftv", None)
    if aftv is None:
        raise HomeAssistantError(
            "This Home Assistant version keeps the TV connection somewhere Alfred "
            "doesn't know yet."
        )
    if not getattr(aftv, "available", True):
        raise HomeAssistantError(
            f"The TV behind {entity_id} isn't reachable over ADB now."
        )
    return aftv


async def async_tv(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Do the action in [data], then report the fresh screen."""
    entity_id: str = data["entity_id"]
    codes = key_codes(data.get("keys") or [])
    text: str | None = data.get("text")
    target: str | None = (data.get("open") or "").strip() or None
    link_command = open_command(target) if target else None
    aftv = _find_adb(hass, entity_id)
    did_something = bool(codes or text or target)

    if target:
        if link_command:
            await aftv.adb_shell(link_command)
        else:
            await aftv.launch_app(target)
    if codes:
        # One `input` per key: slower than one call with many keys, but a TV menu
        # keeps up and none are dropped.
        # A few per call, so no call outlasts the ADB connection's read timeout.
        for start in range(0, len(codes), KEYS_PER_CALL):
            batch = codes[start : start + KEYS_PER_CALL]
            await aftv.adb_shell("; ".join(f"input keyevent {c}" for c in batch))
    if text:
        await aftv.adb_shell(text_command(text))
    wait = data.get("wait")
    if wait is None:
        wait = 1.5 if target else 0.6 if did_something else 0.0
    if wait:
        await asyncio.sleep(wait)

    result: dict[str, Any] = {"entity_id": entity_id}
    want_text = data.get("screen_text", True)
    status_cmd = (
        "dumpsys power | grep -m1 mWakefulness=; "
        "dumpsys window | grep -m2 -E 'mCurrentFocus=|mFocusedApp='"
    )
    if want_text:
        status_cmd += (
            f"; echo {MARK}; uiautomator dump {UI_FILE} >/dev/null 2>&1 "
            f"&& cat {UI_FILE}; rm -f {UI_FILE}"
        )
    output = await aftv.adb_shell(status_cmd) or ""
    status, _, xml = output.partition(MARK)
    awake, app = _parse_status(status)
    result["awake"] = awake
    result["app"] = app
    if awake is False:
        result["note"] = (
            "The TV screen is asleep. Send the key wakeup to turn it on, "
            "then look again."
        )
        return result

    if want_text:
        xml = xml.strip()
        start = xml.find("<?xml")
        if start < 0:
            start = xml.find("<hierarchy")
        if start >= 0:
            try:
                result.update(read_screen(xml[start:]))
            except ValueError as err:
                result["screen_text_problem"] = str(err)
        else:
            result["screen_text_problem"] = (
                "The TV didn't give its screen text (it does not while a video or "
                "animation is playing, and some apps draw without it). Use the picture."
            )

    if data.get("picture", True):
        png = await aftv.adb_screencap()
        if png:
            picture, media_type = await hass.async_add_executor_job(shrink_picture, png)
            result["picture"] = base64.b64encode(picture).decode()
            result["picture_type"] = media_type
        else:
            result["picture_problem"] = "The TV sent no screenshot."
    return result


def async_register_tv_service(hass: HomeAssistant) -> Callable[[], None]:
    """Register alfred.tv; return a function that removes it."""

    async def handle(call: ServiceCall) -> ServiceResponse:
        try:
            return await async_tv(hass, dict(call.data))
        except (ServiceValidationError, HomeAssistantError):
            raise
        except Exception as err:  # the TV's connection can fail in many ways
            LOGGER.debug("alfred.tv failed", exc_info=True)
            raise HomeAssistantError(f"The TV didn't answer: {err}") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_TV,
        handle,
        schema=TV_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return lambda: hass.services.async_remove(DOMAIN, SERVICE_TV)
