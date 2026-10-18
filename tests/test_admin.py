"""Tests for the Alfred admin LLM API."""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, llm
from homeassistant.setup import async_setup_component

from custom_components.alfred.admin import (
    ADMIN_API_ID,
    TUYA_FOLLOW_INTERVAL,
    async_follow_tuya_local_ips,
)
from custom_components.alfred.const import CONF_GITHUB_TOKEN


@pytest.fixture
def config_store() -> dict[str, Any]:
    """Replace automations.yaml and scripts.yaml with an in-memory store."""
    store: dict[str, Any] = {
        "automations.yaml": [
            {
                "id": "morning",
                "alias": "Morning lights",
                "triggers": [{"trigger": "time", "at": "07:00:00"}],
                "actions": [],
            }
        ],
        "scripts.yaml": {},
    }

    def _read(path: str) -> Any:
        return store.get(path.rsplit("/", 1)[-1])

    def _write(path: str, data: Any) -> None:
        store[path.rsplit("/", 1)[-1]] = data

    with (
        patch("homeassistant.components.config.view._read", side_effect=_read),
        patch("homeassistant.components.config.view._write", side_effect=_write),
    ):
        yield store


async def _api(hass: HomeAssistant, user: MockUser | None) -> llm.APIInstance:
    return await llm.async_get_api(
        hass,
        ADMIN_API_ID,
        llm.LLMContext(
            platform="alfred",
            context=Context(user_id=user.id if user else None),
            language="en",
            assistant="conversation",
            device_id=None,
        ),
    )


async def _call(api: llm.APIInstance, name: str, **args: Any) -> Any:
    result = await api.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))
    return getattr(result, "data", result)


async def test_admin_tools_only_for_admins(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """Administrators get the tools; everyone else gets none."""
    api = await _api(hass, hass_admin_user)
    assert {tool.name for tool in api.tools} == {
        "get_error_log",
        "list_automations_and_scripts",
        "get_automation",
        "save_automation",
        "get_script",
        "save_script",
        "list_dashboards",
        "get_dashboard",
        "save_dashboard",
        "find_entities",
        "check_tuya_local_devices",
        "update_tuya_local_device",
    }

    hass_admin_user.groups = []
    assert (await _api(hass, hass_admin_user)).tools == []
    assert (await _api(hass, None)).tools == []

    # A tool instance obtained earlier still refuses non-admins.
    with pytest.raises(HomeAssistantError, match="administrators"):
        await _call(api, "get_error_log")


async def test_get_error_log(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """Recent errors are returned, newest first."""
    assert await async_setup_component(hass, "system_log", {})
    logging.getLogger("custom_components.alfred").error("Something broke")
    await hass.async_block_till_done()

    result = await _call(await _api(hass, hass_admin_user), "get_error_log", limit=5)
    assert result["entries"][0]["message"] == "Something broke"
    assert result["entries"][0]["level"] == "ERROR"


async def test_edit_automation(
    hass: HomeAssistant,
    setup_alfred,
    hass_admin_user: MockUser,
    config_store: dict[str, Any],
) -> None:
    """Automations are read, validated, saved and reloaded."""
    reloads = async_mock_service(hass, "automation", "reload")
    api = await _api(hass, hass_admin_user)

    result = await _call(api, "get_automation", automation_id="morning")
    assert result["config"]["alias"] == "Morning lights"

    with pytest.raises(HomeAssistantError, match="not found"):
        await _call(api, "get_automation", automation_id="missing")

    new_config = {
        "alias": "Morning lights",
        "triggers": [{"trigger": "time", "at": "06:30:00"}],
        "actions": [],
    }
    result = await _call(
        api, "save_automation", automation_id="morning", config=new_config
    )
    assert result == {"success": True, "automation_id": "morning"}
    assert config_store["automations.yaml"] == [{"id": "morning", **new_config}]
    assert reloads[-1].data == {"id": "morning"}

    result = await _call(api, "save_automation", config={**new_config, "alias": "New"})
    assert len(config_store["automations.yaml"]) == 2
    assert config_store["automations.yaml"][1]["id"] == result["automation_id"]

    with pytest.raises(vol.Invalid):
        await _call(api, "save_automation", config={"triggers": "nonsense"})
    assert len(config_store["automations.yaml"]) == 2


async def test_edit_script(
    hass: HomeAssistant,
    setup_alfred,
    hass_admin_user: MockUser,
    config_store: dict[str, Any],
) -> None:
    """Scripts are saved under their slug and reloaded."""
    reloads = async_mock_service(hass, "script", "reload")
    api = await _api(hass, hass_admin_user)
    config = {"alias": "Good night", "sequence": [{"delay": 1}]}

    await _call(api, "save_script", script_id="good_night", config=config)
    assert config_store["scripts.yaml"] == {"good_night": config}
    assert len(reloads) == 1
    assert (await _call(api, "get_script", script_id="good_night"))["config"] == config


async def test_report_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_models_list,
    hass_admin_user: MockUser,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With a GitHub token, bugs are filed as issues with Alfred's logs attached."""
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_GITHUB_TOKEN: "ghp_test"}
    )
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "system_log", {})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    logging.getLogger("custom_components.alfred").error("Alfred failed")
    logging.getLogger("other").error("Unrelated")
    await hass.async_block_till_done()

    aioclient_mock.post(
        "https://api.github.com/repos/jacdk777/home-assistant-alfred/issues",
        status=201,
        json={"html_url": "https://github.com/jacdk777/home-assistant-alfred/issues/3"},
    )
    result = await _call(
        await _api(hass, hass_admin_user),
        "report_issue",
        title="Lights",
        description="It failed",
    )
    assert result["url"].endswith("/issues/3")

    _, _, body, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer ghp_test"
    assert body["title"] == "Lights"
    assert "Alfred failed" in body["body"]
    assert "Unrelated" not in body["body"]


async def test_edit_dashboard(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser, hass_storage
) -> None:
    """Dashboards are listed, read and saved, keeping the previous version."""
    hass_storage["lovelace_dashboards"] = {
        "version": 1,
        "data": {
            "items": [
                {
                    "id": "main_dash",
                    "url_path": "main-dash",
                    "title": "Main Dash",
                    "mode": "storage",
                    "require_admin": False,
                    "show_in_sidebar": True,
                }
            ]
        },
    }
    old = {"views": [{"cards": [{"type": "tile", "entity": "switch.cloud"}]}]}
    hass_storage["lovelace.main_dash"] = {
        "version": 1,
        "key": "lovelace.main_dash",
        "data": {"config": old},
    }
    assert await async_setup_component(hass, "lovelace", {})
    api = await _api(hass, hass_admin_user)

    result = await _call(api, "list_dashboards")
    assert {"url_path": "main-dash", "title": "Main Dash", "mode": "storage"} in (
        result["dashboards"]
    )
    assert (await _call(api, "get_dashboard", url_path="main-dash"))["config"] == old

    with pytest.raises(HomeAssistantError, match="generated automatically"):
        await _call(api, "get_dashboard", url_path="lovelace")
    with pytest.raises(HomeAssistantError, match="No dashboard"):
        await _call(api, "get_dashboard", url_path="missing")
    with pytest.raises(HomeAssistantError, match="not changed"):
        await _call(api, "get_dashboard", url_path="main-dash", previous=True)

    new = {"views": [{"cards": [{"type": "tile", "entity": "light.local"}]}]}
    result = await _call(api, "save_dashboard", url_path="main-dash", config=new)
    assert result == {"success": True, "url_path": "main-dash"}
    assert (await _call(api, "get_dashboard", url_path="main-dash"))["config"] == new
    previous = await _call(api, "get_dashboard", url_path="main-dash", previous=True)
    assert previous["config"] == old

    with pytest.raises(vol.Invalid):
        await _call(api, "save_dashboard", url_path="main-dash", config={"cards": []})


async def test_find_entities(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """Entities are found by integration, domain and text, with their device."""
    entry = MockConfigEntry(domain="tuya_local")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("tuya_local", "abc123")},
        name="Great Room Light Switch",
    )
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "light",
        "tuya_local",
        "abc123-gang-1",
        suggested_object_id="great_room_gang_1",
        device_id=device.id,
        original_name="Gang 1",
    )
    entities.async_get_or_create(
        "switch", "tuya", "tuya.abc123switch_1", suggested_object_id="great_room_1"
    )
    api = await _api(hass, hass_admin_user)

    result = await _call(api, "find_entities", integration="tuya_local")
    assert result["count"] == 1
    assert result["entities"][0] == {
        "entity_id": "light.great_room_gang_1",
        "name": "Gang 1",
        "integration": "tuya_local",
        "state": None,
        "disabled": False,
        "device_id": device.id,
        "device_name": "Great Room Light Switch",
        "device_identifiers": ["tuya_local:abc123"],
    }
    result = await _call(api, "find_entities", search="great room")
    assert [e["entity_id"] for e in result["entities"]] == [
        "light.great_room_gang_1",
        "switch.great_room_1",
    ]
    result = await _call(api, "find_entities", domain="switch")
    assert [e["entity_id"] for e in result["entities"]] == ["switch.great_room_1"]


async def test_tuya_local_devices(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """Tuya Local devices are checked against the LAN scan and can be updated."""
    api = await _api(hass, hass_admin_user)
    with pytest.raises(HomeAssistantError, match="no devices"):
        await _call(api, "check_tuya_local_devices")

    bulb = MockConfigEntry(
        domain="tuya_local",
        title="Chandelier LB 5",
        data={"device_id": "bulb1", "host": "192.168.68.20", "protocol_version": 3.3},
        options={"host": "192.168.68.20", "protocol_version": 3.3},
    )
    switch = MockConfigEntry(
        domain="tuya_local",
        title="Pantry Light 1",
        data={"device_id": "switch1", "host": "192.168.68.30"},
    )
    bulb.add_to_hass(hass)
    switch.add_to_hass(hass)
    hass.data["tuya_local"] = {
        "bulb1": {"device": MagicMock(has_returned_state=False)},
        "switch1": {"device": MagicMock(has_returned_state=True)},
    }
    scan = {"bulb1": {"gwId": "bulb1", "ip": "192.168.68.44", "version": "3.4"}}

    with patch("custom_components.alfred.admin._scan_lan", return_value=scan):
        result = await _call(api, "check_tuya_local_devices")
    assert result["count"] == 2
    assert result["devices"][0] == {
        "entry_id": bulb.entry_id,
        "name": "Chandelier LB 5",
        "device_id": "bulb1",
        "sub_device": False,
        "host": "192.168.68.20",
        "protocol_version": 3.3,
        "connected": False,
        "announced_ip": "192.168.68.44",
        "announced_version": "3.4",
    }
    assert result["devices"][1]["connected"] is True
    assert result["devices"][1]["announced_ip"] is None

    result = await _call(
        api,
        "update_tuya_local_device",
        entry_id=bulb.entry_id,
        host="192.168.68.44",
        protocol_version="3.4",
    )
    assert result["success"] is True
    assert bulb.data["host"] == bulb.options["host"] == "192.168.68.44"
    assert bulb.data["protocol_version"] == bulb.options["protocol_version"] == 3.4

    await _call(api, "update_tuya_local_device", entry_id=switch.entry_id, host="h2")
    assert switch.data["host"] == "h2"
    assert "host" not in switch.options

    with pytest.raises(HomeAssistantError, match="Give a host"):
        await _call(api, "update_tuya_local_device", entry_id=bulb.entry_id)
    with pytest.raises(HomeAssistantError, match="No Tuya Local device"):
        await _call(api, "update_tuya_local_device", entry_id="nope", host="h")
    with pytest.raises(vol.Invalid):
        await _call(
            api,
            "update_tuya_local_device",
            entry_id=bulb.entry_id,
            protocol_version="9",
        )


async def test_follow_tuya_local_ips(
    hass: HomeAssistant, setup_alfred, freezer
) -> None:
    """Disconnected Tuya Local devices are moved to the IP address they announce."""
    moved = MockConfigEntry(
        domain="tuya_local",
        title="Kitchen DL 1",
        data={"device_id": "bulb1", "host": "192.168.68.20"},
        options={"host": "192.168.68.20"},
    )
    silent = MockConfigEntry(
        domain="tuya_local",
        title="Kitchen DL 2",
        data={"device_id": "bulb2", "host": "192.168.68.21"},
    )
    connected = MockConfigEntry(
        domain="tuya_local",
        title="Pantry Light 1",
        data={"device_id": "switch1", "host": "192.168.68.30"},
    )
    for entry in (moved, silent, connected):
        entry.add_to_hass(hass)
    hass.data["tuya_local"] = {
        "bulb1": {"device": MagicMock(has_returned_state=False)},
        "bulb2": {"device": MagicMock(has_returned_state=False)},
        "switch1": {"device": MagicMock(has_returned_state=True)},
    }
    scan = {
        "bulb1": {"gwId": "bulb1", "ip": "192.168.68.44"},
        "switch1": {"gwId": "switch1", "ip": "192.168.68.99"},
    }

    with patch(
        "custom_components.alfred.admin._scan_lan", return_value=scan
    ) as scan_lan:
        freezer.tick(TUYA_FOLLOW_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    scan_lan.assert_called_once()
    assert moved.data["host"] == moved.options["host"] == "192.168.68.44"
    assert silent.data["host"] == "192.168.68.21"
    assert connected.data["host"] == "192.168.68.30"

    # Nothing is scanned while every device is connected.
    hass.data["tuya_local"]["bulb1"]["device"].has_returned_state = True
    hass.data["tuya_local"]["bulb2"]["device"].has_returned_state = True
    with patch("custom_components.alfred.admin._scan_lan") as scan_lan:
        assert await async_follow_tuya_local_ips(hass) == []
    scan_lan.assert_not_called()

    # A failed scan changes nothing.
    hass.data["tuya_local"]["bulb2"]["device"].has_returned_state = False
    with patch(
        "custom_components.alfred.admin._scan_lan",
        side_effect=HomeAssistantError("busy"),
    ):
        assert await async_follow_tuya_local_ips(hass) == []
