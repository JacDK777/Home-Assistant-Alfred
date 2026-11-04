"""Tests for Alfred's extra Huckleberry actions."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from custom_components.alfred.const import DOMAIN


@dataclass
class Medicine:
    """Stand-in for huckleberry_api's FirebaseMedicationTypeDocument."""

    name: str
    id_: str = ""
    lastTake: Any = None


@pytest.fixture
def huckleberry(hass: HomeAssistant) -> tuple[MagicMock, MagicMock, str]:
    """Pretend the Huckleberry integration is loaded with one child, Mika."""
    entry = MockConfigEntry(domain="huckleberry", title="Huckleberry")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("huckleberry", "child-uid")},
        name="Mika",
    )
    api = MagicMock()
    api.list_medicine_types = AsyncMock(
        return_value=[
            Medicine("Calpol", "1", MagicMock(amount=2.5, units="ml")),
            Medicine("Vitamin D drops", "2"),
        ]
    )
    api.create_medicine_type = AsyncMock(
        side_effect=lambda uid, name: Medicine(name, "new")
    )
    api.log_medicine = AsyncMock()
    api.log_temperature = AsyncMock()
    coordinator = MagicMock(async_request_refresh=AsyncMock())
    hass.data["huckleberry"] = {
        entry.entry_id: {"api": api, "coordinator": coordinator, "children": []}
    }
    return api, coordinator, device.id


async def _call(hass: HomeAssistant, service: str, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def test_log_medication(
    hass: HomeAssistant, setup_alfred: MockConfigEntry, huckleberry
) -> None:
    """A known medicine is matched by a partial name and logged."""
    api, coordinator, device_id = huckleberry

    result = await _call(
        hass,
        "huckleberry_log_medication",
        device_id=device_id,
        medication="vitamin d",
        amount=1,
        units="drops",
    )

    kwargs = api.log_medicine.await_args.kwargs
    assert api.log_medicine.await_args.args == ("child-uid",)
    assert kwargs["medicine_type"].name == "Vitamin D drops"
    assert kwargs["amount"] == 1
    assert kwargs["units"] == "drops"
    assert abs(kwargs["start_time"] - dt_util.now()) < timedelta(seconds=5)
    coordinator.async_request_refresh.assert_awaited()
    assert result["summary"] == "Logged 1 drops of Vitamin D drops for Mika"
    api.create_medicine_type.assert_not_awaited()


async def test_log_medication_defaults(
    hass: HomeAssistant, setup_alfred: MockConfigEntry, huckleberry
) -> None:
    """One child needs no device; amounts default to ml; time can be earlier."""
    api, _, _ = huckleberry

    await _call(
        hass,
        "huckleberry_log_medication",
        medication="CALPOL",
        amount=2.5,
        minutes_ago=30,
    )

    kwargs = api.log_medicine.await_args.kwargs
    assert kwargs["medicine_type"].name == "Calpol"
    assert kwargs["units"] == "ml"
    assert abs(
        kwargs["start_time"] - (dt_util.now() - timedelta(minutes=30))
    ) < timedelta(seconds=5)


async def test_log_unknown_medication(
    hass: HomeAssistant, setup_alfred: MockConfigEntry, huckleberry
) -> None:
    """An unknown medicine is refused with the known list, unless add_new is set."""
    api, _, _ = huckleberry

    with pytest.raises(ServiceValidationError, match="known: Calpol, Vitamin D drops"):
        await _call(hass, "huckleberry_log_medication", medication="Nurofen")
    api.log_medicine.assert_not_awaited()

    result = await _call(
        hass, "huckleberry_log_medication", medication="Nurofen", add_new=True
    )
    api.create_medicine_type.assert_awaited_once_with("child-uid", "Nurofen")
    assert api.log_medicine.await_args.kwargs["amount"] is None
    assert api.log_medicine.await_args.kwargs["units"] is None
    assert result["new_medicine"] is True


async def test_list_medications(
    hass: HomeAssistant, setup_alfred: MockConfigEntry, huckleberry
) -> None:
    """Medicines are listed with their usual dose."""
    result = await _call(hass, "huckleberry_list_medications")
    assert result == {
        "medications": [
            {"name": "Calpol", "usual_amount": 2.5, "usual_units": "ml"},
            {"name": "Vitamin D drops"},
        ]
    }


async def test_log_temperature(
    hass: HomeAssistant, setup_alfred: MockConfigEntry, huckleberry
) -> None:
    """Temperatures are logged and silly readings are refused."""
    api, _, _ = huckleberry

    result = await _call(hass, "huckleberry_log_temperature", temperature=37.8)
    kwargs = api.log_temperature.await_args.kwargs
    assert kwargs["amount"] == 37.8
    assert kwargs["units"] == "C"
    assert kwargs["notes"] is None
    assert result["summary"] == "Logged a temperature of 37.8 °C for Mika"

    with pytest.raises(ServiceValidationError, match="not a body temperature"):
        await _call(hass, "huckleberry_log_temperature", temperature=99)


async def test_huckleberry_not_loaded(
    hass: HomeAssistant, setup_alfred: MockConfigEntry
) -> None:
    """A clear error when the Huckleberry integration is missing."""
    with pytest.raises(HomeAssistantError, match="not set up"):
        await _call(hass, "huckleberry_log_medication", medication="Calpol")


async def test_services_removed_on_unload(
    hass: HomeAssistant, setup_alfred: MockConfigEntry
) -> None:
    """The actions go away with the Alfred entry."""
    assert hass.services.has_service(DOMAIN, "huckleberry_log_medication")
    await hass.config_entries.async_unload(setup_alfred.entry_id)
    assert not hass.services.has_service(DOMAIN, "huckleberry_log_medication")
