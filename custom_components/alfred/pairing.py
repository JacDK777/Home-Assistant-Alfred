"""Re-add Tuya devices that fell back into pairing mode (EZ mode).

A Tuya device in pairing mode (fast flashing) has forgotten the Wi-Fi and
listens for its details. The Smart Life app sends them from a phone; Alfred
sends the same packets from Home Assistant over the LAN. The Tuya IoT
Platform hands out the one-time pairing token that links the device to the
user's Smart Life home again.

The EZ ("smartconfig") encoding is ported from tuya-convert
(https://github.com/ct-Open-Source/tuya-convert, MIT licence, by kueblc),
whose broadcast strategy came from https://github.com/tuyapi/link.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import socket
import time
from typing import Any
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_TUYA_ACCESS_ID,
    CONF_TUYA_ACCESS_SECRET,
    CONF_TUYA_DATA_CENTER,
    CONF_WIFI_PASSWORD,
    CONF_WIFI_SSID,
    DOMAIN,
    LOGGER,
)

TUYA_ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "weu": "https://openapi-weaz.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "eus": "https://openapi-ueaz.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
DEFAULT_DATA_CENTER = "eu"
PAIRING_SECONDS = 120
MAX_PAIRING_SECONDS = 300
PACKET_GAP = 0.005
DATA_PAIRING = f"{DOMAIN}_pairing"
_monotonic = time.monotonic


# --- EZ mode encoding ------------------------------------------------------


def _crc8(data: list[int]) -> int:
    crc = 0
    for value in data:
        byte = crc ^ value
        crc = 0
        for _ in range(8):
            if (crc ^ byte) & 1:
                crc ^= 0x18
                crc >>= 1
                crc |= 0x80
            else:
                crc >>= 1
            byte >>= 1
    return crc


def _crc32(data: list[int]) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


BROADCAST_HEAD = [1, 3, 6, 10]


def encode_broadcast_body(password: str, ssid: str, token_group: str) -> list[int]:
    """Packet lengths that carry the Wi-Fi details in broadcast frames."""
    data = [len(password), *password.encode()]
    data += [len(token_group), *token_group.encode(), *ssid.encode()]
    length = len(data)
    length_crc = _crc8([length])
    out = [
        length >> 4 | 16,
        length & 0xF | 32,
        length_crc >> 4 | 48,
        length_crc & 0xF | 64,
    ]
    sequence = 0
    index = 0
    for index in range(0, length, 4):
        chunk = data[index : index + 4]
        group = [sequence, *chunk]
        group += [0] * (5 - len(group))
        out.append(_crc8(group) & 0x7F | 128)
        out.append(sequence | 128)
        out.extend(byte | 256 for byte in chunk)
        sequence += 1
    out.extend([256] * (length - index))
    return out


def _bytes_to_ips(data: list[int], sequence: int) -> list[str]:
    data = list(data)
    if len(data) & 1:
        data.append(0)
    ips = []
    for index in range(0, len(data), 2):
        ips.append(f"226.{sequence}.{data[index + 1]}.{data[index]}")
        sequence += 1
    return ips


def _encode_plain(text: bytes) -> list[int]:
    crc = _crc32(list(text))
    return [len(text), len(text), *(crc >> s & 255 for s in (0, 8, 16, 24)), *text]


def _encode_password(password: bytes) -> list[int]:
    crc = _crc32(list(password))
    padded = password + b"\0" * (-len(password) % 16)
    encryptor = Cipher(algorithms.AES(b"a3c6794oiu876t54"), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return [
        len(password),
        len(password),
        *(crc >> s & 255 for s in (0, 8, 16, 24)),
        *encrypted,
    ]


MULTICAST_HEAD = _bytes_to_ips(list(b"TYST01"), 120)


def encode_multicast_body(password: str, ssid: str, token_group: str) -> list[str]:
    """Multicast addresses that carry the Wi-Fi details."""
    return [
        *_bytes_to_ips(_encode_plain(ssid.encode()), 64),
        *_bytes_to_ips(_encode_password(password.encode()), 0),
        *_bytes_to_ips(_encode_plain(token_group.encode()), 32),
    ]


def send_ez_round(password: str, ssid: str, token_group: str) -> None:
    """Send one round of EZ mode packets (blocking, about 10 seconds)."""
    broadcast_body = encode_broadcast_body(password, ssid, token_group)
    multicast_body = encode_multicast_body(password, ssid, token_group)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

        def broadcast(lengths: list[int]) -> None:
            for length in lengths:
                sock.sendto(b"\0" * length, ("255.255.255.255", 30011))
                time.sleep(PACKET_GAP)

        def multicast(ips: list[str]) -> None:
            for ip in ips:
                sock.sendto(b"\0", (ip, 30012))
                time.sleep(PACKET_GAP)

        for _ in range(40):
            multicast(MULTICAST_HEAD)
            broadcast(BROADCAST_HEAD)
        for _ in range(10):
            multicast(MULTICAST_HEAD)
            multicast(multicast_body)
            broadcast(broadcast_body)


# --- Tuya IoT Platform (OpenAPI) -------------------------------------------


class TuyaOpenAPI:
    """The few Tuya OpenAPI calls pairing needs."""

    def __init__(
        self, hass: HomeAssistant, access_id: str, secret: str, data_center: str
    ) -> None:
        self._time_zone = hass.config.time_zone
        self._session = async_get_clientsession(hass)
        self._access_id = access_id
        self._secret = secret
        self._endpoint = TUYA_ENDPOINTS.get(data_center, TUYA_ENDPOINTS["eu"])
        self._token: str | None = None

    def _sign(self, method: str, path: str, body: str, token: str) -> dict[str, str]:
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        content = hashlib.sha256(body.encode()).hexdigest()
        string = f"{method}\n{content}\n\n{path}"
        message = f"{self._access_id}{token}{t}{nonce}{string}"
        sign = hmac.new(self._secret.encode(), message.encode(), hashlib.sha256)
        headers = {
            "client_id": self._access_id,
            "sign": sign.hexdigest().upper(),
            "t": t,
            "nonce": nonce,
            "sign_method": "HMAC-SHA256",
        }
        if token:
            headers["access_token"] = token
        return headers

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        if self._token is None and not path.startswith("/v1.0/token"):
            result = await self._request("GET", "/v1.0/token?grant_type=1")
            self._token = result["access_token"]
        payload = json.dumps(body) if body is not None else ""
        headers = self._sign(method, path, payload, self._token or "")
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with self._session.request(
            method, self._endpoint + path, data=payload or None, headers=headers
        ) as response:
            data = await response.json(content_type=None)
        if not data.get("success"):
            raise HomeAssistantError(
                f"Tuya IoT Platform error {data.get('code')}: {data.get('msg')}. "
                "Check the Access ID, Access Secret and data center in Alfred's "
                "settings, that the Smart Life app is linked to the cloud project, "
                "and that its free trial has not run out."
            )
        return data.get("result")

    async def async_pairing_token(self, device_id: str | None) -> dict[str, Any]:
        """A one-time EZ pairing token for the linked Smart Life home."""
        uid = None
        if device_id:
            uid = (await self._request("GET", f"/v1.0/devices/{device_id}")).get("uid")
        if not uid:
            users = await self._request(
                "GET", "/v1.0/iot-01/associated-users/actions/batch-pull?last_row_key="
            )
            linked = (users or {}).get("list") or []
            if not linked:
                raise HomeAssistantError(
                    "No Smart Life account is linked to the Tuya cloud project. "
                    "Link it under Devices > Link App Account."
                )
            uid = linked[0]["uid"]
        homes = await self._request("GET", f"/v1.0/users/{uid}/homes")
        if not homes:
            raise HomeAssistantError("The Smart Life account has no home.")
        return await self._request(
            "POST",
            "/v1.0/device/paring/token",
            {
                "paring_type": "EZ",
                "uid": uid,
                "home_id": homes[0]["home_id"],
                "time_zone_id": self._time_zone,
            },
        )

    async def async_paired(self, token: str) -> list[dict[str, Any]]:
        """Devices that joined with this pairing token so far."""
        result = await self._request("GET", f"/v1.0/device/paring/tokens/{token}")
        return (result or {}).get("success_devices") or []


# --- Pairing run -------------------------------------------------------------


@dataclass
class PairingRun:
    """State of the latest pairing run."""

    running: bool = False
    started: float | None = None
    finished: float | None = None
    paired: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    keys_updated: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started,
            "finished_at": self.finished,
            "paired": [
                {"device_id": d.get("id"), "name": d.get("name"), "ip": d.get("ip")}
                for d in self.paired
            ],
            "tuya_local_keys_updated": self.keys_updated,
            "error": self.error,
        }


def pairing_settings(hass: HomeAssistant) -> dict[str, Any]:
    """Pairing settings from Alfred's options; raises when any are missing."""
    entries = hass.config_entries.async_entries(DOMAIN)
    options = {**entries[0].data, **entries[0].options} if entries else {}
    missing = [
        label
        for key, label in (
            (CONF_TUYA_ACCESS_ID, "Tuya Access ID"),
            (CONF_TUYA_ACCESS_SECRET, "Tuya Access Secret"),
            (CONF_WIFI_SSID, "Wi-Fi name"),
            (CONF_WIFI_PASSWORD, "Wi-Fi password"),
        )
        if not options.get(key)
    ]
    if missing:
        raise HomeAssistantError(
            "Pairing needs these in Alfred's settings (Settings > Devices & "
            f"services > Alfred > Configure): {', '.join(missing)}."
        )
    return options


def pairing_run(hass: HomeAssistant) -> PairingRun:
    """The latest pairing run."""
    return hass.data.setdefault(DATA_PAIRING, PairingRun())


async def async_start_pairing(
    hass: HomeAssistant,
    seconds: int,
    device_id: str | None,
    on_done: Callable[[PairingRun], None] | None = None,
) -> PairingRun:
    """Start sending EZ mode pairing packets in the background."""
    options = pairing_settings(hass)
    run = pairing_run(hass)
    if run.running:
        raise HomeAssistantError("Pairing is already running. Check its status.")
    api = TuyaOpenAPI(
        hass,
        options[CONF_TUYA_ACCESS_ID],
        options[CONF_TUYA_ACCESS_SECRET],
        options.get(CONF_TUYA_DATA_CENTER) or DEFAULT_DATA_CENTER,
    )
    token = await api.async_pairing_token(device_id)
    token_group = f"{token['region']}{token['token']}{token['secret']}"
    run = PairingRun(running=True, started=time.time())
    hass.data[DATA_PAIRING] = run
    hass.async_create_background_task(
        _async_pair(
            hass, api, run, options, token["token"], token_group, seconds, on_done
        ),
        f"{DOMAIN} tuya pairing",
    )
    return run


async def _async_pair(
    hass: HomeAssistant,
    api: TuyaOpenAPI,
    run: PairingRun,
    options: dict[str, Any],
    token: str,
    token_group: str,
    seconds: int,
    on_done: Callable[[PairingRun], None] | None = None,
) -> None:
    # Imported here: admin imports this module for its tools.
    from .admin import async_follow_tuya_local_ips

    try:
        deadline = _monotonic() + seconds
        while _monotonic() < deadline:
            await hass.async_add_executor_job(
                send_ez_round,
                options[CONF_WIFI_PASSWORD],
                options[CONF_WIFI_SSID],
                token_group,
            )
            paired = await api.async_paired(token)
            if len(paired) > len(run.paired):
                run.paired = paired
        run.paired = await api.async_paired(token)
        if run.paired:
            # Re-added devices get new local keys: pass them to Tuya Local.
            run.keys_updated = await async_follow_tuya_local_ips(hass)
    except Exception as err:  # noqa: BLE001 - report anything to the user
        LOGGER.warning("Tuya pairing failed: %s", err)
        run.error = str(err)
    finally:
        run.running = False
        run.finished = time.time()
        if on_done is not None:
            on_done(run)
