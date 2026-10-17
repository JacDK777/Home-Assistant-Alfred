"""Tests for the Alfred admin LLM API."""

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import voluptuous as vol

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from custom_components.alfred.admin import ADMIN_API_ID
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
    return await api.async_call_tool(llm.ToolInput(tool_name=name, tool_args=args))


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
