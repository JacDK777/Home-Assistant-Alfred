"""Tests for re-adding Tuya devices in pairing mode."""

from typing import Any
from unittest.mock import patch
import zlib

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .test_admin import _api, _call
from custom_components.alfred import pairing
from custom_components.alfred.const import (
    CONF_TUYA_ACCESS_ID,
    CONF_TUYA_ACCESS_SECRET,
    CONF_WIFI_PASSWORD,
    CONF_WIFI_SSID,
)

API = "https://openapi.tuyaeu.com"


def test_crcs() -> None:
    """The checksums are CRC-8/MAXIM and the standard CRC-32."""
    data = list(b"123456789")
    assert pairing._crc8(data) == 0xA1
    assert pairing._crc32(data) == zlib.crc32(b"123456789")


def test_broadcast_body_round_trip() -> None:
    """The broadcast lengths decode back to the Wi-Fi details."""
    body = pairing.encode_broadcast_body("pass word!", "Deco", "EUtoken12abcd")
    length = (body[0] & 0xF) << 4 | body[1] & 0xF
    assert (body[2] & 0xF) << 4 | body[3] & 0xF == pairing._crc8([length])
    data: list[int] = []
    position = 4
    while len(data) < length:
        assert body[position] & 128
        assert body[position + 1] == 128 | len(data) // 4
        chunk = body[position + 2 : position + 6][: length - len(data)]
        assert all(value & 256 for value in chunk)
        data += [value & 0xFF for value in chunk]
        position += 2 + len(chunk)
    raw = bytes(data[:length])
    assert raw == bytes([10]) + b"pass word!" + bytes([13]) + b"EUtoken12abcdDeco"


def test_multicast_body() -> None:
    """Multicast addresses carry length, CRC and data, two bytes each."""
    head = pairing.MULTICAST_HEAD
    assert head[0] == f"226.120.{ord('Y')}.{ord('T')}"
    body = pairing.encode_multicast_body("pw", "Deco", "EUtoken")
    crc = zlib.crc32(b"Deco")
    assert body[0] == "226.64.4.4"
    assert body[1] == f"226.65.{crc >> 8 & 255}.{crc & 255}"
    assert body[3] == f"226.67.{ord('e')}.{ord('D')}"
    # Password: 2 length bytes, 4 CRC bytes and 16 encrypted bytes.
    password = [ip for ip in body if int(ip.split(".")[1]) < 32]
    assert len(password) == 11
    assert password[0] == "226.0.2.2"


async def test_pairing_needs_settings(
    hass: HomeAssistant, setup_alfred, hass_admin_user: MockUser
) -> None:
    """Without the Tuya and Wi-Fi settings the tool says what is missing."""
    api = await _api(hass, hass_admin_user)
    with pytest.raises(HomeAssistantError, match="Tuya Access ID, Tuya Access"):
        await _call(api, "pair_tuya_devices", action="start")
    assert (await _call(api, "pair_tuya_devices", action="status"))["running"] is False


def _ok(result: Any) -> dict[str, Any]:
    return {"success": True, "result": result}


async def test_pairing_run(
    hass: HomeAssistant,
    setup_alfred: MockConfigEntry,
    hass_admin_user: MockUser,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A run gets a token, sends EZ packets and reports re-added devices."""
    hass.config_entries.async_update_entry(
        setup_alfred,
        options={
            **setup_alfred.options,
            CONF_TUYA_ACCESS_ID: "id",
            CONF_TUYA_ACCESS_SECRET: "secret",
            CONF_WIFI_SSID: "Deco",
            CONF_WIFI_PASSWORD: "pw",
        },
    )
    await hass.async_block_till_done()
    aioclient_mock.get(
        f"{API}/v1.0/token?grant_type=1", json=_ok({"access_token": "t"})
    )
    aioclient_mock.get(
        f"{API}/v1.0/iot-01/associated-users/actions/batch-pull?last_row_key=",
        json=_ok({"list": [{"uid": "u1"}]}),
    )
    aioclient_mock.get(f"{API}/v1.0/users/u1/homes", json=_ok([{"home_id": 7}]))
    aioclient_mock.post(
        f"{API}/v1.0/device/paring/token",
        json=_ok({"region": "EU", "token": "tok12345", "secret": "ab12"}),
    )
    aioclient_mock.get(
        f"{API}/v1.0/device/paring/tokens/tok12345",
        json=_ok(
            {"success_devices": [{"id": "b1", "name": "Kitchen DL 1", "ip": "1"}]}
        ),
    )
    api = await _api(hass, hass_admin_user)
    with (
        patch.object(pairing, "send_ez_round") as send,
        patch.object(pairing, "_monotonic", side_effect=[0, 0, 0] + [31] * 9),
        patch(
            "custom_components.alfred.admin.async_follow_tuya_local_ips",
            return_value=["Kitchen DL 1"],
        ),
    ):
        result = await _call(api, "pair_tuya_devices", action="start", seconds=30)
        assert result["started"] is True
        await hass.async_block_till_done(wait_background_tasks=True)

    send.assert_called_with("pw", "Deco", "EUtok12345ab12")
    status = await _call(api, "pair_tuya_devices", action="status")
    assert status["running"] is False
    assert status["error"] is None
    assert status["paired"] == [{"device_id": "b1", "name": "Kitchen DL 1", "ip": "1"}]
    assert status["tuya_local_keys_updated"] == ["Kitchen DL 1"]
    pairing.pairing_run(hass).running = True
    with pytest.raises(HomeAssistantError, match="already running"):
        await _call(api, "pair_tuya_devices", action="start")
    token_call = next(
        c for c in aioclient_mock.mock_calls if "paring/token" in str(c[1])
    )
    assert '"paring_type": "EZ"' in token_call[2]
    assert '"home_id": 7' in token_call[2]


async def test_pairing_cloud_error(
    hass: HomeAssistant,
    setup_alfred: MockConfigEntry,
    hass_admin_user: MockUser,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A Tuya IoT Platform error is passed on with what to check."""
    hass.config_entries.async_update_entry(
        setup_alfred,
        options={
            **setup_alfred.options,
            CONF_TUYA_ACCESS_ID: "id",
            CONF_TUYA_ACCESS_SECRET: "secret",
            CONF_WIFI_SSID: "Deco",
            CONF_WIFI_PASSWORD: "pw",
        },
    )
    await hass.async_block_till_done()
    aioclient_mock.get(
        f"{API}/v1.0/token?grant_type=1",
        json={"success": False, "code": 1004, "msg": "sign invalid"},
    )
    api = await _api(hass, hass_admin_user)
    with pytest.raises(HomeAssistantError, match="error 1004: sign invalid"):
        await _call(api, "pair_tuya_devices", action="start")
