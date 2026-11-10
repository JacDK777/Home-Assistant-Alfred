"""Alfred admin: an opt-in LLM API for looking after Home Assistant itself.

Its tools read the error log, create, edit and delete UI-managed automations,
scripts and scenes, run any Home Assistant action, create, edit and delete
dashboards,
look up and rename entities and devices, add, download and update Marketplace
(HACS) repositories, list the Bluetooth devices Home Assistant hears,
manage dashboard resources, check and fix Tuya Local connections and file GitHub
issues.
They are only offered to Home Assistant administrators.
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    aiohttp_client,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    llm,
    storage,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.json import JsonObjectType

from .const import (
    CONF_GITHUB_REPO,
    CONF_GITHUB_TOKEN,
    DEFAULT_GITHUB_REPO,
    DOMAIN,
    LOGGER,
)

ADMIN_API_ID = "alfred_admin"
ADMIN_API_NAME = "Alfred admin (error log, automations, scripts, issues)"

ADMIN_PROMPT = """You also have admin tools for this Home Assistant installation.
- Before any tool that saves or reports something, tell the user exactly what you
  will change or report and wait for an explicit yes in this conversation.
- Read an automation, script, scene or dashboard before editing it, then save the
  complete updated configuration.
- For everyday device control use the Assist tools. Use call_action for anything
  they cannot do. Ask first before an action that is risky or hard to undo, such
  as locks, alarms, garage doors, restarts or deleting things.
- Never put passwords, API keys or other secrets into automations, scripts or issues.
- GitHub issues are public. Use report_issue for bugs in Alfred itself."""

NOT_ADMIN_PROMPT = """Admin tools are only available to Home Assistant
administrators, and this request did not come from one. If asked to change
settings, explain that an administrator has to ask from their own account."""

MAX_LOG_TEXT = 2000
MAX_ENTITIES = 500
DEFAULT_DASHBOARD = "lovelace"
GITHUB_API = "https://api.github.com"


async def _async_require_admin(
    hass: HomeAssistant, llm_context: llm.LLMContext
) -> None:
    """Raise unless the request came from an administrator."""
    user_id = llm_context.context.user_id if llm_context.context else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_admin:
        raise HomeAssistantError("Only Home Assistant administrators can do this")


def _log_entries(hass: HomeAssistant, limit: int, only_alfred: bool = False) -> list:
    """Return recent warnings and errors from the system log, newest first."""
    handler = hass.data.get("system_log")
    if handler is None:
        raise HomeAssistantError("The system_log integration is not loaded")
    entries = []
    for entry in handler.records.to_list():
        if only_alfred and "alfred" not in entry["name"]:
            continue
        entries.append(
            {
                "logger": entry["name"],
                "level": entry["level"],
                "message": "\n".join(entry["message"])[:MAX_LOG_TEXT],
                "source": "{}:{}".format(*entry["source"]),
                "count": entry["count"],
                "exception": (entry["exception"] or "")[-MAX_LOG_TEXT:],
            }
        )
        if len(entries) >= limit:
            break
    return entries


class AdminTool(llm.Tool):
    """Base class: checks for an admin before running the tool."""

    integration = DOMAIN

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> Any:
        """Check permissions, then run the tool."""
        await _async_require_admin(hass, llm_context)
        data = await self._async_run(hass, self.parameters(tool_input.tool_args))
        if hasattr(llm, "ToolResult"):  # Home Assistant 2026.10 and newer
            return llm.ToolResult(data=data)
        return data

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        raise NotImplementedError


class GetErrorLogTool(AdminTool):
    """Read the Home Assistant error log."""

    name = "get_error_log"
    description = "Get recent warnings and errors from the Home Assistant log."
    parameters = vol.Schema(
        {vol.Optional("limit", default=10): vol.All(int, vol.Range(min=1, max=50))}
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        return {"entries": _log_entries(hass, args["limit"])}


class ListAutomationsAndScriptsTool(AdminTool):
    """List automations, scripts and scenes with the ids the other tools need."""

    name = "list_automations_and_scripts"
    description = (
        "List all automations, scripts and scenes with their ids, names and states. "
        "Use the ids with the get_, save_ and delete_ tools for each of them."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        return {
            "automations": [
                {
                    "automation_id": state.attributes.get("id"),
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "state": state.state,
                }
                for state in hass.states.async_all("automation")
            ],
            "scripts": [
                {
                    "script_id": state.object_id,
                    "entity_id": state.entity_id,
                    "name": state.name,
                }
                for state in hass.states.async_all("script")
            ],
            "scenes": [
                {
                    "scene_id": state.attributes.get("id"),
                    "entity_id": state.entity_id,
                    "name": state.name,
                }
                for state in hass.states.async_all("scene")
            ],
        }


def _automation_view():
    from homeassistant.components.automation.config import async_validate_config_item
    from homeassistant.components.config.automation import EditAutomationConfigView
    from homeassistant.config import AUTOMATION_CONFIG_PATH

    return EditAutomationConfigView(
        "automation",
        "config",
        AUTOMATION_CONFIG_PATH,
        cv.string,
        data_validator=async_validate_config_item,
    )


def _script_view():
    from homeassistant.components.config.script import EditScriptConfigView
    from homeassistant.components.script.config import async_validate_config_item
    from homeassistant.config import SCRIPT_CONFIG_PATH

    return EditScriptConfigView(
        "script",
        "config",
        SCRIPT_CONFIG_PATH,
        cv.slug,
        data_validator=async_validate_config_item,
    )


def _scene_view():
    from homeassistant.components.config.scene import (
        PLATFORM_SCHEMA,
        EditSceneConfigView,
    )
    from homeassistant.config import SCENE_CONFIG_PATH

    return EditSceneConfigView(
        "scene", "config", SCENE_CONFIG_PATH, cv.string, data_schema=PLATFORM_SCHEMA
    )


async def _async_read_item(hass: HomeAssistant, view: Any, key: str) -> dict[str, Any]:
    """Read one automation, script or scene from its UI-managed YAML file."""
    current = await view.read_config(hass)
    if (value := view._get_value(hass, current, key)) is None:
        raise HomeAssistantError(
            f"{key} was not found in {view.path}. Only automations, scripts and "
            "scenes created in the UI can be read, edited or deleted."
        )
    return value


async def _async_write_item(
    hass: HomeAssistant, view: Any, key: str, config: dict[str, Any]
) -> None:
    """Validate and save one automation, script or scene the way the UI editor does."""
    from homeassistant.components.config.view import _write

    if view.data_validator:
        await view.data_validator(hass, key, config)
    else:
        view.data_schema(config)
    async with view.mutation_lock:
        current = await view.read_config(hass)
        view._write_value(hass, current, key, config)
        await hass.async_add_executor_job(_write, hass.config.path(view.path), current)


async def _async_delete_item(
    hass: HomeAssistant, view: Any, domain: str, platform: str, key: str
) -> None:
    """Delete one automation, script or scene the way the UI editor does."""
    from homeassistant.components.config.view import _write

    async with view.mutation_lock:
        current = await view.read_config(hass)
        if view._get_value(hass, current, key) is None:
            raise HomeAssistantError(
                f"{key} was not found in {view.path}. Only automations, scripts and "
                "scenes created in the UI can be deleted."
            )
        view._delete_value(hass, current, key)
        await hass.async_add_executor_job(_write, hass.config.path(view.path), current)
    ent_reg = er.async_get(hass)
    if entity_id := ent_reg.async_get_entity_id(domain, platform, key):
        ent_reg.async_remove(entity_id)


class GetAutomationTool(AdminTool):
    """Read one automation's configuration."""

    name = "get_automation"
    description = "Get the full configuration of an automation by its automation_id."
    parameters = vol.Schema({vol.Required("automation_id"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        config = await _async_read_item(hass, _automation_view(), args["automation_id"])
        return {"config": config}


class SaveAutomationTool(AdminTool):
    """Create or replace an automation."""

    name = "save_automation"
    description = (
        "Create a new automation, or replace an existing one by passing its "
        "automation_id. config uses the automations.yaml format with alias, "
        "description, triggers, conditions and actions. Only call after the user "
        "has confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Optional("automation_id"): cv.string,
            vol.Required("config"): dict,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        key = args.get("automation_id") or uuid.uuid4().hex
        config = {k: v for k, v in args["config"].items() if k != "id"}
        await _async_write_item(hass, _automation_view(), key, config)
        await hass.services.async_call(
            "automation", "reload", {"id": key}, blocking=True
        )
        return {"success": True, "automation_id": key}


class GetScriptTool(AdminTool):
    """Read one script's configuration."""

    name = "get_script"
    description = "Get the full configuration of a script by its script_id."
    parameters = vol.Schema({vol.Required("script_id"): cv.slug})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        config = await _async_read_item(hass, _script_view(), args["script_id"])
        return {"config": config}


class SaveScriptTool(AdminTool):
    """Create or replace a script."""

    name = "save_script"
    description = (
        "Create or replace a script. script_id is a lowercase slug such as "
        "good_night. config uses the scripts.yaml format with alias, description, "
        "fields and sequence. Only call after the user has confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Required("script_id"): cv.slug,
            vol.Required("config"): dict,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        await _async_write_item(hass, _script_view(), args["script_id"], args["config"])
        await hass.services.async_call("script", "reload", blocking=True)
        return {"success": True, "script_id": args["script_id"]}


class DeleteAutomationTool(AdminTool):
    """Delete an automation."""

    name = "delete_automation"
    description = (
        "Delete an automation by its automation_id. Only call after the user has "
        "confirmed which automation to delete."
    )
    parameters = vol.Schema({vol.Required("automation_id"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        key = args["automation_id"]
        await _async_delete_item(
            hass, _automation_view(), "automation", "automation", key
        )
        return {"success": True, "automation_id": key}


class DeleteScriptTool(AdminTool):
    """Delete a script."""

    name = "delete_script"
    description = (
        "Delete a script by its script_id. Only call after the user has confirmed "
        "which script to delete."
    )
    parameters = vol.Schema({vol.Required("script_id"): cv.slug})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        key = args["script_id"]
        await _async_delete_item(hass, _script_view(), "script", "script", key)
        return {"success": True, "script_id": key}


class GetSceneTool(AdminTool):
    """Read one scene's configuration."""

    name = "get_scene"
    description = "Get the full configuration of a scene by its scene_id."
    parameters = vol.Schema({vol.Required("scene_id"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        config = await _async_read_item(hass, _scene_view(), args["scene_id"])
        return {"config": config}


class SaveSceneTool(AdminTool):
    """Create or replace a scene."""

    name = "save_scene"
    description = (
        "Create a new scene, or replace an existing one by passing its scene_id. "
        "config uses the scenes.yaml format: name, optional icon, and entities "
        "mapping each entity_id to the state and attributes it should have, such "
        'as {"light.kitchen": {"state": "on", "brightness": 128}}. Only '
        "call after the user has confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Optional("scene_id"): cv.string,
            vol.Required("config"): dict,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        key = args.get("scene_id") or uuid.uuid4().hex
        config = {k: v for k, v in args["config"].items() if k != "id"}
        await _async_write_item(hass, _scene_view(), key, config)
        await hass.services.async_call("scene", "reload", blocking=True)
        return {"success": True, "scene_id": key}


class DeleteSceneTool(AdminTool):
    """Delete a scene."""

    name = "delete_scene"
    description = (
        "Delete a scene by its scene_id. Only call after the user has confirmed "
        "which scene to delete."
    )
    parameters = vol.Schema({vol.Required("scene_id"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        key = args["scene_id"]
        await _async_delete_item(hass, _scene_view(), "scene", "homeassistant", key)
        return {"success": True, "scene_id": key}


class CallActionTool(AdminTool):
    """Run any Home Assistant action (service)."""

    name = "call_action"
    description = (
        "Run any Home Assistant action, such as light.turn_on, "
        "media_player.play_media, input_number.set_value or scene.create, for "
        "anything the Assist tools cannot do. target can hold entity_id, "
        "device_id, area_id or floor_id. Set return_response for actions that "
        "return data, such as weather.get_forecasts or calendar.get_events."
    )
    parameters = vol.Schema(
        {
            vol.Required("domain"): cv.string,
            vol.Required("action"): cv.string,
            vol.Optional("target"): dict,
            vol.Optional("data"): dict,
            vol.Optional("return_response"): cv.boolean,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        domain, action = args["domain"], args["action"]
        if not hass.services.has_service(domain, action):
            raise HomeAssistantError(f"There is no action {domain}.{action}")
        response = await hass.services.async_call(
            domain,
            action,
            args.get("data") or {},
            blocking=True,
            target=args.get("target"),
            return_response=args.get("return_response", False),
        )
        result: dict[str, Any] = {"success": True}
        if response is not None:
            result["response"] = response
        return result


def _dashboards(hass: HomeAssistant) -> dict[str | None, Any]:
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    if LOVELACE_DATA not in hass.data:
        raise HomeAssistantError("The lovelace integration is not loaded")
    return hass.data[LOVELACE_DATA].dashboards


def _dashboard(hass: HomeAssistant, url_path: str) -> Any:
    """Return a dashboard by url_path; "lovelace" is the default dashboard."""
    dashboards = _dashboards(hass)
    key = None if url_path == DEFAULT_DASHBOARD else url_path
    if key not in dashboards:
        raise HomeAssistantError(
            f"No dashboard with url_path {url_path}. Use list_dashboards."
        )
    return dashboards[key]


def _backup_store(hass: HomeAssistant, url_path: str) -> storage.Store:
    return storage.Store(hass, 1, f"{DOMAIN}.dashboard_backup.{url_path}")


class ListDashboardsTool(AdminTool):
    """List dashboards with the url_path the other dashboard tools need."""

    name = "list_dashboards"
    description = (
        "List all dashboards with their url_path, title, mode, icon and sidebar "
        "settings. Use the url_path with get_dashboard, save_dashboard, "
        "update_dashboard and delete_dashboard."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        return {
            "dashboards": [
                {
                    "url_path": key or DEFAULT_DASHBOARD,
                    "title": (dash.config or {}).get("title", "Overview"),
                    "mode": dash.mode,
                    "icon": (dash.config or {}).get("icon"),
                    "show_in_sidebar": (dash.config or {}).get("show_in_sidebar", True),
                    "require_admin": (dash.config or {}).get("require_admin", False),
                }
                for key, dash in _dashboards(hass).items()
            ]
        }


class GetDashboardTool(AdminTool):
    """Read a dashboard's views and cards."""

    name = "get_dashboard"
    description = (
        "Get the full configuration (views and cards) of a dashboard by url_path. "
        "Set previous to true to get the version saved before Alfred's last change."
    )
    parameters = vol.Schema(
        {
            vol.Required("url_path"): cv.string,
            vol.Optional("previous", default=False): cv.boolean,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        from homeassistant.components.lovelace.const import ConfigNotFound

        if args["previous"]:
            backup = await _backup_store(hass, args["url_path"]).async_load()
            if backup is None:
                raise HomeAssistantError("Alfred has not changed this dashboard yet")
            return {"config": backup}
        dashboard = _dashboard(hass, args["url_path"])
        try:
            config = await dashboard.async_load(False)
        except ConfigNotFound:
            raise HomeAssistantError(
                "This dashboard is generated automatically and has no cards to edit "
                "yet. Take control of it in the UI first."
            ) from None
        return {"mode": dashboard.mode, "config": config}


class SaveDashboardTool(AdminTool):
    """Replace a dashboard's views and cards, keeping the previous version."""

    name = "save_dashboard"
    description = (
        "Replace the full configuration of a UI-managed dashboard. config is the "
        "complete dashboard with its views, as returned by get_dashboard. The "
        "previous version is kept and can be read with get_dashboard previous=true. "
        "Only call after the user has confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Required("url_path"): cv.string,
            vol.Required("config"): vol.Schema(
                {vol.Required("views"): list}, extra=True
            ),
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        from homeassistant.components.lovelace.const import MODE_STORAGE, ConfigNotFound

        dashboard = _dashboard(hass, args["url_path"])
        if dashboard.mode != MODE_STORAGE:
            raise HomeAssistantError(
                "This dashboard is defined in YAML files, so it can't be saved here"
            )
        try:
            previous = await dashboard.async_load(False)
        except ConfigNotFound:
            previous = None
        if previous is not None:
            await _backup_store(hass, args["url_path"]).async_save(previous)
        await dashboard.async_save(args["config"])
        return {"success": True, "url_path": args["url_path"]}


def _dashboards_collection(hass: HomeAssistant) -> Any:
    """Return the collection behind Settings > Dashboards.

    Home Assistant does not keep it in hass.data, but the websocket command that
    lists dashboards is a method of the object that owns it.
    """
    from homeassistant.components.websocket_api import DOMAIN as WS_DOMAIN

    _dashboards(hass)  # raises when lovelace is not loaded
    handler = hass.data.get(WS_DOMAIN, {}).get("lovelace/dashboards/list", (None,))[0]
    collection = getattr(getattr(handler, "__self__", None), "storage_collection", None)
    if collection is None:
        raise HomeAssistantError("Dashboards can't be managed on this Home Assistant")
    return collection


def _dashboard_item_id(hass: HomeAssistant, collection: Any, url_path: str) -> str:
    """Return the collection id of a UI-managed dashboard, by url_path."""
    for item_id, item in collection.data.items():
        if item.get("url_path") == url_path:
            return item_id
    if url_path in _dashboards(hass):
        raise HomeAssistantError(
            f"{url_path} is defined in YAML or built in, so it can't be changed here"
        )
    raise HomeAssistantError(
        f"No dashboard with url_path {url_path}. Use list_dashboards."
    )


def _dashboard_settings(args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: args[key]
        for key in ("title", "icon", "show_in_sidebar", "require_admin")
        if key in args
    }


class CreateDashboardTool(AdminTool):
    """Add a new dashboard, as Settings > Dashboards > Add dashboard does."""

    name = "create_dashboard"
    description = (
        "Create a new UI-managed dashboard, shown in the sidebar. url_path is the "
        "web address and must contain a hyphen, for example house-stats; it is made "
        "from the title when left out. Optionally pass config (views and cards, as "
        "for save_dashboard) to fill it straight away. Only call after the user has "
        "confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Required("title"): cv.string,
            vol.Optional("url_path"): cv.string,
            vol.Optional("icon"): cv.icon,
            vol.Optional("show_in_sidebar", default=True): cv.boolean,
            vol.Optional("require_admin", default=False): cv.boolean,
            vol.Optional("config"): vol.Schema(
                {vol.Required("views"): list}, extra=True
            ),
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        from homeassistant.util import slugify

        collection = _dashboards_collection(hass)
        url_path = args.get("url_path") or slugify(args["title"], separator="-")
        if "-" not in url_path:
            url_path = f"{url_path}-dashboard"
        if url_path in _dashboards(hass):
            raise HomeAssistantError(
                f"A dashboard with url_path {url_path} already exists"
            )
        try:
            await collection.async_create_item(
                {"url_path": url_path, "mode": "storage", **_dashboard_settings(args)}
            )
        except vol.Invalid as err:
            raise HomeAssistantError(f"Can't create the dashboard: {err}") from err
        if "config" in args:
            await _dashboard(hass, url_path).async_save(args["config"])
        return {"success": True, "url_path": url_path}


class UpdateDashboardTool(AdminTool):
    """Change a dashboard's title, icon or sidebar settings."""

    name = "update_dashboard"
    description = (
        "Change a UI-managed dashboard's title (its name in the sidebar), icon "
        "(for example mdi:chart-bar), whether it shows in the sidebar, and whether "
        "only administrators can open it. Its views and cards are changed with "
        "save_dashboard. The url_path stays the same. Only call after the user has "
        "confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Required("url_path"): cv.string,
            vol.Optional("title"): cv.string,
            vol.Optional("icon"): vol.Any(cv.icon, None),
            vol.Optional("show_in_sidebar"): cv.boolean,
            vol.Optional("require_admin"): cv.boolean,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        collection = _dashboards_collection(hass)
        item_id = _dashboard_item_id(hass, collection, args["url_path"])
        changes = _dashboard_settings(args)
        if not changes:
            raise HomeAssistantError("Nothing to change")
        item = await collection.async_update_item(item_id, changes)
        return {"success": True, "dashboard": dict(item)}


class DeleteDashboardTool(AdminTool):
    """Delete a dashboard, keeping a copy of its cards."""

    name = "delete_dashboard"
    description = (
        "Delete a UI-managed dashboard by url_path. A copy of its views and cards is "
        "kept and can be read with get_dashboard previous=true after creating a "
        "dashboard with the same url_path. Only call after the user has confirmed "
        "the change."
    )
    parameters = vol.Schema({vol.Required("url_path"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        from homeassistant.components.lovelace.const import ConfigNotFound

        collection = _dashboards_collection(hass)
        item_id = _dashboard_item_id(hass, collection, args["url_path"])
        try:
            config = await _dashboard(hass, args["url_path"]).async_load(False)
        except ConfigNotFound:
            config = None
        if config is not None:
            await _backup_store(hass, args["url_path"]).async_save(config)
        await collection.async_delete_item(item_id)
        return {"success": True, "url_path": args["url_path"]}


class FindEntitiesTool(AdminTool):
    """Look up entities with the integration and device they come from."""

    name = "find_entities"
    description = (
        "Find entities in the entity registry, including ones not exposed to the "
        "assistant. Filter by integration (for example tuya or tuya_local), domain "
        "(for example light), or a search text matched against entity id, name and "
        "device name. Returns each entity's integration and device."
    )
    parameters = vol.Schema(
        {
            vol.Optional("integration"): cv.string,
            vol.Optional("domain"): cv.string,
            vol.Optional("search"): cv.string,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        devices = dr.async_get(hass)
        search = (args.get("search") or "").lower().replace("_", " ")
        found = []
        for entry in er.async_get(hass).entities.values():
            if args.get("integration") and entry.platform != args["integration"]:
                continue
            if args.get("domain") and entry.domain != args["domain"]:
                continue
            device = devices.async_get(entry.device_id) if entry.device_id else None
            device_name = device and (device.name_by_user or device.name)
            name = entry.name or entry.original_name
            if search and not any(
                search in (text or "").lower().replace("_", " ")
                for text in (entry.entity_id, name, device_name)
            ):
                continue
            state = hass.states.get(entry.entity_id)
            found.append(
                {
                    "entity_id": entry.entity_id,
                    "name": name,
                    "integration": entry.platform,
                    "state": state.state if state else None,
                    "disabled": entry.disabled_by is not None,
                    "device_id": entry.device_id,
                    "device_name": device_name,
                    "device_identifiers": sorted(
                        f"{domain}:{ident}" for domain, ident in device.identifiers
                    )
                    if device
                    else [],
                }
            )
        found.sort(key=lambda e: e["entity_id"])
        return {
            "count": len(found),
            "entities": found[:MAX_ENTITIES],
            "truncated": len(found) > MAX_ENTITIES,
        }


class RenameEntityTool(AdminTool):
    """Change the name an entity is shown with."""

    name = "rename_entity"
    description = (
        "Change the display name of an entity, by its entity_id from find_entities. "
        "The entity_id stays the same, so automations and dashboards keep working. "
        "An empty name goes back to the integration's own name. Only call after "
        "the user has confirmed the change."
    )
    parameters = vol.Schema(
        {vol.Required("entity_id"): cv.entity_id, vol.Required("name"): str}
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        entities = er.async_get(hass)
        if entities.async_get(args["entity_id"]) is None:
            raise HomeAssistantError(
                "No entity with that entity_id in the entity registry. "
                "Use find_entities."
            )
        name = args["name"].strip() or None
        entry = entities.async_update_entity(args["entity_id"], name=name)
        return {
            "success": True,
            "entity_id": entry.entity_id,
            "name": entry.name or entry.original_name,
        }


class SetEntityEnabledTool(AdminTool):
    """Turn a disabled entity on, or turn an entity off."""

    name = "set_entity_enabled"
    description = (
        "Enable or disable an entity, by its entity_id from find_entities. Use it to "
        "turn on a setting an integration hides by default, such as a Tuya switch's "
        "Power-on behavior. Home Assistant reloads the integration about 30 seconds "
        "after an entity is enabled, and the entity has a state after that. Only "
        "call after the user has asked for the change."
    )
    parameters = vol.Schema(
        {vol.Required("entity_id"): cv.entity_id, vol.Required("enabled"): bool}
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        entities = er.async_get(hass)
        if entities.async_get(args["entity_id"]) is None:
            raise HomeAssistantError(
                "No entity with that entity_id in the entity registry. "
                "Use find_entities."
            )
        disabled_by = None if args["enabled"] else er.RegistryEntryDisabler.USER
        entry = entities.async_update_entity(args["entity_id"], disabled_by=disabled_by)
        return {
            "success": True,
            "entity_id": entry.entity_id,
            "enabled": entry.disabled_by is None,
        }


class RenameDeviceTool(AdminTool):
    """Change the name a device is shown with."""

    name = "rename_device"
    description = (
        "Change the display name of a device, by its device_id from find_entities. "
        "Its entity ids stay the same. An empty name goes back to the "
        "integration's own name. Only call after the user has confirmed the change."
    )
    parameters = vol.Schema(
        {vol.Required("device_id"): cv.string, vol.Required("name"): str}
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        devices = dr.async_get(hass)
        if devices.async_get(args["device_id"]) is None:
            raise HomeAssistantError(
                "No device with that device_id. Use find_entities."
            )
        name = args["name"].strip() or None
        device = devices.async_update_device(args["device_id"], name_by_user=name)
        return {
            "success": True,
            "device_id": device.id,
            "name": device.name_by_user or device.name,
        }


HACS = "hacs"
MARKETPLACE = "marketplace"
# Sent by the Marketplace after a first install so its update entities appear
MARKETPLACE_SIGNAL_INSTALLED = "marketplace_repository_installed"
MAX_HACS_RESULTS = 50


def _store(hass: HomeAssistant) -> tuple[str, Any]:
    """Return the community store: the built-in Marketplace, or HACS before it.

    Home Assistant 2026.11 took HACS into core as the Marketplace. Its manager
    lives on the config entry and keeps the same repository objects as HACS.
    """
    if entries := hass.config_entries.async_loaded_entries(MARKETPLACE):
        return MARKETPLACE, entries[0].runtime_data
    if (hacs := hass.data.get(HACS)) is not None:
        return HACS, hacs
    raise HomeAssistantError(
        "Neither the Marketplace nor HACS is set up in Home Assistant"
    )


def _hacs_repository_info(repository: Any) -> dict[str, Any]:
    return {
        "repository_id": str(repository.data.id),
        "full_name": repository.data.full_name,
        "name": repository.display_name,
        "category": str(repository.data.category),
        "downloaded": repository.data.installed,
        "installed_version": repository.display_installed_version,
        "available_version": repository.display_available_version,
        "update_available": repository.pending_update,
    }


def _hacs_matches(repository: Any, search: str) -> bool:
    if not search:
        return repository.data.installed
    return any(
        search in (text or "").lower()
        for text in (
            repository.display_name,
            repository.data.full_name,
            repository.data.description,
        )
    )


class ListHacsRepositoriesTool(AdminTool):
    """List Marketplace (HACS) repositories with installed and newest versions."""

    name = "list_hacs_repositories"
    description = (
        "List repositories in the Marketplace (Home Assistant's built-in community "
        "store, formerly HACS): dashboard cards, integrations, themes and so on, "
        "with their repository_id, installed version and newest version. Without a "
        "search text it lists the downloaded ones; with one it searches every "
        "repository the store knows about by name, full name and description."
    )
    parameters = vol.Schema({vol.Optional("search"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        search = (args.get("search") or "").lower()
        _, store = _store(hass)
        found = [
            _hacs_repository_info(repository)
            for repository in store.repositories.list_all
            if _hacs_matches(repository, search)
        ]
        found.sort(key=lambda r: (not r["downloaded"], r["name"] or ""))
        return {
            "count": len(found),
            "repositories": found[:MAX_HACS_RESULTS],
            "truncated": len(found) > MAX_HACS_RESULTS,
        }


class DownloadHacsRepositoryTool(AdminTool):
    """Download or update a Marketplace (HACS) repository."""

    name = "download_hacs_repository"
    description = (
        "Download a Marketplace (formerly HACS) repository by its repository_id from "
        "list_hacs_repositories, or update it if it is already downloaded. Gets the "
        "newest version unless a version is given. The Marketplace adds dashboard "
        "cards to the "
        "dashboard resources itself; they work once the page is refreshed. "
        "Integrations need a Home Assistant restart. This installs code from "
        "GitHub, so only call after the user has confirmed the exact repository "
        "and version."
    )
    parameters = vol.Schema(
        {
            vol.Required("repository_id"): cv.string,
            vol.Optional("version"): cv.string,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        kind, store = _store(hass)
        repository = store.repositories.get_by_id(args["repository_id"])
        if repository is None:
            raise HomeAssistantError(
                "No repository with that repository_id. Use list_hacs_repositories."
            )
        if kind == MARKETPLACE and not store.warning_acceptances:
            raise HomeAssistantError(
                "The Marketplace's community-code warning has not been accepted "
                "yet. Open Settings > Marketplace once and accept it first."
            )
        was_downloaded = repository.data.installed
        previous_version = repository.display_installed_version
        try:
            # Fetch the newest release information first, as a refresh does.
            await repository.update_repository(ignore_issues=True, force=True)
            if kind == MARKETPLACE:
                await repository.async_install_repository(ref=args.get("version"))
            else:
                await repository.async_download_repository(ref=args.get("version"))
        except Exception as err:  # noqa: BLE001 - the stores raise their own types
            raise HomeAssistantError(f"Could not download it: {err}") from err
        if kind == MARKETPLACE:
            # The install already saved the Marketplace's data
            if not was_downloaded and repository.data.installed:
                async_dispatcher_send(hass, MARKETPLACE_SIGNAL_INSTALLED, repository)
        else:
            if not was_downloaded:
                await store.async_recreate_entities()
            await store.data.async_write()
        info = _hacs_repository_info(repository)
        next_step = {
            "plugin": "Refresh the dashboard page in the browser or app",
            "integration": "Restart Home Assistant",
        }.get(info["category"])
        return {
            "success": True,
            **info,
            "previous_version": previous_version if was_downloaded else None,
            "next_step": next_step,
        }


_GITHUB_REPOSITORY = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class AddHacsCustomRepositoryTool(AdminTool):
    """Add a GitHub repository to the Marketplace (HACS) as a custom repository."""

    name = "add_hacs_custom_repository"
    description = (
        "Add a GitHub repository that is not in the Marketplace's default list "
        "(formerly a HACS custom repository), such as an integration a README says "
        "to add as a custom repository. Give owner/name or the GitHub URL and its "
        "category. It only adds it to the list, nothing is downloaded: then use "
        "download_hacs_repository with the repository_id it returns. Only call after "
        "the user has confirmed the exact repository."
    )
    parameters = vol.Schema(
        {
            vol.Required("repository"): cv.string,
            vol.Required("category"): vol.In(
                ["integration", "plugin", "theme", "template", "python_script"]
            ),
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        kind, store = _store(hass)
        match = _GITHUB_REPOSITORY.match(args["repository"].strip())
        if match is None:
            raise HomeAssistantError(
                "Give the repository as owner/name or its GitHub URL"
            )
        full_name = match["repository"]
        category = args["category"]
        if kind == MARKETPLACE and not store.warning_acceptances:
            raise HomeAssistantError(
                "The Marketplace's community-code warning has not been accepted "
                "yet. Open Settings > Marketplace once and accept it first."
            )
        if category not in store.common.categories:
            raise HomeAssistantError(
                f"The {category} category is not turned on in the Marketplace"
            )
        renamed = store.common.renamed_repositories.get(full_name)
        if renamed:
            full_name = renamed
        existing = store.repositories.get_by_full_name(full_name)
        if existing is not None:
            return {
                "success": True,
                "already_listed": True,
                **_hacs_repository_info(existing),
            }
        store.common.skip.discard(full_name)
        try:
            errors = await store.async_register_repository(
                repository_full_name=full_name, category=category
            )
        except Exception as err:  # noqa: BLE001 - the stores raise their own types
            raise HomeAssistantError(f"Could not add it: {err}") from err
        if errors:
            raise HomeAssistantError(f"Could not add it: {errors[0]}")
        repository = store.repositories.get_by_full_name(full_name)
        if repository is None:
            raise HomeAssistantError(
                f"{full_name} was not added. Check the Home Assistant log."
            )
        await store.data.async_write()
        return {
            "success": True,
            "already_listed": False,
            **_hacs_repository_info(repository),
            "next_step": "download_hacs_repository with this repository_id",
        }


MAX_BLUETOOTH_RESULTS = 50


class ListBluetoothDevicesTool(AdminTool):
    """List the Bluetooth devices Home Assistant hears right now."""

    name = "list_bluetooth_devices"
    description = (
        "List the Bluetooth (BLE) devices Home Assistant currently hears through "
        "its adapters and ESPHome Bluetooth proxies: name, MAC address, signal "
        "strength (rssi, closer to 0 is stronger), which adapter or proxy hears it "
        "best, whether it accepts connections, and any discovered integration "
        "waiting to be set up for it. Use it to find a Bluetooth light or sensor "
        "and work out which integration it needs. Search matches name or address."
    )
    parameters = vol.Schema({vol.Optional("search"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        if "bluetooth" not in hass.config.components:
            raise HomeAssistantError("The Bluetooth integration is not set up")
        from homeassistant.components import bluetooth

        search = (args.get("search") or "").lower()
        scanners = {
            scanner.source: scanner.name
            for scanner in bluetooth.async_current_scanners(hass)
        }
        discovered: dict[str, list[str]] = {}
        for flow in hass.config_entries.flow.async_progress():
            if flow.get("context", {}).get("source") != "bluetooth":
                continue
            unique_id = str(flow["context"].get("unique_id") or "").upper()
            discovered.setdefault(unique_id, []).append(flow["handler"])
        found = []
        for info in bluetooth.async_discovered_service_info(hass, connectable=False):
            name = info.name or ""
            if (
                search
                and search not in name.lower()
                and search not in (info.address.lower())
            ):
                continue
            found.append(
                {
                    "name": name,
                    "address": info.address,
                    "rssi": info.rssi,
                    "heard_by": scanners.get(info.source, info.source),
                    "connectable": info.connectable,
                    "service_uuids": list(info.service_uuids),
                    "manufacturer_ids": sorted(info.manufacturer_data),
                    "discovered_integrations": discovered.get(info.address.upper(), []),
                }
            )
        found.sort(key=lambda d: d["rssi"] or -999, reverse=True)
        return {
            "count": len(found),
            "scanners": sorted(scanners.values()),
            "devices": found[:MAX_BLUETOOTH_RESULTS],
            "truncated": len(found) > MAX_BLUETOOTH_RESULTS,
        }


async def _async_resources(hass: HomeAssistant) -> Any:
    """Return the UI-managed dashboard resources collection, loaded."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    if LOVELACE_DATA not in hass.data:
        raise HomeAssistantError("The lovelace integration is not loaded")
    resources = hass.data[LOVELACE_DATA].resources
    if not hasattr(resources, "async_delete_item"):
        raise HomeAssistantError(
            "Dashboard resources are defined in YAML, so they can't be changed here"
        )
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    return resources


class ListDashboardResourcesTool(AdminTool):
    """List the files dashboards load, such as custom cards."""

    name = "list_dashboard_resources"
    description = (
        "List the dashboard resources: the JavaScript modules and CSS files that "
        "dashboards load, such as custom cards from HACS (/hacsfiles/...). A custom "
        "card shows a configuration error when its file is missing here, or when "
        "an old file for the same card is loaded."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        resources = await _async_resources(hass)
        return {
            "resources": [
                {"resource_id": item["id"], "url": item["url"], "type": item["type"]}
                for item in resources.async_items()
            ]
        }


class RemoveDashboardResourceTool(AdminTool):
    """Remove a dashboard resource, such as an old custom card file."""

    name = "remove_dashboard_resource"
    description = (
        "Remove a dashboard resource by its resource_id from "
        "list_dashboard_resources, for example an old custom card file left behind "
        "after an update. Cards that need it stop working. Only call after the "
        "user has confirmed the change."
    )
    parameters = vol.Schema({vol.Required("resource_id"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        resources = await _async_resources(hass)
        item = next(
            (i for i in resources.async_items() if i["id"] == args["resource_id"]),
            None,
        )
        if item is None:
            raise HomeAssistantError(
                "No dashboard resource with that resource_id. "
                "Use list_dashboard_resources."
            )
        await resources.async_delete_item(args["resource_id"])
        return {"success": True, "removed_url": item["url"]}


TUYA_LOCAL = "tuya_local"
TUYA_CLOUD = "tuya"
TUYA_PROTOCOLS = ["auto", "3.1", "3.2", "3.3", "3.4", "3.5", "3.22"]


def _scan_lan() -> dict[str, dict[str, Any]]:
    """Listen for Tuya devices announcing themselves on the LAN (blocking).

    Returns tinytuya's scan results keyed by device id.
    """
    try:
        import tinytuya
    except ImportError:
        raise HomeAssistantError("Tuya Local is not installed") from None
    try:
        found = tinytuya.deviceScan(verbose=False, poll=False)
    except OSError as err:
        raise HomeAssistantError(f"The network scan failed: {err}") from err
    return {info["gwId"]: info for info in found.values() if info.get("gwId")}


def _tuya_local_connected(hass: HomeAssistant, entry: Any) -> bool:
    """Whether Tuya Local has had a reply from the device."""
    device_id, cid = entry.data.get("device_id"), entry.data.get("device_cid")
    key = f"{device_id}/{cid}" if device_id and cid else cid or device_id
    device = (hass.data.get(TUYA_LOCAL, {}).get(key) or {}).get("device")
    return bool(device is not None and device.has_returned_state)


def _update_tuya_local_entry(
    hass: HomeAssistant, entry: Any, changes: dict[str, Any]
) -> None:
    """Save a new host or protocol version for a Tuya Local device."""
    # Tuya Local's options flow stores these in options, which override data,
    # so write them wherever they take effect. Its update listener reloads it.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, **changes},
        options={
            **entry.options,
            **{k: v for k, v in changes.items() if k in entry.options},
        },
    )
    if entry.state is ConfigEntryState.SETUP_RETRY:
        # Its update listener only exists once set up, so a device that is
        # waiting to retry would keep failing until its next back-off attempt.
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _fetch_cloud_local_keys(manager: Any) -> dict[str, str]:
    """Ask the Tuya cloud for the current local key of every device (blocking).

    Uses the signed-in session of Home Assistant's own Tuya integration and
    reads the device list directly, so its device cache is left alone.
    """
    keys: dict[str, str] = {}
    for home in getattr(manager, "user_homes", None) or []:
        response = manager.customer_api.get(
            "/v1.0/m/life/ha/home/devices", {"homeId": home.id}
        )
        if not response or not response.get("success"):
            continue
        for item in response.get("result") or []:
            if item.get("id") and item.get("local_key"):
                keys[item["id"]] = item["local_key"]
    return keys


async def _async_cloud_local_keys(hass: HomeAssistant) -> dict[str, str] | None:
    """Current local keys from the Tuya cloud integration, by device id.

    Returns None when the Tuya cloud integration is not set up and loaded.
    """
    managers = _cloud_managers(hass)
    if not managers:
        return None
    keys: dict[str, str] = {}
    for manager in managers:
        try:
            keys.update(
                await hass.async_add_executor_job(_fetch_cloud_local_keys, manager)
            )
        except Exception as err:  # noqa: BLE001 - the cloud SDK raises anything
            LOGGER.debug("Could not read local keys from the Tuya cloud: %s", err)
    return keys


def _new_local_key(entry: Any, keys: dict[str, str] | None) -> str | None:
    """The cloud's local key for a Tuya Local device, when it differs."""
    config = {**entry.data, **entry.options}
    if not keys or config.get("device_cid"):
        # Sub-devices use their gateway's key.
        return None
    key = keys.get(config.get("device_id"))
    return key if key and key != config.get("local_key") else None


async def async_scan_tuya_local(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Scan the LAN for Tuya devices; an empty result when the scan fails."""
    try:
        return await hass.async_add_executor_job(_scan_lan)
    except HomeAssistantError as err:
        LOGGER.debug("The Tuya LAN scan failed: %s", err)
        return {}


async def async_follow_tuya_local_ips(
    hass: HomeAssistant, found: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    """Point disconnected Tuya Local devices at the IP address they now announce.

    A disconnected device that announces itself at its saved address is usually
    refusing Home Assistant's local key (the device was re-paired or reset), so
    its key is refreshed from the Tuya cloud integration when the cloud has a
    different one. Pass the result of a LAN scan as found to reuse it.
    Returns the names of the devices that were updated.
    """
    entries = [
        entry
        for entry in hass.config_entries.async_entries(TUYA_LOCAL)
        if not _tuya_local_connected(hass, entry)
    ]
    if not entries:
        return []
    if found is None:
        try:
            found = await hass.async_add_executor_job(_scan_lan)
        except HomeAssistantError as err:
            LOGGER.debug("Skipping the Tuya Local IP check: %s", err)
            return []
    moved = []
    refused = []
    for entry in entries:
        config = {**entry.data, **entry.options}
        ip = (found.get(config.get("device_id")) or {}).get("ip")
        if ip and ip != config.get("host"):
            LOGGER.info(
                "%s moved from %s to %s; updating Tuya Local",
                entry.title,
                config.get("host"),
                ip,
            )
            _update_tuya_local_entry(hass, entry, {"host": ip})
            moved.append(entry.title)
        elif ip:
            refused.append(entry)
    if refused:
        keys = await _async_cloud_local_keys(hass)
        for entry in refused:
            if key := _new_local_key(entry, keys):
                LOGGER.info(
                    "%s has a new local key in the Tuya cloud; updating Tuya Local",
                    entry.title,
                )
                _update_tuya_local_entry(hass, entry, {"local_key": key})
                moved.append(entry.title)
    return moved


class CheckTuyaLocalDevicesTool(AdminTool):
    """Compare each Tuya Local device's saved address with the one it announces."""

    name = "check_tuya_local_devices"
    description = (
        "Check every Tuya Local device: the IP address (host) and protocol version "
        "saved in Tuya Local, whether it is connected, and the IP address and "
        "version the device is announcing on the network right now (takes about "
        "20 seconds). A device whose announced_ip differs from host needs "
        "update_tuya_local_device. A device that announces nothing is switched "
        "off, offline or on another network. local_key_matches_cloud is false "
        "when the Tuya cloud integration has a newer local key for the device "
        "(it refuses connections; fix it with update_tuya_local_device and "
        "refresh_local_key), and null when the cloud can't tell."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        entries = hass.config_entries.async_entries(TUYA_LOCAL)
        if not entries:
            raise HomeAssistantError("Tuya Local has no devices set up")
        found = await hass.async_add_executor_job(_scan_lan)
        keys = await _async_cloud_local_keys(hass)
        devices = []
        for entry in entries:
            config = {**entry.data, **entry.options}
            announced = found.get(config.get("device_id")) or {}
            devices.append(
                {
                    "entry_id": entry.entry_id,
                    "name": entry.title,
                    "device_id": config.get("device_id"),
                    "sub_device": bool(config.get("device_cid")),
                    "host": config.get("host"),
                    "protocol_version": config.get("protocol_version"),
                    "connected": _tuya_local_connected(hass, entry),
                    "announced_ip": announced.get("ip"),
                    "announced_version": announced.get("version"),
                    "local_key_matches_cloud": (
                        None
                        if keys is None
                        or config.get("device_cid")
                        or config.get("device_id") not in keys
                        else _new_local_key(entry, keys) is None
                    ),
                }
            )
        devices.sort(key=lambda d: (d["connected"], d["name"] or ""))
        return {"count": len(devices), "devices": devices}


class UpdateTuyaLocalDeviceTool(AdminTool):
    """Change the IP address or protocol version Tuya Local uses for a device."""

    name = "update_tuya_local_device"
    description = (
        "Change the IP address (host) and/or protocol version Tuya Local uses for "
        "one device, by the entry_id from check_tuya_local_devices, or set "
        "refresh_local_key to copy the device's current local key from the Tuya "
        "cloud integration. The device reconnects straight away. Only call after "
        "the user has confirmed the change."
    )
    parameters = vol.Schema(
        {
            vol.Required("entry_id"): cv.string,
            vol.Optional("host"): cv.string,
            vol.Optional("protocol_version"): vol.In(TUYA_PROTOCOLS),
            vol.Optional("refresh_local_key"): cv.boolean,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        entry = hass.config_entries.async_get_entry(args["entry_id"])
        if entry is None or entry.domain != TUYA_LOCAL:
            raise HomeAssistantError(
                "No Tuya Local device with that entry_id. Use check_tuya_local_devices."
            )
        changes: dict[str, Any] = {}
        if host := args.get("host"):
            changes["host"] = host
        if version := args.get("protocol_version"):
            changes["protocol_version"] = (
                version if version == "auto" else float(version)
            )
        result: dict[str, Any] = {}
        if args.get("refresh_local_key"):
            config = {**entry.data, **entry.options}
            if config.get("device_cid"):
                raise HomeAssistantError(
                    "This is a sub-device; refresh the key of its gateway instead"
                )
            keys = await _async_cloud_local_keys(hass)
            if keys is None:
                raise HomeAssistantError(
                    "The Tuya cloud integration is not set up or not loaded"
                )
            if config.get("device_id") not in keys:
                raise HomeAssistantError(
                    "The Tuya cloud has no local key for this device"
                )
            if key := _new_local_key(entry, keys):
                changes["local_key"] = key
                result["local_key_updated"] = True
            else:
                result["local_key_updated"] = False
        elif not changes:
            raise HomeAssistantError(
                "Give a host, a protocol_version or refresh_local_key"
            )
        if changes:
            _update_tuya_local_entry(hass, entry, changes)
        # Never echo the key itself.
        changes.pop("local_key", None)
        return {"success": True, "name": entry.title, **changes, **result}


def _watchdog(hass: HomeAssistant) -> Any:
    from .watchdog import async_get_watchdog

    watchdog = async_get_watchdog(hass)
    if watchdog is None:
        raise HomeAssistantError("Alfred's watchdog is not running")
    return watchdog


class GetWatchdogStatusTool(AdminTool):
    """Show what Alfred's watchdog is repairing and has learned."""

    name = "get_watchdog_status"
    description = (
        "Alfred's watchdog checks every 5 minutes for entities that have been "
        "unavailable for 10 minutes, and reloads the integration entry that "
        "provides them, pausing longer after each try (Tuya Local devices also "
        "get their IP address and local key followed, and are left alone while "
        "they have no power). This returns the entries it is working on "
        "(entities, attempts, next attempt, why it skipped), its recent actions "
        "and outcomes, how often reloading has fixed each integration, and what "
        "it ignores. Use it when asked why something is unavailable or what "
        "Alfred has repaired."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        return _watchdog(hass).status()


class SetWatchdogIgnoreTool(AdminTool):
    """Make the watchdog ignore an entity or integration entry, or stop ignoring it."""

    name = "set_watchdog_ignore"
    description = (
        "Make Alfred's watchdog ignore an entity (entity_id) or a whole "
        "integration entry (entry_id from get_watchdog_status) that is expected "
        "to be unavailable, or set ignore to false to watch it again. Only call "
        "after the user has confirmed."
    )
    parameters = vol.Schema(
        {
            vol.Required("target"): cv.string,
            vol.Optional("ignore", default=True): cv.boolean,
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        target = args["target"]
        if (
            er.async_get(hass).async_get(target) is None
            and hass.config_entries.async_get_entry(target) is None
        ):
            raise HomeAssistantError(
                "No entity or integration entry with that id. Use find_entities "
                "or get_watchdog_status."
            )
        _watchdog(hass).set_ignored(target, args["ignore"])
        return {"success": True, "target": target, "ignored": args["ignore"]}


class SetWatchdogFeedTool(AdminTool):
    """Tell the watchdog which switch powers which bulbs."""

    name = "set_watchdog_feed"
    description = (
        "Tell Alfred's watchdog that a wall switch (switch entity_id) cuts the "
        "power to these bulbs (their light entity_ids), so they are expected to "
        "be unavailable while the switch is off and are not repaired then. The "
        "watchdog also learns this by itself; get_watchdog_status shows it as "
        "powered_by. Only call after the user has confirmed."
    )
    parameters = vol.Schema(
        {
            vol.Required("switch"): cv.entity_id,
            vol.Required("entities"): vol.All(cv.ensure_list, [cv.entity_id]),
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        registry = er.async_get(hass)
        unknown = [
            entity_id
            for entity_id in [args["switch"], *args["entities"]]
            if registry.async_get(entity_id) is None
            and hass.states.get(entity_id) is None
        ]
        if unknown:
            raise HomeAssistantError(f"Unknown entities: {', '.join(unknown)}")
        _watchdog(hass).teach_feed(args["switch"], args["entities"])
        return {"success": True, "switch": args["switch"], "entities": args["entities"]}


def _cloud_managers(hass: HomeAssistant) -> list[Any]:
    """Device managers of the loaded Tuya cloud (Smart Life) integrations."""
    return [
        manager
        for entry in hass.config_entries.async_loaded_entries(TUYA_CLOUD)
        if (manager := getattr(getattr(entry, "runtime_data", None), "manager", None))
        is not None
    ]


def _cloud_device_info(device: Any, details: bool) -> dict[str, Any]:
    """What Alfred shows about one Tuya cloud device."""
    info: dict[str, Any] = {
        "device_id": device.id,
        "name": device.name,
        "product": getattr(device, "product_name", None),
        "category": getattr(device, "category", None),
        "online": getattr(device, "online", None),
    }
    if details:
        info["status"] = dict(getattr(device, "status", None) or {})
        info["settable"] = {
            code: {
                "type": getattr(function, "type", None),
                "values": getattr(function, "values", None),
            }
            for code, function in (getattr(device, "function", None) or {}).items()
        }
    return info


class TuyaCloudDeviceTool(AdminTool):
    """Read or change any setting of a Smart Life device through the Tuya cloud."""

    name = "tuya_cloud_device"
    description = (
        "Read or change the settings of devices in the user's Smart Life (Tuya) "
        "account, as the Smart Life app does, through Home Assistant's Tuya "
        "integration. This reaches settings that have no entity in Home Assistant, "
        "such as a wall switch's relay status (power-on behaviour, e.g. "
        "relay_status=memory), indicator light mode or a bulb's power-on state. "
        "Give search to find devices by name or id. Give device_id alone to see its "
        "current status and the settings it accepts (code, type, allowed values). "
        "Give device_id and set (code to value) to change settings; only codes "
        "listed under settable work. Only change settings the user asked for."
    )
    parameters = vol.Schema(
        {
            vol.Optional("search"): cv.string,
            vol.Optional("device_id"): cv.string,
            vol.Optional("set"): vol.Schema({cv.string: object}),
        }
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        managers = _cloud_managers(hass)
        if not managers:
            raise HomeAssistantError(
                "The Tuya cloud integration is not set up and loaded in Home Assistant."
            )
        if device_id := args.get("device_id"):
            for manager in managers:
                device = (getattr(manager, "device_map", None) or {}).get(device_id)
                if device is not None:
                    break
            else:
                raise HomeAssistantError(
                    "No Tuya cloud device with that device_id. Use search."
                )
            if not (changes := args.get("set")):
                return _cloud_device_info(device, details=True)
            settable = getattr(device, "function", None) or {}
            if unknown := sorted(set(changes) - set(settable)):
                raise HomeAssistantError(
                    f"This device does not accept {', '.join(unknown)}. "
                    f"Settable codes: {', '.join(sorted(settable))}."
                )
            commands = [
                {"code": code, "value": value} for code, value in changes.items()
            ]
            try:
                await hass.async_add_executor_job(
                    manager.send_commands, device_id, commands
                )
            except Exception as err:  # noqa: BLE001 - the cloud SDK raises anything
                raise HomeAssistantError(f"The Tuya cloud refused: {err}") from err
            return {"success": True, "device_id": device_id, "sent": changes}
        search = (args.get("search") or "").casefold()
        found = [
            _cloud_device_info(device, details=False)
            for manager in managers
            for device in (getattr(manager, "device_map", None) or {}).values()
            if search in f"{device.name} {device.id}".casefold()
        ]
        return {"devices": found[:MAX_ENTITIES], "truncated": len(found) > MAX_ENTITIES}


class ReportIssueTool(AdminTool):
    """File a GitHub issue about Alfred, with its recent log entries attached."""

    name = "report_issue"
    description = (
        "Report a bug in Alfred as a public GitHub issue so its developer can fix "
        "it. Alfred's recent log entries are attached automatically. Only call "
        "after the user has confirmed the title and description."
    )
    parameters = vol.Schema(
        {
            vol.Required("title"): cv.string,
            vol.Required("description"): cv.string,
        }
    )

    def __init__(self, token: str, repo: str) -> None:
        """Initialize the tool."""
        self._token = token
        self._repo = repo

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        logs = "\n\n".join(
            f"{e['level']} {e['logger']} ({e['source']}, x{e['count']})\n"
            f"{e['message']}\n{e['exception']}".rstrip()
            for e in _log_entries(hass, 5, only_alfred=True)
        )
        body = (
            f"{args['description']}\n\n"
            f"_Reported by Alfred from Home Assistant {HA_VERSION}._\n\n"
            f"### Recent Alfred log entries\n\n```\n{logs or 'None'}\n```"
        )
        session = aiohttp_client.async_get_clientsession(hass)
        async with session.post(
            f"{GITHUB_API}/repos/{self._repo}/issues",
            json={"title": args["title"], "body": body},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 201:
                LOGGER.warning("GitHub rejected the issue: %s %s", resp.status, data)
                raise HomeAssistantError(
                    f"GitHub rejected the issue ({resp.status}). Check the GitHub "
                    "token in Alfred's settings."
                )
        return {"success": True, "url": data["html_url"]}


class AlfredAdminAPI(llm.API):
    """Admin tools, offered only to Home Assistant administrators."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        """Initialize the API."""
        super().__init__(hass=hass, id=ADMIN_API_ID, name=ADMIN_API_NAME)
        self.options = options

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the tools, or none for non-admins."""
        try:
            await _async_require_admin(self.hass, llm_context)
        except HomeAssistantError:
            return llm.APIInstance(self, NOT_ADMIN_PROMPT, llm_context, tools=[])

        tools: list[llm.Tool] = [
            GetErrorLogTool(),
            ListAutomationsAndScriptsTool(),
            GetAutomationTool(),
            SaveAutomationTool(),
            GetScriptTool(),
            SaveScriptTool(),
            DeleteAutomationTool(),
            DeleteScriptTool(),
            GetSceneTool(),
            SaveSceneTool(),
            DeleteSceneTool(),
            CallActionTool(),
            ListDashboardsTool(),
            GetDashboardTool(),
            SaveDashboardTool(),
            CreateDashboardTool(),
            UpdateDashboardTool(),
            DeleteDashboardTool(),
            FindEntitiesTool(),
            RenameEntityTool(),
            SetEntityEnabledTool(),
            RenameDeviceTool(),
            ListHacsRepositoriesTool(),
            DownloadHacsRepositoryTool(),
            AddHacsCustomRepositoryTool(),
            ListBluetoothDevicesTool(),
            ListDashboardResourcesTool(),
            RemoveDashboardResourceTool(),
            CheckTuyaLocalDevicesTool(),
            UpdateTuyaLocalDeviceTool(),
            GetWatchdogStatusTool(),
            SetWatchdogIgnoreTool(),
            SetWatchdogFeedTool(),
            TuyaCloudDeviceTool(),
        ]
        if token := self.options.get(CONF_GITHUB_TOKEN):
            repo = self.options.get(CONF_GITHUB_REPO) or DEFAULT_GITHUB_REPO
            tools.append(ReportIssueTool(token, repo))
        return llm.APIInstance(self, ADMIN_PROMPT, llm_context, tools=tools)


def async_register_admin_api(
    hass: HomeAssistant, options: dict[str, Any]
) -> Callable[[], None]:
    """Register the admin API; return a callback that unregisters it."""
    return llm.async_register_api(hass, AlfredAdminAPI(hass, options))
