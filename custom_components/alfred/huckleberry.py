"""Extra Huckleberry baby-tracker actions that the Huckleberry integration lacks.

The Huckleberry integration (Woyken/huckleberry-homeassistant) can log sleep,
feeds, nappies, growth, pumping, activities and solids, but not medicine or
temperature. Its API client can, so Alfred adds these actions on top of it:

- ``alfred.huckleberry_log_medication``
- ``alfred.huckleberry_list_medications``
- ``alfred.huckleberry_log_temperature``

They borrow the signed-in client from the Huckleberry integration, so nothing
extra has to be configured. Scripts (for example a "Mika log medication" script
exposed to Assist) call them so that they can be used by voice.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

HUCKLEBERRY_DOMAIN = "huckleberry"

SERVICE_LOG_MEDICATION = "huckleberry_log_medication"
SERVICE_LIST_MEDICATIONS = "huckleberry_list_medications"
SERVICE_LOG_TEMPERATURE = "huckleberry_log_temperature"

MEDICATION_UNITS = ["ml", "oz", "tsp", "drops"]
TEMPERATURE_UNITS = ["C", "F"]
TEMPERATURE_RANGE = {"C": (30.0, 45.0), "F": (86.0, 113.0)}

ATTR_MEDICATION = "medication"
ATTR_AMOUNT = "amount"
ATTR_UNITS = "units"
ATTR_NOTES = "notes"
ATTR_ADD_NEW = "add_new"
ATTR_MINUTES_AGO = "minutes_ago"
ATTR_TEMPERATURE = "temperature"

_CHILD_SCHEMA = {vol.Optional(CONF_DEVICE_ID): cv.string}
_TIME_SCHEMA = {
    vol.Optional(ATTR_MINUTES_AGO, default=0): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=7 * 24 * 60)
    )
}

LOG_MEDICATION_SCHEMA = vol.Schema(
    {
        **_CHILD_SCHEMA,
        **_TIME_SCHEMA,
        vol.Required(ATTR_MEDICATION): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(ATTR_AMOUNT): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(ATTR_UNITS): vol.In(MEDICATION_UNITS),
        vol.Optional(ATTR_NOTES, default=""): cv.string,
        vol.Optional(ATTR_ADD_NEW, default=False): cv.boolean,
    }
)
LIST_MEDICATIONS_SCHEMA = vol.Schema(_CHILD_SCHEMA)
LOG_TEMPERATURE_SCHEMA = vol.Schema(
    {
        **_CHILD_SCHEMA,
        **_TIME_SCHEMA,
        vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
        vol.Optional(ATTR_UNITS, default="C"): vol.In(TEMPERATURE_UNITS),
        vol.Optional(ATTR_NOTES, default=""): cv.string,
    }
)


def _huckleberry_entries(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return the Huckleberry integration's loaded entries that hold an API client."""
    data = hass.data.get(HUCKLEBERRY_DOMAIN)
    if not isinstance(data, dict):
        return {}
    return {
        entry_id: entry_data
        for entry_id, entry_data in data.items()
        if isinstance(entry_data, dict) and entry_data.get("api") is not None
    }


def _child_devices(hass: HomeAssistant) -> list[tuple[dr.DeviceEntry, str]]:
    """Return every Huckleberry child device with its child id."""
    devices = []
    for device in dr.async_get(hass).devices.values():
        for identifier_domain, identifier in device.identifiers:
            if identifier_domain == HUCKLEBERRY_DOMAIN:
                devices.append((device, identifier))
                break
    return devices


def _resolve_child(hass: HomeAssistant, call: ServiceCall) -> tuple[Any, Any, str]:
    """Return the API client, its coordinator and the child id for a call.

    Without a device_id the call is allowed when there is exactly one child.
    """
    entries = _huckleberry_entries(hass)
    if not entries:
        raise HomeAssistantError(
            "The Huckleberry integration is not set up or not loaded"
        )

    children = _child_devices(hass)
    device_id = call.data.get(CONF_DEVICE_ID)
    if device_id:
        matches = [child for child in children if child[0].id == device_id]
        if not matches:
            raise ServiceValidationError(
                f"Device {device_id} is not a Huckleberry child"
            )
    else:
        matches = children
        if len(matches) != 1:
            names = ", ".join(
                device.name_by_user or device.name or "?" for device, _ in children
            )
            raise ServiceValidationError(
                f"Say which child this is for by passing device_id. Children: {names}"
                if children
                else "Huckleberry has no children set up"
            )

    device, child_uid = matches[0]
    entry_data = next(
        (entries[eid] for eid in device.config_entries if eid in entries),
        next(iter(entries.values())),
    )
    return entry_data["api"], entry_data.get("coordinator"), child_uid


def _child_name(hass: HomeAssistant, child_uid: str) -> str:
    for device, uid in _child_devices(hass):
        if uid == child_uid:
            return device.name_by_user or device.name or "the baby"
    return "the baby"


def _start_time(call: ServiceCall):
    return dt_util.now() - timedelta(minutes=call.data.get(ATTR_MINUTES_AGO, 0))


async def _async_refresh(coordinator: Any) -> None:
    """Ask the Huckleberry coordinator to refresh, if it can."""
    refresh = getattr(coordinator, "async_request_refresh", None)
    if refresh is None:
        return
    try:
        await refresh()
    except Exception as err:  # noqa: BLE001 - a refresh failure must not fail the log
        LOGGER.debug("Huckleberry refresh failed: %s", err)


def _match_medicine(medicines: list[Any], wanted: str) -> Any | None:
    """Find a medicine by name: exact, then a unique prefix or partial match."""
    wanted_key = wanted.strip().casefold()
    by_name = [(medicine.name.strip().casefold(), medicine) for medicine in medicines]
    for name, medicine in by_name:
        if name == wanted_key:
            return medicine
    for test in (
        lambda name: name.startswith(wanted_key) or wanted_key.startswith(name),
        lambda name: wanted_key in name or name in wanted_key,
    ):
        found = [medicine for name, medicine in by_name if test(name)]
        if len(found) == 1:
            return found[0]
    return None


def _format_amount(amount: float) -> str:
    return f"{amount:g}"


async def _async_log_medication(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    api, coordinator, child_uid = _resolve_child(hass, call)
    wanted = call.data[ATTR_MEDICATION].strip()
    amount = call.data.get(ATTR_AMOUNT)
    units = call.data.get(ATTR_UNITS)
    if amount is not None and units is None:
        units = "ml"

    medicines = await api.list_medicine_types(child_uid)
    medicine = _match_medicine(medicines, wanted)
    created = False
    if medicine is None:
        if not call.data[ATTR_ADD_NEW]:
            known = ", ".join(m.name for m in medicines) or "none yet"
            raise ServiceValidationError(
                f"{wanted} is not one of the medicines in Huckleberry "
                f"(known: {known}). Ask whether it is one of those, or whether to "
                "add it as a new medicine (add_new: true)."
            )
        medicine = await api.create_medicine_type(child_uid, wanted)
        created = True

    await api.log_medicine(
        child_uid,
        start_time=_start_time(call),
        medicine_type=medicine,
        amount=amount,
        units=units,
        notes=call.data[ATTR_NOTES],
    )
    await _async_refresh(coordinator)

    dose = f"{_format_amount(amount)} {units} of " if amount is not None else ""
    summary = f"Logged {dose}{medicine.name} for {_child_name(hass, child_uid)}"
    if created:
        summary += f" ({medicine.name} was added as a new medicine)"
    return {
        "medication": medicine.name,
        "amount": amount,
        "units": units,
        "new_medicine": created,
        "summary": summary,
    }


async def _async_list_medications(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    api, _, child_uid = _resolve_child(hass, call)
    medicines = await api.list_medicine_types(child_uid)
    result = []
    for medicine in medicines:
        item: dict[str, Any] = {"name": medicine.name}
        last_take = getattr(medicine, "lastTake", None)
        if last_take is not None and getattr(last_take, "amount", None) is not None:
            item["usual_amount"] = last_take.amount
            item["usual_units"] = getattr(last_take, "units", None)
        result.append(item)
    return {"medications": result}


async def _async_log_temperature(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    api, coordinator, child_uid = _resolve_child(hass, call)
    temperature = call.data[ATTR_TEMPERATURE]
    units = call.data[ATTR_UNITS]
    low, high = TEMPERATURE_RANGE[units]
    if not low <= temperature <= high:
        raise ServiceValidationError(
            f"{_format_amount(temperature)} °{units} is not a body temperature; "
            "check the reading and the units"
        )
    await api.log_temperature(
        child_uid,
        start_time=_start_time(call),
        amount=temperature,
        units=units,
        notes=call.data[ATTR_NOTES] or None,
    )
    await _async_refresh(coordinator)
    return {
        "temperature": temperature,
        "units": units,
        "summary": (
            f"Logged a temperature of {_format_amount(temperature)} °{units} "
            f"for {_child_name(hass, child_uid)}"
        ),
    }


def async_register_huckleberry_services(hass: HomeAssistant) -> Callable[[], None]:
    """Register the extra Huckleberry actions; return a function that removes them."""

    async def log_medication(call: ServiceCall) -> ServiceResponse:
        return await _async_log_medication(hass, call)

    async def list_medications(call: ServiceCall) -> ServiceResponse:
        return await _async_list_medications(hass, call)

    async def log_temperature(call: ServiceCall) -> ServiceResponse:
        return await _async_log_temperature(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_MEDICATION,
        log_medication,
        schema=LOG_MEDICATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_MEDICATIONS,
        list_medications,
        schema=LIST_MEDICATIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_TEMPERATURE,
        log_temperature,
        schema=LOG_TEMPERATURE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    def _remove() -> None:
        for service in (
            SERVICE_LOG_MEDICATION,
            SERVICE_LIST_MEDICATIONS,
            SERVICE_LOG_TEMPERATURE,
        ):
            hass.services.async_remove(DOMAIN, service)

    return _remove
