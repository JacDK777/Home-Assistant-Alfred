"""Config flow for Alfred."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import anthropic
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_create_client
from .const import (
    CONF_CHAT_MODEL,
    CONF_EFFORT,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_WEB_SEARCH,
    DEFAULT_CHAT_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_NAME,
    DEFAULT_PROMPT,
    DEFAULT_WEB_SEARCH,
    DOMAIN,
    EFFORT_OPTIONS,
    LOGGER,
    MODEL_OPTIONS,
)

API_KEYS_URL = "https://platform.claude.com/settings/keys"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

DEFAULT_OPTIONS = {
    CONF_CHAT_MODEL: DEFAULT_CHAT_MODEL,
    CONF_PROMPT: DEFAULT_PROMPT,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_EFFORT: DEFAULT_EFFORT,
    CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
    CONF_WEB_SEARCH: DEFAULT_WEB_SEARCH,
}


async def _async_validate_api_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Return an error key, or None if the API key works."""
    client = await async_create_client(hass, api_key)
    try:
        await client.models.list(timeout=10.0)
    except anthropic.AuthenticationError:
        return "invalid_auth"
    except anthropic.APIConnectionError:
        return "cannot_connect"
    except anthropic.AnthropicError:
        LOGGER.exception("Unexpected error validating Anthropic API key")
        return "unknown"
    return None


class AlfredConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alfred."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Anthropic API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match(user_input)
            if not (
                error := await _async_validate_api_key(
                    self.hass, user_input[CONF_API_KEY]
                )
            ):
                return self.async_create_entry(
                    title=DEFAULT_NAME, data=user_input, options=DEFAULT_OPTIONS
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"api_keys_url": API_KEYS_URL},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a rejected API key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (
                error := await _async_validate_api_key(
                    self.hass, user_input[CONF_API_KEY]
                )
            ):
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return AlfredOptionsFlow()


class AlfredOptionsFlow(OptionsFlow):
    """Alfred options: model, persona, which Home Assistant APIs to expose."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            if not user_input.get(CONF_LLM_HASS_API):
                user_input.pop(CONF_LLM_HASS_API, None)
            return self.async_create_entry(data=user_input)

        options = {**DEFAULT_OPTIONS, **self.config_entry.options}
        hass_apis = [
            SelectOptionDict(label=api.name, value=api.id)
            for api in llm.async_get_apis(self.hass)
        ]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PROMPT,
                    description={"suggested_value": options[CONF_PROMPT]},
                ): TemplateSelector(),
                vol.Optional(
                    CONF_LLM_HASS_API,
                    description={"suggested_value": options.get(CONF_LLM_HASS_API)},
                ): SelectSelector(
                    SelectSelectorConfig(options=hass_apis, multiple=True)
                ),
                vol.Required(
                    CONF_CHAT_MODEL, default=options[CONF_CHAT_MODEL]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=MODEL_OPTIONS,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_EFFORT, default=options[CONF_EFFORT]): SelectSelector(
                    SelectSelectorConfig(
                        options=EFFORT_OPTIONS,
                        translation_key=CONF_EFFORT,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_MAX_TOKENS, default=options[CONF_MAX_TOKENS]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=256, max=64000, step=256, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_WEB_SEARCH, default=options[CONF_WEB_SEARCH]
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
