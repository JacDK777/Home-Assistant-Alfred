"""Tests for alfred.tv: drive an Android TV and see the fresh screen."""

import base64
from collections.abc import Iterator
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from PIL import Image
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.alfred.const import DOMAIN
from custom_components.alfred.tv import (
    async_register_tv_service,
    key_codes,
    read_screen,
    text_command,
)

TV = "media_player.living_room_tv"

UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" content-desc="" focused="false" bounds="[0,0][1920,1080]">
    <node index="0" text="For you" content-desc="" selected="true" focused="false"
          bounds="[100,40][260,90]" />
    <node index="1" text="" content-desc="" focused="true" bounds="[100,300][400,470]">
      <node index="0" text="Netflix" content-desc="" focused="false"
            bounds="[120,320][380,450]" />
    </node>
    <node index="2" text="YouTube" content-desc="" focused="false"
          bounds="[420,300][720,470]" />
  </node>
</hierarchy>"""


def _png(width: int = 1920, height: int = 1080) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def aftv(hass: HomeAssistant) -> Iterator[MagicMock]:
    """A loaded Android Debug Bridge entry with one TV, awake on the home screen."""
    entry = MockConfigEntry(domain="androidtv", title="192.168.68.173")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    er.async_get(hass).async_get_or_create(
        "media_player",
        "androidtv",
        "38:26:56:4b:91:e9",
        config_entry=entry,
        suggested_object_id="living_room_tv",
        original_name="Living Room TV Screen",
    )
    tv = MagicMock(available=True)
    shell_calls: list[str] = []

    async def shell(cmd: str) -> str:
        shell_calls.append(cmd)
        if cmd.startswith("dumpsys power"):
            return (
                "  mWakefulness=Awake\n"
                "  mCurrentFocus=Window{1 u0 com.google.android.apps.tv.launcherx/"
                "com.google.android.apps.tv.launcherx.home.HomeActivity}\n"
                f"@@ALFRED@@\n{UI}"
            )
        return ""

    tv.adb_shell = AsyncMock(side_effect=shell)
    tv.adb_screencap = AsyncMock(return_value=_png())
    tv.launch_app = AsyncMock()
    tv.shell_calls = shell_calls
    entry.runtime_data = SimpleNamespace(aftv=tv)
    async_register_tv_service(hass)
    yield tv
    # Leave nothing for teardown to unload: the real androidtv integration isn't here.
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def _tv(hass: HomeAssistant, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN,
        "tv",
        {"entity_id": TV, "wait": 0, **data},
        blocking=True,
        return_response=True,
    )


async def test_looks_at_a_fresh_screen(hass: HomeAssistant, aftv: MagicMock) -> None:
    """With nothing to do it just looks: what has focus, the text, and a picture."""
    result = await _tv(hass)
    assert result["awake"] is True
    assert result["app"] == "com.google.android.apps.tv.launcherx"
    assert result["focused"] == "Netflix"
    assert "Netflix [FOCUSED] @250,385" in result["on_screen"]
    assert "For you [selected] @180,65" in result["on_screen"]
    assert "YouTube @570,385" in result["on_screen"]
    assert result["picture_type"] == "image/jpeg"
    with Image.open(BytesIO(base64.b64decode(result["picture"]))) as img:
        assert img.size == (1280, 720)
    aftv.adb_screencap.assert_awaited_once()
    assert len(aftv.shell_calls) == 1


async def test_presses_keys_types_and_opens(
    hass: HomeAssistant, aftv: MagicMock
) -> None:
    """Keys go one by one, text is escaped, and apps and links open first."""
    await _tv(hass, keys=["down*2", "OK", "KEYCODE_BACK"], text="it's 100% fun")
    assert aftv.shell_calls[0] == (
        "input keyevent KEYCODE_DPAD_DOWN; input keyevent KEYCODE_DPAD_DOWN; "
        "input keyevent KEYCODE_DPAD_CENTER; input keyevent KEYCODE_BACK"
    )
    assert aftv.shell_calls[1] == "input text 'it'\\''s%s100%%sfun'"
    await _tv(hass, open="com.netflix.ninja", picture=False)
    aftv.launch_app.assert_awaited_once_with("com.netflix.ninja")
    await _tv(hass, open="https://www.youtube.com/watch?v=abc", screen_text=False)
    assert (
        "am start -a android.intent.action.VIEW -d "
        "'https://www.youtube.com/watch?v=abc'" in aftv.shell_calls
    )


async def test_says_when_the_tv_is_asleep(hass: HomeAssistant, aftv: MagicMock) -> None:
    """An asleep TV gets a note and no screenshot."""
    aftv.adb_shell.side_effect = None
    aftv.adb_shell.return_value = (
        "  mWakefulness=Asleep\n  mCurrentFocus=null\n@@ALFRED@@\n"
    )
    result = await _tv(hass)
    assert result["awake"] is False
    assert "wakeup" in result["note"]
    aftv.adb_screencap.assert_not_awaited()


async def test_no_screen_text_during_video(
    hass: HomeAssistant, aftv: MagicMock
) -> None:
    """When uiautomator gives nothing, the picture still comes back."""
    aftv.adb_shell.side_effect = None
    aftv.adb_shell.return_value = "  mWakefulness=Awake\n@@ALFRED@@\n"
    result = await _tv(hass)
    assert "screen_text_problem" in result
    assert "picture" in result


async def test_refuses_what_is_not_an_adb_tv(
    hass: HomeAssistant, aftv: MagicMock
) -> None:
    """Another entity gets a list of the TVs that work; bad input is refused."""
    with pytest.raises(ServiceValidationError, match="media_player.living_room_tv"):
        await _tv(hass, entity_id="media_player.living_room_tcl_tv_2")
    with pytest.raises(ServiceValidationError):
        await _tv(hass, open="rm -rf /")
    with pytest.raises(ServiceValidationError):
        await _tv(hass, keys=["down; reboot"])
    with pytest.raises(ServiceValidationError):
        await _tv(hass, keys=["down*31"])


async def test_many_keys_go_in_batches(hass: HomeAssistant, aftv: MagicMock) -> None:
    """Twenty presses take three calls, so none runs too long."""
    await _tv(hass, keys=["right*20"], picture=False, screen_text=False)
    presses = [c for c in aftv.shell_calls if c.startswith("input keyevent")]
    assert [c.count("input keyevent") for c in presses] == [8, 8, 4]


def test_key_names() -> None:
    """Friendly names, KEYCODE_ names, short names and numbers all work."""
    assert key_codes(["up", "dpad_up", "KEYCODE_HOME", "4", "right x2"]) == [
        "KEYCODE_DPAD_UP",
        "KEYCODE_DPAD_UP",
        "KEYCODE_HOME",
        "4",
        "KEYCODE_DPAD_RIGHT",
        "KEYCODE_DPAD_RIGHT",
    ]
    assert text_command("a b") == "input text 'a%sb'"


def test_read_screen_caps_long_lists() -> None:
    """A very busy screen is cut short so it stays cheap to read."""
    nodes = "".join(
        f'<node text="Item {i}" bounds="[0,{i}][10,{i + 1}]" />' for i in range(300)
    )
    screen = read_screen(f"<hierarchy>{nodes}</hierarchy>")
    assert screen["focused"] is None
    assert len(screen["on_screen"]) == 121
    assert screen["on_screen"][-1] == "... and 180 more"
