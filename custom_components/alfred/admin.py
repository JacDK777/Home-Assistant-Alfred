"""Alfred admin: an opt-in LLM API for looking after Home Assistant itself.

Its tools read the error log, edit UI-managed automations, scripts and dashboards,
look up entities and file GitHub issues. They are only offered to Home Assistant
administrators.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import uuid

import voluptuous as vol

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
- Read an automation, script or dashboard before editing it, then save the complete
  updated configuration.
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
    """List automations and scripts with the ids the other tools need."""

    name = "list_automations_and_scripts"
    description = (
        "List all automations and scripts with their ids, names and states. "
        "Use the ids with get_automation, save_automation, get_script and save_script."
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


async def _async_read_item(hass: HomeAssistant, view: Any, key: str) -> dict[str, Any]:
    """Read one automation or script from its UI-managed YAML file."""
    current = await view.read_config(hass)
    if (value := view._get_value(hass, current, key)) is None:
        raise HomeAssistantError(
            f"{key} was not found in {view.path}. Only automations and scripts "
            "created in the UI can be read or edited."
        )
    return value


async def _async_write_item(
    hass: HomeAssistant, view: Any, key: str, config: dict[str, Any]
) -> None:
    """Validate and save one automation or script the way the UI editor does."""
    from homeassistant.components.config.view import _write

    await view.data_validator(hass, key, config)
    async with view.mutation_lock:
        current = await view.read_config(hass)
        view._write_value(hass, current, key, config)
        await hass.async_add_executor_job(_write, hass.config.path(view.path), current)


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
        "List all dashboards with their url_path, title and mode. Use the url_path "
        "with get_dashboard and save_dashboard."
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

        dashboard = _dashboard(hass, args["url_path"])
        if args["previous"]:
            backup = await _backup_store(hass, args["url_path"]).async_load()
            if backup is None:
                raise HomeAssistantError("Alfred has not changed this dashboard yet")
            return {"config": backup}
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
            ListDashboardsTool(),
            GetDashboardTool(),
            SaveDashboardTool(),
            FindEntitiesTool(),
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
