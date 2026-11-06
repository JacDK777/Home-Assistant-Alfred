"""Alfred's watchdog: find integrations whose entities went unavailable and repair them.

Every few minutes the watchdog looks for entities that have been unavailable for
a while, groups them by the integration entry that provides them and tries to
bring that entry back:

- Tuya Local devices first get Alfred's IP and local key follow (admin.py), and
  are only reloaded when the device still announces itself on the network (a
  bulb that announces nothing has no power, so reloading it can't help).
- Bulbs that are switched off at the wall are unavailable too. The watchdog
  learns which switch powers which bulb (a bulb that drops out right after a
  switch turns off, and comes back right after it turns on, twice) and leaves a
  bulb alone while its switch is off. It can also be told.
- Everything else is reloaded, with a growing pause between attempts.

The watchdog remembers what it did. When an entry comes back after a repair the
repair is counted as a fix for that integration; an integration that has had
several repairs and never come back from one is no longer repaired automatically
and only reported. After a few failed attempts Alfred raises a notification in
Home Assistant saying what is down and what it tried.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, storage
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

WATCHDOG_INTERVAL = timedelta(minutes=5)
# How long an entity must have been unavailable before the watchdog acts.
GRACE_PERIOD = timedelta(minutes=10)
# Pause after each attempt on the same entry; the last one repeats.
BACKOFF = (
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)
NOTIFY_AFTER_ATTEMPTS = 3
# An integration that has been repaired this often without ever coming back
# from a repair is only reported from then on.
GIVE_UP_AFTER_TRIES = 6
HISTORY_LENGTH = 50
# A bulb that goes unavailable this soon after a switch turns off (or comes
# back this soon after it turns on) is probably powered by that switch.
FEED_WINDOW = timedelta(minutes=3)
# How often that has to be seen before the watchdog relies on it.
FEED_MIN_SEEN = 2
TAUGHT = 100
# Spread reloads out when a lot goes down at once (a Wi-Fi or power outage).
MAX_RELOADS_PER_CHECK = 5

# Devices from these integrations are unavailable whenever they are switched
# off or away from home (TVs in standby, phones), which is normal.
DEFAULT_IGNORED_DOMAINS = frozenset(
    {
        DOMAIN,
        "androidtv",
        "androidtv_remote",
        "braviatv",
        "cast",
        "dlna_dmr",
        "mobile_app",
        "samsungtv",
        "webostv",
    }
)

TUYA_LOCAL = "tuya_local"
ACTION_RELOAD = "reload"

STORAGE_KEY = f"{DOMAIN}.watchdog"
STORAGE_VERSION = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


class Watchdog:
    """Keeps track of unavailable integrations and repairs them."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the watchdog."""
        self.hass = hass
        self._store: storage.Store[dict[str, Any]] = storage.Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._lock = asyncio.Lock()
        self.ignored: set[str] = set()
        # entry_id -> tracking info for entries that are currently down.
        self.issues: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        # integration domain -> {"tries": int, "fixes": int}
        self.learned: dict[str, dict[str, int]] = {}
        # entity_id -> {switch entity_id: times seen}: which switch powers it.
        self.feeds: dict[str, dict[str, int]] = {}
        # switch entity_id -> (state it changed to, when), for recent changes.
        self._recent: dict[str, tuple[str, datetime]] = {}

    async def async_load(self) -> None:
        """Load what the watchdog remembered from before a restart."""
        data = await self._store.async_load() or {}
        self.ignored = set(data.get("ignored", []))
        self.issues = data.get("issues", {})
        self.history = data.get("history", [])
        self.learned = data.get("learned", {})
        self.feeds = data.get("feeds", {})

    def _data(self) -> dict[str, Any]:
        return {
            "ignored": sorted(self.ignored),
            "issues": self.issues,
            "history": self.history[-HISTORY_LENGTH:],
            "learned": self.learned,
            "feeds": self.feeds,
        }

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 5)

    def _log(self, entry: Any, action: str, outcome: str) -> None:
        self.history.append(
            {
                "time": _iso(dt_util.utcnow()),
                "entry_id": entry.entry_id,
                "name": entry.title,
                "integration": entry.domain,
                "action": action,
                "outcome": outcome,
            }
        )
        del self.history[:-HISTORY_LENGTH]

    def _learned(self, domain: str) -> dict[str, int]:
        return self.learned.setdefault(domain, {"tries": 0, "fixes": 0})

    def repairs_help(self, domain: str) -> bool:
        """Whether reloading has a chance of fixing this integration."""
        stats = self.learned.get(domain)
        return not (
            stats and stats["fixes"] == 0 and stats["tries"] >= GIVE_UP_AFTER_TRIES
        )

    def set_ignored(self, target: str, ignored: bool) -> None:
        """Ignore an entity id or an integration entry id, or stop ignoring it."""
        if ignored:
            self.ignored.add(target)
            self.issues.pop(target, None)
            persistent_notification.async_dismiss(self.hass, _notification_id(target))
        else:
            self.ignored.discard(target)
        self._save()

    @callback
    def async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Learn which switch powers which bulb from the order things happen in."""
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or old.state == new.state:
            return
        now = new.last_changed
        if (
            entity_id.split(".", 1)[0] in ("switch", "light")
            and old.state in (STATE_ON, STATE_OFF)
            and new.state in (STATE_ON, STATE_OFF)
        ):
            self._recent[entity_id] = (new.state, now)
            return
        if new.state == STATE_UNAVAILABLE:
            expected = STATE_OFF
        elif old.state == STATE_UNAVAILABLE:
            expected = STATE_ON
        else:
            return
        seen = False
        for switch, (state, when) in list(self._recent.items()):
            if now - when > FEED_WINDOW:
                del self._recent[switch]
            elif state == expected and switch != entity_id:
                counts = self.feeds.setdefault(entity_id, {})
                counts[switch] = counts.get(switch, 0) + 1
                seen = True
        if seen:
            self._save()

    def feed_of(self, entity_id: str) -> str | None:
        """The switch the watchdog has learned powers this entity."""
        counts = self.feeds.get(entity_id)
        if not counts:
            return None
        switch, seen = max(counts.items(), key=lambda item: item[1])
        return switch if seen >= FEED_MIN_SEEN else None

    def teach_feed(self, switch: str, entity_ids: list[str]) -> None:
        """Record that a switch powers these entities."""
        for entity_id in entity_ids:
            self.feeds[entity_id] = {switch: TAUGHT}
        self._save()

    def _powered_off(self, entity: er.RegistryEntry, device_feeds: dict) -> bool:
        switch = self.feed_of(entity.entity_id) or device_feeds.get(entity.device_id)
        if switch is None:
            return False
        state = self.hass.states.get(switch)
        return state is not None and state.state == STATE_OFF

    def unavailable_by_entry(self) -> dict[str, list[str]]:
        """Entities unavailable for longer than the grace period, by config entry."""
        now = dt_util.utcnow()
        entities = er.async_get(self.hass)
        found: dict[str, list[str]] = {}
        # A bulb's other entities (scene, timer...) share its power supply.
        device_feeds: dict[str, str] = {}
        for entity_id in self.feeds:
            entity = entities.async_get(entity_id)
            if entity and entity.device_id and (switch := self.feed_of(entity_id)):
                device_feeds[entity.device_id] = switch
        for entity in entities.entities.values():
            if (
                entity.disabled_by is not None
                or entity.config_entry_id is None
                or entity.platform in DEFAULT_IGNORED_DOMAINS
                or entity.entity_id in self.ignored
                or entity.config_entry_id in self.ignored
            ):
                continue
            state = self.hass.states.get(entity.entity_id)
            if (
                state is None
                or state.state != STATE_UNAVAILABLE
                or now - state.last_changed < GRACE_PERIOD
                or self._powered_off(entity, device_feeds)
            ):
                continue
            found.setdefault(entity.config_entry_id, []).append(entity.entity_id)
        return found

    async def async_check(self) -> None:
        """Run one round: note recoveries, then repair what is still down."""
        if self._lock.locked():
            return
        async with self._lock:
            await self._async_check()

    async def _async_check(self) -> None:
        # Let the Tuya Local IP and key follow run first: it fixes the most
        # common causes and its scan tells which devices have power.
        from .admin import (
            _tuya_local_connected,
            async_follow_tuya_local_ips,
            async_scan_tuya_local,
        )

        found: dict[str, Any] = {}
        if any(
            not _tuya_local_connected(self.hass, entry)
            for entry in self.hass.config_entries.async_entries(TUYA_LOCAL)
        ):
            found = await async_scan_tuya_local(self.hass)
            await async_follow_tuya_local_ips(self.hass, found)
        down = self.unavailable_by_entry()
        now = dt_util.utcnow()
        changed = False

        for entry_id in list(self.issues):
            if entry_id in down:
                continue
            issue = self.issues.pop(entry_id)
            changed = True
            persistent_notification.async_dismiss(self.hass, _notification_id(entry_id))
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                continue
            if issue.get("last_action"):
                self._learned(entry.domain)["fixes"] += 1
                self._log(entry, issue["last_action"], "fixed")
                LOGGER.info("Watchdog: %s is back after a reload", entry.title)
            else:
                self._log(entry, "none", "recovered on its own")

        reloads = 0
        for entry_id, entity_ids in down.items():
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if (
                entry is None
                or entry.disabled_by is not None
                or entry.domain in DEFAULT_IGNORED_DOMAINS
            ):
                continue
            issue = self.issues.get(entry_id)
            if issue is None:
                issue = self.issues[entry_id] = {
                    "name": entry.title,
                    "integration": entry.domain,
                    "down_since": _iso(now),
                    "attempts": 0,
                    "next_attempt": _iso(now),
                    "last_action": None,
                    "notified": False,
                }
                changed = True
            issue["entities"] = sorted(entity_ids)

            next_attempt = _parse(issue.get("next_attempt"))
            if next_attempt and now < next_attempt:
                continue
            if not self.repairs_help(entry.domain):
                issue["skipped"] = "reloading never fixed this integration"
                self._notify(entry, issue)
                changed = True
                continue
            if entry.domain == TUYA_LOCAL:
                config = {**entry.data, **entry.options}
                if config.get("device_id") not in found:
                    # No power (wall switch off) or off the network: a reload
                    # can't help, so wait without counting an attempt.
                    issue["skipped"] = "the device is not on the network"
                    changed = True
                    continue
            if reloads >= MAX_RELOADS_PER_CHECK:
                continue
            issue.pop("skipped", None)
            reloads += 1
            await self._async_reload(entry, issue, now)
            changed = True
            if issue["attempts"] >= NOTIFY_AFTER_ATTEMPTS:
                self._notify(entry, issue)

        if changed:
            self._save()

    async def _async_reload(
        self, entry: Any, issue: dict[str, Any], now: datetime
    ) -> None:
        LOGGER.info(
            "Watchdog: reloading %s (%s), unavailable: %s",
            entry.title,
            entry.domain,
            ", ".join(issue["entities"]),
        )
        try:
            await self.hass.config_entries.async_reload(entry.entry_id)
        except (HomeAssistantError, ValueError) as err:
            LOGGER.debug("Watchdog could not reload %s: %s", entry.title, err)
            outcome = f"reload failed: {err}"
        else:
            outcome = (
                "reloaded"
                if entry.state is ConfigEntryState.LOADED
                else f"reloaded, now {entry.state.value}"
            )
        issue["attempts"] += 1
        issue["last_action"] = ACTION_RELOAD
        issue["last_attempt"] = _iso(now)
        pause = BACKOFF[min(issue["attempts"], len(BACKOFF)) - 1]
        issue["next_attempt"] = _iso(now + pause)
        self._learned(entry.domain)["tries"] += 1
        self._log(entry, ACTION_RELOAD, outcome)

    def _notify(self, entry: Any, issue: dict[str, Any]) -> None:
        if issue.get("notified"):
            return
        issue["notified"] = True
        tried = (
            f"Alfred reloaded it {issue['attempts']} times without success"
            if issue["attempts"]
            else "Alfred stopped reloading it because that has never helped"
        )
        persistent_notification.async_create(
            self.hass,
            (
                f"{entry.title} ({entry.domain}) has been unavailable since "
                f"{issue['down_since']}. {tried}. Unavailable: "
                f"{', '.join(issue['entities'])}. Ask Alfred to ignore it if "
                "this is expected."
            ),
            title="Alfred watchdog",
            notification_id=_notification_id(entry.entry_id),
        )

    def status(self) -> dict[str, Any]:
        """What the watchdog is tracking, has done and has learned."""
        return {
            "issues": [
                {"entry_id": entry_id, **issue}
                for entry_id, issue in self.issues.items()
            ],
            "history": list(reversed(self.history[-20:])),
            "learned": self.learned,
            "ignored": sorted(self.ignored),
            "powered_by": {
                entity_id: switch
                for entity_id in sorted(self.feeds)
                if (switch := self.feed_of(entity_id))
            },
            "ignored_integrations": sorted(DEFAULT_IGNORED_DOMAINS - {DOMAIN}),
        }


def _notification_id(entry_id: str) -> str:
    return f"{DOMAIN}_watchdog_{entry_id}"


def async_get_watchdog(hass: HomeAssistant) -> Watchdog | None:
    """The running watchdog, if Alfred is set up."""
    return hass.data.get(STORAGE_KEY)


async def async_start_watchdog(hass: HomeAssistant) -> Callable[[], None]:
    """Start the watchdog; return a stop callback."""
    watchdog = Watchdog(hass)
    await watchdog.async_load()
    hass.data[STORAGE_KEY] = watchdog

    async def _async_tick(now: datetime) -> None:
        await watchdog.async_check()

    stop = async_track_time_interval(
        hass, _async_tick, WATCHDOG_INTERVAL, name="Alfred watchdog"
    )
    stop_listening = hass.bus.async_listen(
        EVENT_STATE_CHANGED, watchdog.async_state_changed
    )

    def _stop() -> None:
        stop()
        stop_listening()
        hass.data.pop(STORAGE_KEY, None)

    return _stop
