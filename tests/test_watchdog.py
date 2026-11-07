"""Tests for Alfred's watchdog."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, llm

from custom_components.alfred.admin import ADMIN_API_ID
from custom_components.alfred.watchdog import (
    GIVE_UP_AFTER_TRIES,
    GRACE_PERIOD,
    NOTIFY_AFTER_ATTEMPTS,
    async_get_watchdog,
)


def _entity(
    hass: HomeAssistant, entry: MockConfigEntry, object_id: str, state: str
) -> str:
    entity = er.async_get(hass).async_get_or_create(
        "light",
        entry.domain,
        object_id,
        suggested_object_id=object_id,
        config_entry=entry,
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


def _notifications(hass: HomeAssistant) -> dict:
    return persistent_notification._async_get_or_create_notifications(hass)


@pytest.fixture
def reload(hass: HomeAssistant):
    """Record reloads instead of running them."""
    with patch.object(hass.config_entries, "async_reload") as mock:
        yield mock


async def test_reloads_and_learns(
    hass: HomeAssistant, setup_alfred, freezer, reload
) -> None:
    """An entry whose entities stay unavailable is reloaded, then counted as fixed."""
    watchdog = async_get_watchdog(hass)
    hub = MockConfigEntry(domain="rainbird", title="Rain Bird")
    hub.add_to_hass(hass)
    zone = _entity(hass, hub, "zone_1", "unavailable")
    _entity(hass, hub, "zone_2", "on")

    # Not long enough yet.
    await watchdog.async_check()
    reload.assert_not_called()

    freezer.tick(GRACE_PERIOD)
    await watchdog.async_check()
    reload.assert_called_once_with(hub.entry_id)
    assert watchdog.issues[hub.entry_id]["entities"] == [zone]
    assert watchdog.issues[hub.entry_id]["attempts"] == 1

    # Waits before trying again.
    await watchdog.async_check()
    reload.assert_called_once()

    hass.states.async_set(zone, "on")
    await watchdog.async_check()
    assert watchdog.issues == {}
    assert watchdog.learned == {"rainbird": {"tries": 1, "fixes": 1}}
    status = watchdog.status()
    assert [h["outcome"] for h in status["history"]] == [
        "fixed",
        "reloaded, now not_loaded",
    ]


async def test_notifies_and_gives_up(
    hass: HomeAssistant, setup_alfred, freezer, reload
) -> None:
    """After a few failed reloads Alfred tells the user; useless reloads stop."""
    watchdog = async_get_watchdog(hass)
    hub = MockConfigEntry(domain="ring", title="Ring")
    hub.add_to_hass(hass)
    cam = _entity(hass, hub, "front_door", "unavailable")
    freezer.tick(GRACE_PERIOD)

    for _ in range(NOTIFY_AFTER_ATTEMPTS):
        await watchdog.async_check()
        freezer.tick(timedelta(hours=6))
    assert reload.call_count == NOTIFY_AFTER_ATTEMPTS
    notification = _notifications(hass)[f"alfred_watchdog_{hub.entry_id}"]
    assert cam in notification["message"]

    watchdog.learned["ring"] = {"tries": GIVE_UP_AFTER_TRIES, "fixes": 0}
    await watchdog.async_check()
    assert reload.call_count == NOTIFY_AFTER_ATTEMPTS
    assert "never fixed" in watchdog.issues[hub.entry_id]["skipped"]

    # Coming back clears the notification.
    hass.states.async_set(cam, "idle")
    await watchdog.async_check()
    assert f"alfred_watchdog_{hub.entry_id}" not in _notifications(hass)


async def test_skips_ignored_and_expected(
    hass: HomeAssistant, setup_alfred, freezer, reload
) -> None:
    """Ignored entities, TVs and disabled entries are left alone."""
    watchdog = async_get_watchdog(hass)
    tv = MockConfigEntry(domain="androidtv_remote", title="Bedroom TV")
    group = MockConfigEntry(domain="group", title="Kitchen Lights")
    group.add_to_hass(hass)
    _entity(hass, group, "kitchen_lights", "unavailable")
    hub = MockConfigEntry(domain="rainbird", title="Rain Bird")
    off = MockConfigEntry(
        domain="ring", title="Ring", disabled_by=ConfigEntryDisabler.USER
    )
    for entry in (tv, hub, off):
        entry.add_to_hass(hass)
    _entity(hass, tv, "tv", "unavailable")
    zone = _entity(hass, hub, "zone_1", "unavailable")
    _entity(hass, off, "cam", "unavailable")
    freezer.tick(GRACE_PERIOD)

    watchdog.set_ignored(zone, True)
    await watchdog.async_check()
    reload.assert_not_called()

    watchdog.set_ignored(zone, False)
    await watchdog.async_check()
    reload.assert_called_once_with(hub.entry_id)


async def test_tuya_local_without_power(
    hass: HomeAssistant, setup_alfred, freezer, reload
) -> None:
    """A Tuya Local bulb that doesn't announce itself has no power: no reload."""
    watchdog = async_get_watchdog(hass)
    bulb = MockConfigEntry(
        domain="tuya_local",
        title="Kitchen DL 1",
        data={"device_id": "bulb1", "host": "192.168.68.20"},
    )
    bulb.add_to_hass(hass)
    hass.data["tuya_local"] = {"bulb1": {"device": MagicMock(has_returned_state=False)}}
    _entity(hass, bulb, "kitchen_dl_1", "unavailable")
    freezer.tick(GRACE_PERIOD)

    with patch("custom_components.alfred.admin._scan_lan", return_value={}):
        await watchdog.async_check()
    reload.assert_not_called()
    assert watchdog.issues[bulb.entry_id]["attempts"] == 0

    scan = {"bulb1": {"gwId": "bulb1", "ip": "192.168.68.20"}}
    with patch("custom_components.alfred.admin._scan_lan", return_value=scan):
        await watchdog.async_check()
    reload.assert_called_once_with(bulb.entry_id)


async def test_remembers_across_restarts(
    hass: HomeAssistant, setup_alfred, freezer, reload, config_entry
) -> None:
    """Ignored items and what was learned survive a reload of Alfred."""
    watchdog = async_get_watchdog(hass)
    hub = MockConfigEntry(domain="rainbird")
    hub.add_to_hass(hass)
    watchdog.set_ignored(hub.entry_id, True)
    watchdog.learned["rainbird"] = {"tries": 2, "fixes": 1}
    await watchdog._store.async_save(watchdog._data())

    await hass.config_entries.async_unload(config_entry.entry_id)
    assert async_get_watchdog(hass) is None
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    watchdog = async_get_watchdog(hass)
    assert hub.entry_id in watchdog.ignored
    assert watchdog.learned["rainbird"] == {"tries": 2, "fixes": 1}


async def test_watchdog_tools(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """The model can read the watchdog's status and ignore things."""
    api = await llm.async_get_api(
        hass,
        ADMIN_API_ID,
        llm.LLMContext(
            platform="alfred",
            context=Context(user_id=hass_admin_user.id),
            language="en",
            assistant="conversation",
            device_id=None,
        ),
    )
    hub = MockConfigEntry(domain="rainbird")
    hub.add_to_hass(hass)

    async def call(name: str, **args):
        result = await api.async_call_tool(
            llm.ToolInput(tool_name=name, tool_args=args)
        )
        return getattr(result, "data", result)

    status = await call("get_watchdog_status")
    assert status["issues"] == []
    assert "androidtv_remote" in status["ignored_integrations"]

    assert (await call("set_watchdog_ignore", target=hub.entry_id))["ignored"]
    hass.states.async_set("switch.wall", "on")
    hass.states.async_set("light.bulb", "on")
    await call("set_watchdog_feed", switch="switch.wall", entities=["light.bulb"])
    assert (await call("get_watchdog_status"))["powered_by"] == {
        "light.bulb": "switch.wall"
    }
    with pytest.raises(HomeAssistantError, match="Unknown"):
        await call("set_watchdog_feed", switch="switch.nope", entities=["light.bulb"])
    assert hub.entry_id in (await call("get_watchdog_status"))["ignored"]
    await call("set_watchdog_ignore", target=hub.entry_id, ignore=False)
    assert (await call("get_watchdog_status"))["ignored"] == []
    with pytest.raises(HomeAssistantError, match="No entity"):
        await call("set_watchdog_ignore", target="light.nope")


async def test_learns_which_switch_powers_a_bulb(
    hass: HomeAssistant, setup_alfred, freezer, reload
) -> None:
    """A bulb that drops out after its wall switch turns off is left alone."""
    watchdog = async_get_watchdog(hass)
    hub = MockConfigEntry(domain="zha", title="Zigbee")
    hub.add_to_hass(hass)
    bulb = _entity(hass, hub, "bedroom_dl_1", "on")
    hass.states.async_set("switch.bedroom_wall", "on")
    hass.states.async_set("switch.kettle", "on")

    for _ in range(2):
        hass.states.async_set("switch.bedroom_wall", "off")
        freezer.tick(timedelta(seconds=40))
        hass.states.async_set(bulb, "unavailable")
        freezer.tick(timedelta(minutes=5))
        hass.states.async_set("switch.bedroom_wall", "on")
        freezer.tick(timedelta(seconds=20))
        hass.states.async_set(bulb, "on")
        freezer.tick(timedelta(minutes=5))
    assert watchdog.feed_of(bulb) == "switch.bedroom_wall"
    # Only bulbs learn a supply; a TV dropping out after a timer is a coincidence.
    for _ in range(2):
        hass.states.async_set("switch.timer", "on")
        hass.states.async_set("switch.timer", "off")
        hass.states.async_set("media_player.tv", "on")
        hass.states.async_set("media_player.tv", "unavailable")
    assert watchdog.feed_of("media_player.tv") is None
    assert watchdog.status()["powered_by"] == {bulb: "switch.bedroom_wall"}

    # Off at the wall: expected, nothing to repair.
    hass.states.async_set("switch.bedroom_wall", "off")
    hass.states.async_set(bulb, "unavailable")
    freezer.tick(GRACE_PERIOD)
    await watchdog.async_check()
    reload.assert_not_called()
    assert watchdog.issues == {}

    # Switch on but the bulb stays away: that is a fault.
    hass.states.async_set("switch.bedroom_wall", "on")
    await watchdog.async_check()
    reload.assert_called_once_with(hub.entry_id)


async def test_taught_feed(hass: HomeAssistant, setup_alfred, freezer, reload) -> None:
    """A feed the user names counts straight away, for all of a bulb's entities."""
    watchdog = async_get_watchdog(hass)
    hub = MockConfigEntry(domain="zha")
    hub.add_to_hass(hass)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=hub.entry_id, identifiers={("zha", "b1")}
    )
    light = registry.async_get_or_create(
        "light", "zha", "b1-light", config_entry=hub, device_id=device.id
    )
    scene = registry.async_get_or_create(
        "select", "zha", "b1-scene", config_entry=hub, device_id=device.id
    )
    hass.states.async_set(light.entity_id, "unavailable")
    hass.states.async_set(scene.entity_id, "unavailable")
    hass.states.async_set("switch.wall", "off")
    watchdog.teach_feed("switch.wall", [light.entity_id])
    freezer.tick(GRACE_PERIOD)
    await watchdog.async_check()
    reload.assert_not_called()
