"""Tests for the Alfred config flow."""

from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.alfred.const import (
    CONF_CHAT_MODEL,
    CONF_EFFORT,
    CONF_MAX_TOKENS,
    CONF_WEB_SEARCH,
    DOMAIN,
)


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Alfred depends on conversation, which needs the core integration."""
    assert await async_setup_component(hass, "homeassistant", {})


async def test_user_flow(hass: HomeAssistant, mock_models_list: AsyncMock) -> None:
    """A valid key creates an entry with default options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "sk-test"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: "sk-test"}
    assert result["options"][CONF_CHAT_MODEL] == "claude-haiku-5-5"
    assert result["options"][CONF_LLM_HASS_API] == ["assist"]


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://api.anthropic.com/v1/models")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            lambda: anthropic.AuthenticationError(
                "bad key", response=httpx.Response(401, request=_request()), body=None
            ),
            "invalid_auth",
        ),
        (lambda: anthropic.APIConnectionError(request=_request()), "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, mock_models_list: AsyncMock, error, expected: str
) -> None:
    """Bad keys and network failures show an error and let the user retry."""
    mock_models_list.side_effect = error()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "sk-bad"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_options_flow(hass: HomeAssistant, setup_alfred) -> None:
    """Options can switch model and turn off device control."""
    entry = setup_alfred
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHAT_MODEL: "claude-sonnet-5-5",
            CONF_EFFORT: "medium",
            CONF_MAX_TOKENS: 4096,
            CONF_WEB_SEARCH: True,
            CONF_LLM_HASS_API: [],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CHAT_MODEL] == "claude-sonnet-5-5"
    assert CONF_LLM_HASS_API not in entry.options
