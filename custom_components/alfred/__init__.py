"""Alfred: a Claude-powered conversation agent for Home Assistant."""

from __future__ import annotations

from functools import partial

import anthropic

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .admin import async_register_admin_api
from .const import DOMAIN, LOGGER

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AlfredConfigEntry = ConfigEntry[anthropic.AsyncAnthropic]


async def async_create_client(
    hass: HomeAssistant, api_key: str
) -> anthropic.AsyncAnthropic:
    """Create an Anthropic client without blocking the event loop.

    Client construction loads TLS certificates from disk, so it runs in the executor.
    """
    return await hass.async_add_executor_job(
        partial(anthropic.AsyncAnthropic, api_key=api_key)
    )


async def async_setup_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    """Set up Alfred from a config entry."""
    client = await async_create_client(hass, entry.data[CONF_API_KEY])
    try:
        await client.models.list(timeout=10.0)
    except anthropic.AuthenticationError as err:
        raise ConfigEntryAuthFailed(err) from err
    except anthropic.AnthropicError as err:
        LOGGER.debug("Anthropic API not reachable: %s", err)
        raise ConfigEntryNotReady(err) from err

    entry.runtime_data = client
    entry.async_on_unload(async_register_admin_api(hass, dict(entry.options)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlfredConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: AlfredConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
