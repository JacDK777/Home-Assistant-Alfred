"""Alfred admin: an opt-in LLM API for looking after Home Assistant itself.

Its tools read the error log, edit UI-managed automations, scripts and dashboards,
look up and rename entities and devices, download and update HACS repositories,
manage dashboard resources, check and fix Tuya Local connections and file GitHub
issues.
They are only offered to Home Assistant administrators.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
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
from homeassistant.helpers.event import async_track_time_interval
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
MAX_HACS_RESULTS = 50


def _hacs(hass: HomeAssistant) -> Any:
    if (hacs := hass.data.get(HACS)) is None:
        raise HomeAssistantError("HACS is not installed")
    return hacs


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
    """List HACS repositories with their installed and newest versions."""

    name = "list_hacs_repositories"
    description = (
        "List HACS repositories (dashboard cards, integrations, themes and so on) "
        "with their repository_id, installed version and newest version. Without a "
        "search text it lists the downloaded ones; with one it searches every "
        "repository HACS knows about by name, full name and description."
    )
    parameters = vol.Schema({vol.Optional("search"): cv.string})

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        search = (args.get("search") or "").lower()
        found = [
            _hacs_repository_info(repository)
            for repository in _hacs(hass).repositories.list_all
            if _hacs_matches(repository, search)
        ]
        found.sort(key=lambda r: (not r["downloaded"], r["name"] or ""))
        return {
            "count": len(found),
            "repositories": found[:MAX_HACS_RESULTS],
            "truncated": len(found) > MAX_HACS_RESULTS,
        }


class DownloadHacsRepositoryTool(AdminTool):
    """Download or update a HACS repository."""

    name = "download_hacs_repository"
    description = (
        "Download a HACS repository by its repository_id from "
        "list_hacs_repositories, or update it if it is already downloaded. Gets the "
        "newest version unless a version is given. HACS adds dashboard cards to the "
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
        hacs = _hacs(hass)
        repository = hacs.repositories.get_by_id(args["repository_id"])
        if repository is None:
            raise HomeAssistantError(
                "No HACS repository with that repository_id. "
                "Use list_hacs_repositories."
            )
        was_downloaded = repository.data.installed
        previous_version = repository.display_installed_version
        try:
            # Fetch the newest release information first, as HACS's refresh does.
            await repository.update_repository(ignore_issues=True, force=True)
            await repository.async_download_repository(ref=args.get("version"))
        except Exception as err:  # noqa: BLE001 - HACS raises its own exception types
            raise HomeAssistantError(f"HACS could not download it: {err}") from err
        if not was_downloaded:
            await hacs.async_recreate_entities()
        await hacs.data.async_write()
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
TUYA_PROTOCOLS = ["auto", "3.1", "3.2", "3.3", "3.4", "3.5", "3.22"]
TUYA_FOLLOW_INTERVAL = timedelta(minutes=5)


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


async def async_follow_tuya_local_ips(hass: HomeAssistant) -> list[str]:
    """Point disconnected Tuya Local devices at the IP address they now announce.

    Returns the names of the devices that were updated.
    """
    entries = [
        entry
        for entry in hass.config_entries.async_entries(TUYA_LOCAL)
        if not _tuya_local_connected(hass, entry)
    ]
    if not entries:
        return []
    try:
        found = await hass.async_add_executor_job(_scan_lan)
    except HomeAssistantError as err:
        LOGGER.debug("Skipping the Tuya Local IP check: %s", err)
        return []
    moved = []
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
    return moved


def async_start_tuya_local_ip_follow(hass: HomeAssistant) -> Callable[[], None]:
    """Check for moved Tuya Local devices every few minutes; return a stop callback."""
    lock = asyncio.Lock()

    async def _async_check(now: datetime) -> None:
        if lock.locked():
            return
        async with lock:
            await async_follow_tuya_local_ips(hass)

    return async_track_time_interval(
        hass, _async_check, TUYA_FOLLOW_INTERVAL, name="Alfred Tuya Local IP follow"
    )


class CheckTuyaLocalDevicesTool(AdminTool):
    """Compare each Tuya Local device's saved address with the one it announces."""

    name = "check_tuya_local_devices"
    description = (
        "Check every Tuya Local device: the IP address (host) and protocol version "
        "saved in Tuya Local, whether it is connected, and the IP address and "
        "version the device is announcing on the network right now (takes about "
        "20 seconds). A device whose announced_ip differs from host needs "
        "update_tuya_local_device. A device that announces nothing is switched "
        "off, offline or on another network."
    )

    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any]
    ) -> JsonObjectType:
        entries = hass.config_entries.async_entries(TUYA_LOCAL)
        if not entries:
            raise HomeAssistantError("Tuya Local has no devices set up")
        found = await hass.async_add_executor_job(_scan_lan)
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
                }
            )
        devices.sort(key=lambda d: (d["connected"], d["name"] or ""))
        return {"count": len(devices), "devices": devices}


class UpdateTuyaLocalDeviceTool(AdminTool):
    """Change the IP address or protocol version Tuya Local uses for a device."""

    name = "update_tuya_local_device"
    description = (
        "Change the IP address (host) and/or protocol version Tuya Local uses for "
        "one device, by the entry_id from check_tuya_local_devices. The device "
        "reconnects straight away. Only call after the user has confirmed the "
        "change."
    )
    parameters = vol.Schema(
        {
            vol.Required("entry_id"): cv.string,
            vol.Optional("host"): cv.string,
            vol.Optional("protocol_version"): vol.In(TUYA_PROTOCOLS),
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
        if not changes:
            raise HomeAssistantError("Give a host, a protocol_version or both")
        _update_tuya_local_entry(hass, entry, changes)
        return {"success": True, "name": entry.title, **changes}


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
            RenameEntityTool(),
            RenameDeviceTool(),
            ListHacsRepositoriesTool(),
            DownloadHacsRepositoryTool(),
            ListDashboardResourcesTool(),
            RemoveDashboardResourceTool(),
            CheckTuyaLocalDevicesTool(),
            UpdateTuyaLocalDeviceTool(),
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
