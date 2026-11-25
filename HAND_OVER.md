# Home Assistant hand-over

Everything someone (a person or Claude) needs to pick up managing this Home Assistant setup.
The same file lives in both GitHub repositories and in the Claude project's shared files at
`handover/HAND_OVER.md`. A daily scheduled task refreshes it when something has changed.

**Last updated:** 2026-11-25 (04:55)

> **Passwords and tokens are not in this file.** They are kept only in the Claude project's
> shared files at `handover/credentials.md`, which is never committed to git. This file only
> says where each credential lives.

---

## 1. The setup at a glance

| Item | Value |
| --- | --- |
| Owner | Jacques De Kock (GitHub `JacDK777`) |
| Home Assistant version | 2026.11.0 (updated 2026-11-05 about 10:43) |
| Config folder in the Terminal & SSH add-on | `/homeassistant` (not `/config` on this version) |
| Network | TP-Link Deco mesh (Deco app; login in credentials). LAN `192.168.68.x`; HA at `192.168.68.148:8123`; NAS at `192.168.0.153` |
| Remote access | Home Assistant Cloud (Nabu Casa); URL in credentials |
| Claude project | "Alfred and Home Assistant" on claude.ai |
| Phone | Samsung S22 Ultra; side button → Digital assistant → Home Assistant Assist (Alfred) |

## 2. Repositories

### `JacDK777/home-assistant-config`: the live HA config
- Default branch: `main`. This repo **is** the `/homeassistant` folder on the HA box.
- Synced into HA by the **Git pull** add-on:
  - Repository: `https://github.com/JacDK777/home-assistant-config.git` (no token in the URL)
  - Deployment user: `JacDK777`; Deployment password: the fine-grained GitHub token (see credentials)
  - Branch `main`, remote `origin`, Git command `pull`, Auto restart **on**, Polling **on**, interval **300 s**
  - Healthy log ends with "Already up to date".
- The HA box's own git remote was set with
  `git remote set-url origin https://github.com/JacDK777/home-assistant-config.git` (token removed).
- `.gitignore` keeps out: `secrets.yaml`, `.storage/`, `*.db*`, `*.log*`, `.cloud/`, `deps/`, `tts/`,
  `backups/`, `custom_components`, `ip_bans.yaml`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`,
  `.cache/`, `.ha_run.lock`.
  - Automations, scripts and scenes are deliberately **not** in git: they are UI-managed and edited
    through the HA connector, so tracking them would make Git pull conflict.
  - Dashboards (e.g. Main Dash) live in `.storage/` and are therefore **not** in git either.
- `packages/` (loaded with `!include_dir_named packages`): `water_stats.yaml` (config PRs #7/#8: municipal in, rain in,
  house use, irrigation out per zone, live flow rates, `input_boolean.water_house_supply`) and `solar_stats.yaml`
  (PR #9: solar autarky today sensor), and `gas_stats.yaml` (PR #11: `sensor.gas_level_filtered`, 2-hour median of
  1-minute samples to drop junk readings; PR #12 halves double-echo distances). These feed the **House Stats** dashboard.
  `light_switch_buttons.yaml` (PRs #13/#14): room defaults for the remembered switch scenes after a restart (bedrooms about 1%
  at 4110 K) and Tania's office switch as a button. `irrigation.yaml` (PRs #15/#16): Home Assistant runs all Rain Bird watering
  (see section 5). `irrigation_runs.yaml`: measures the litres of every valve run from the tank probe
  (event `irrigation_run_measured`, logbook "Irrigation run", `sensor.irrigation_<zone>_litres_per_10_min`).
  `powercalc.yaml` (PR #24, needs the Powercalc integration from the Marketplace): estimated watts per room from on/off states
  (`sensor.<room>_estimated_power`; typical values, not measured). `oven_energy.yaml` (PR #25): real oven kWh worked out from
  `sensor.oven_mains_1_power` (the CBI switch's own energy counter undercounts), `sensor.oven_energy_today` / `_month`.
- Contents: `configuration.yaml`, `blueprints/`, `esphome/` (empty), `www/community/` (HACS frontend
  cards: button-card, mushroom, template-entity-row, tv-card, flex-horseshoe, atomic-calendar-revive,
  HTML Jinja2 template card).
- **Rule:** all YAML changes go as pull requests for Jacques to approve. Git pull applies them
  within 5 minutes of merge and restarts HA if needed.

### `JacDK777/Home-Assistant-Alfred`: the Alfred custom integration
- Default branch: `claude/amazing-bell-51b2u2` (there is no `main`).
- Alfred = Claude as an Assist conversation agent (HACS custom repository, type Integration).
  Version 0.16.0 (installed 2026-11-11). Default model `claude-haiku-5-5`, requires `anthropic>=0.125.0` (HA's pin).
- Merged PRs: #1 CI fixes + MIT license, #2 `voluptuous-openapi` requirement,
  #3 Alfred admin tools (error log, automations/scripts, GitHub issues), #4 tool-format fix for HA 2026.9+,
  #7 dashboard tools (list/get/save_dashboard) + find_entities, #8 check_tuya_local_devices /
  update_tuya_local_device, #9 auto-reconnect of Tuya Local devices to their new IP every 5 min,
  #10 rename_entity / rename_device, #12 HACS tools (list_hacs_repositories, download_hacs_repository)
  and dashboard resource tools (list/remove_dashboard_resource). Alfred always names the exact version and
  waits for a yes before downloading anything from HACS. #15 (0.8.0) scenes (get/save/delete_scene), delete_automation /
  delete_script and call_action (run any HA action). #16 butler icon. #17 (0.9.0) Huckleberry medication and
  temperature actions, #18 (0.10.0) Huckleberry activity timers (bath, tummy time), #19 (0.10.1) device-registry deprecation fix.
  #20/#21 (0.11.x) fresh Tuya Local keys from the Tuya cloud integration, #22/#23 (0.12.0/0.12.1) watchdog,
  #24 (0.12.2) `set_entity_enabled` and `tuya_cloud_device` (read and change Smart Life device settings, everything but pairing).
  #26 create/update/delete_dashboard, #27 (0.14.0) HACS tools use HA 2026.11's built-in Marketplace,
  #25 + #28 (0.15.0) `pair_tuya_devices` (EZ-mode re-pairing; Tuya IoT and Wi-Fi details in Alfred's options),
  `add_hacs_custom_repository` and `list_bluetooth_devices`.
  #29 (0.16.0) watchdog re-pairs a bulb by itself when it stays offline 10 min with its switch on.
  #31 (0.17.0) `alfred.tv`: presses keys, types, or opens an app or link on an Android TV over the Android Debug Bridge
  integration, then returns a fresh screenshot and the on-screen text (HA's own screenshot only refreshes every few minutes).
  #36 (0.18.2, 2026-11-22) the watchdog follows an Android Debug Bridge TV to its new IP address.
  #33 (0.18.1, 2026-11-18) closes a Tuya Local setup left open for the device before adding it.
  #32 (0.18.0, 2026-11-16) `add_tuya_local_device`: adds a Smart Life device to Tuya Local by itself (key from the Tuya cloud
  integration, IP from the network) and can replace a dead device, taking over its entity IDs, name and area.
  Alfred can restart HA itself (call_action).
- After updating Alfred (`update.alfred_update` install + restart; see the standing rule below), start a **new Claude thread** to see the new tools.
- **Standing rule (Jacques, 2026-11-06): after Jacques merges an Alfred PR, Claude installs the update itself** (`update.install` on
  `update.alfred_update` with the merge commit, then restart HA) without asking. HA 2026.11 replaced HACS with the built-in
  Marketplace, which blocks installs until its warning is accepted once in the UI.
- **Standing rule (Jacques, 2026-11-07): always give the best possible fix, even an unconventional one.** Before asking Jacques
  to do something by hand (Smart Life app, HA UI), check whether Claude can do it: hidden or disabled entities (for example
  the Tuya cloud `select.*_power_on_behavior`), the Tuya cloud API via HA, or a new Alfred tool in a PR. Offer that first.
- Wall switches: set Power-on behavior / Relay Status to **memory** ("Restart Memory"). Tuya Local `*_initial_state` does not stick on
  most switches, so use the Tuya cloud selects (enable with `set_entity_enabled`, Alfred 0.12.2+).
- `scripts/tuya_swap.py` (merged in #7): swaps Tuya cloud entities
  on a dashboard for their Tuya Local twins by Tuya device ID. Dry run by default, `--apply` backs up
  the dashboard first, then restart HA. Run in the Terminal add-on:
  `curl -sL https://raw.githubusercontent.com/JacDK777/Home-Assistant-Alfred/3e492a6a44986e53616657a7cc435a63ce3d07e6/scripts/tuya_swap.py -o /tmp/tuya_swap.py && python3 /tmp/tuya_swap.py`
  (`apk add python3` if python3 is missing).

#### Alfred: Smart Life control and re-pairing (PRs #24, #25, #29; 2026-11-10/11)
- `tuya_cloud_device`: reads and changes any Smart Life device setting through HA's Tuya integration (for example `relay_status=last`). All 8 wall switches are set to "last state".
- `pair_tuya_devices`: re-adds devices in EZ pairing mode (fast flash) through the Tuya IoT Platform project "HA TuyaLocal" (Central Europe). The credentials and Wi-Fi details are in Alfred's options. The free IoT Core plan needs renewing every few months (error 28841002 means it has expired).
- The watchdog re-pairs a Tuya Local bulb by itself when it stays offline for 10 minutes while its known switch is on. It tries at most twice per outage and at most once every 6 hours.
- Taught feeds: `switch.main_bedroom_light_switch_1` powers Main Bedroom DL1–5. `light.great_room_light_switch_3_gang_1` powers the 23 Great Room bulbs.
- Kitchen DL1: the socket is empty (bulb removed 2026-11-16). `light.kitchen_dl_1` is disabled but keeps its name, group and
  automation places; when a working bulb is fitted, add it in Smart Life and swap it in with `add_tuya_local_device`.
  The leftover Smart Life entries "Kitchen 1" and "PAR16-PA-WIFI-TY-RGBCW 23" can be deleted. Re-pairing hasn't been proven on a real flashing bulb yet.
- In this cloud session, new Alfred tools can be called through `conversation.process` with agent `conversation.alfred` until a new session lists them.

### `JacDK777/Alfred_App`: the standalone Alfred phone app
- Android app (sideloaded on the S22): "Hey Alfred" wake word, Claude (Sonnet by default) plus the HA MCP server,
  so it has the same Assist and Alfred admin tools and asks before changing anything. Admin tools need an admin HA token.
- Installs from the repo's Releases page; since PR #10 it checks GitHub on open and offers a one-tap **Update**
  (also Settings → Updates → Check for Updates). Can be set as the phone's Digital assistant app.
- Voice: the user's own designed Afrikaans "Alfred" voice on ElevenLabs (credit-limited; ran out on 2026-11-05),
  PR #11 added Azure Speech "Willem" (free F0 tier, key pasted in Settings → Free voice, region South Africa North)
  and an offline Afrikaans voice as backup. Keys live in the app's settings, not in this doc.
- Version 0.1.48 on the S22 (2026-11-14). Moving to its own Claude project; its full hand-over is
  `handover/ALFRED_VOICE_APP_HANDOVER.md` in this project's shared files (no secrets).
- Worked on in the "Build standalone Alfred voice app" thread. Not in this hand-over routine's GitHub scope, so no copy of this doc there.

## 3. How Claude reaches Home Assistant

- **HA MCP connector** (claude.ai custom connector at `https://<HA internet URL>/api/mcp`, signed in
  with the HA admin account). Exposes the **Assist** API (devices exposed to Assist) and **Alfred admin**
  (error log, automations/scripts, dashboards, entity lookup and rename, Tuya Local device checks and IP
  updates). It cannot read YAML files.
- **GitHub** for YAML (PRs on `home-assistant-config`) and Alfred code (PRs on `Home-Assistant-Alfred`).
- **Terminal & SSH add-on** for anything else: Jacques runs commands and pastes the output.
  Paste is Ctrl+Shift+V; password prompts show nothing while typing.

## 4. Alfred configuration (Settings → Devices & services → Alfred → Configure)

- Control Home Assistant: **Assist** + **Alfred admin**.
- Web search: can be enabled ("Allow web search"); about US$0.01 per search, max 3 per question.
  Suggested instruction line for general-assistant use is in the Alfred thread.
- Optional GitHub token field (Issues read/write on the Alfred repo) for bug reports; issues are public.
- Admin tools only work for HA admin users.
- Wake word: a phone can't do "Hey Alfred"; S22 uses side-button hold. A real "Hey Alfred" needs a
  Voice PE / Wyoming satellite with a custom openWakeWord or microWakeWord model.

## 5. Devices and integrations

- **Tuya → Tuya Local (make-all, installed via HACS custom repo `https://github.com/make-all/tuya-local`)**.
  Added via Smart Life QR login; each device needs its IP (reserve it in the router).
  - Wall switches: Foyer, Great Room, Outside, Garage, Pantry, Wine Cellar, Guest Bathroom, Guest Bedroom,
    Main Bedroom, En-Suite, JDK Study, Braai. 3-gang switches (great room, downstairs) appear as
    `light.*_gang_N` in Tuya Local.
  - Plugs: Oven Mains, Heat Mat socket 1, Grow Domes, Wine Cellar "Open Switch".
  - Lights (BNETA = Tuya): Kitchen, Kitchen Pendants, Dining, Dining Room DL2, Chandelier, Main Bedroom,
    Great Room, Living Room, Braai Room DL1, Tania Office Light LB 1.
  - Tania office switch gangs are named "Tania's Office Light" and "Tania's Office Bathroom Light".
  - 45 Tuya Local devices. "error 901: Unable to Connect" means the bulb is unreachable: either its wall switch is
    off (no power) or its IP changed. On 2026-10-18, 28 bulbs had new IPs; all were reconnected and Alfred now
    follows IP changes automatically. 4 Living Room DLs were re-added with new local keys. Details:
    `/mnt/project-files/tuya-local/bulb-ip-check.md`. Reserving every Tuya device's IP in the Deco is still recommended.
    Known feeds: **Main Bedroom Light Switch 1** → Main Bedroom DL 1–5.
  - Ginny vacuum is on Tuya Local (`vacuum.ginny_2`, `sensor.ginny_battery_2`).
- **Main Dash** (`lovelace` dashboard `new-dash-v1`, UI-managed) uses only Tuya Local entities since 2026-10-18;
  the Grow Domes card uses `switch.plant_fan_1` and the Ginny tiles use the Tuya Local vacuum.
  The water tank cards are on **House Stats → Water** and use the ESPHome probe (`sensor.outside_water_tank_*`) since 2026-11-20.
  - Old cloud **Tuya** integration is kept but most of its entities are disabled. Still enabled: Temp & Humidity
    Sensor (no local copy) and an unnamed A60 bulb.
- **Samsung aircons (3)**: probably SmartThings (cloud). Local options depend on the model number
  (climate_ip for older Wi-Fi units, ESPHome samsung_ac + RS485 for WindFree). Model number not yet given.
- **Comfee (Midea) aircon:** plan (2026-11-22) to control it locally with an ESP32 running ESPHome's Midea component on the
  unit's USB port (ESP32 WROOM DevKit, 4-channel 3.3V↔5V level shifter, USB-A plug; Jacques buys from Micro Robotics or
  ISP Home). Claude sends the wiring and ESPHome config once the parts arrive.
- **TCL TVs (3)** via Chromecast/DLNA: Living Room TCL TV at 192.168.68.101, Bedroom TV at 192.168.68.167. Cast can turn
  them off but not on (they drop off Wi-Fi in standby). **Android TV Remote** integration is now added for both
  (`remote.living_room_tcl_tv` / `media_player.living_room_tcl_tv_2`, `remote.bedroom_tv` / `media_player.bedroom_tv`).
  Living Room TV shows as three duplicate entities.
  - **Remotes** dashboard: TV remotes use Universal Remote Card 4.12.1 (HACS); old android-tv-card / tv-card
    resources removed. Keyboard buttons use live typing; scripts `script.living_room_tv_type_text` and
    `script.bedroom_tv_type_text` send text. Live typing needs **Enable IME** on (Android TV Remote → Configure).
- **Huckleberry** (baby tracker, Mika): HACS custom repo `Woyken/huckleberry-homeassistant` (now v0.5.9), added 2026-10-25,
  signed in with the Huckleberry account (unofficial Firebase API). Device "Mika" (`device_id 3445e23e0b7211c4ef1c664f8163cdb5`):
  `sensor.mika_sleep/nursing/bottle/diaper/growth/medication`, `switch.mika_sleep_timer`, `switch.mika_nursing_left/right`,
  `calendar.mika_events`. Actions `huckleberry.log_bottle`, `log_diaper_*`, `log_activity`, `start/complete_sleep` etc.
  **Mika** dashboard (`dashboard-mika`, UI-managed) has status tiles, quick-log buttons (bottles are breast milk) and a history calendar.
  Voice logging via Alfred: scripts `script.mika_log_bottle` (default breast milk), `mika_log_nappy` (wet/dirty/both/dry with pee size, poo size, colour, consistency; refuses and asks if any are missing), `mika_sleep`, `mika_log_activity`,
  `mika_log_solids`, breastfeeding `mika_breastfeed_start` (asks "which side?"), `mika_breastfeed_start_left/right`,
  `mika_breastfeed_stop_left/right` (pause that side, feed stays open), `mika_breastfeed_stop` (save the feed), and
  `mika_nursing` ("breastfeeding extras": switch/pause/resume/cancel). Helper `mika_breastfeed_stop_side` is not exposed.
  Added 2026-11-04: `mika_log_medication` and `mika_log_temperature` (need Alfred 0.9.0, Alfred PR #17, which adds `alfred.huckleberry_log_medication` / `huckleberry_list_medications` / `huckleberry_log_temperature` because the Huckleberry integration has no medicine or temperature action; unknown medicines are refused unless confirmed as new), plus `mika_log_growth` (kg/cm) and `mika_log_pump` (ml + minutes). Live timers `mika_bath` and `mika_tummy_time` (start/stop/pause/resume/cancel) need Alfred 0.10.0 (PR #18, `alfred.huckleberry_activity_timer`); `mika_log_activity` still logs a finished one. Huckleberry is now v0.5.9. Exposed scripts and Mika sensors must be ticked in Settings → Voice assistants → Expose. Mika is mostly breastfed; bottles are breast milk.
- **Rain Bird** (controller at 192.168.68.168, reserved in the Deco). Zones: 1 Municipal Tank Input (fills the 10,000 L tank,
  about 2,000 L/h, measured 2026-11-18), 2 Front Gardens (fynbos and succulents), 3 Front Grass, 4 Back Yard (grass plus tree drippers),
  5 and 6 unused. Since 2026-11-18 the controller has **no programs**: `automation.irrigation_run_zone_schedules` (config PRs #15/#16)
  runs each zone from `input_*.irrigation_<zone>_*` helpers (days, start, minutes, optional second run), with master switch
  `input_boolean.irrigation_ha_schedule`, skipping at 2 mm forecast rain or tank below 15%. If HA is down, nothing waters.
  **Irrigation** dashboard (`irrigation-dashboard`) shows the week and has an edit page per zone. The Tuya water tank sensor
  level now comes from the ESPHome pressure probe `water-tank` (DFRobot KIT0139, 0–5 m, 4–20 mA, installed 2026-11-20):
  `sensor.outside_water_tank_water_level` and `..._water_volume` (litres; about 9,650 L above the probe when full). The old Tuya
  "Water Tenk 1" ultrasonic sensor was removed.
  Since 2026-11-19 irrigation, garden and tank work has moved to Jacques's separate garden and irrigation Claude project
  (handover: "Garden, plants and irrigation handover" Claude doc; builder files copied from `irrigation/` in these project files).
  The weekly re-plan reminders run there now.
- **Ring** camera, **Baby Camera** motion sensor, garage door, ESPHome
  device `eyegas` at 192.168.68.115 (the LPG gas Bluetooth bridge; keep it).
- **Gas (LPG, 19 kg cylinder):** eyeGas meter = Mopeka Pro Check ("Pro Check 8B5E"), read by the built-in Mopeka
  integration; level `sensor.outside_eyegas_bridge_lpg_level` (through an ESP32 Bluetooth proxy, set up 2026-11-10).
  Since 2026-11-22 the gauge, history and alert read only from the ESPHome gas bridge; the separate Bluetooth "Pro Check 8B5E"
  Mopeka entry is unused and Jacques can delete it (Settings → Devices & services → Mopeka).
  Readings fail when the sensor can't see through the cylinder: keep the base clean, sensor centred, cylinder level.
  Raw readings jump around (junk 6%/41%/100% values), so the Gas tab and the alert use `sensor.gas_level_filtered`.
- **Solar:** Sunsynk integration (battery held at 50% overnight for outages, by choice). **House Stats** dashboard
  (`house-stats`, created 2026-11-10): gas gauge and history, Electricity (Sunsynk power flow card in Lite style,
  daily autarky chart), Water tab, appliance power. **Home Energy Plan** dashboard (`home-energy-plan`, 2026-11-18): schematic
  floor plan with live watts per room. Only 5 plugs report power (oven mains, heat mat, plant fan, baby camera plug, Mika baby
  monitor); aircon power sensors read 0 W. Since 2026-11-23 the plan follows Jacques's own pins (architect drawings): Powercalc room
  estimates shown with "~", oven kWh today, chandelier, gas canister and geyser, dishwasher plug, chips that glow when on, an
  "On now" row, and a phone layout (floors stacked).
- **Mika baby monitor** plug (`switch.mika_baby_monitor`, Tuya Local at 192.168.68.150, added 2026-11-18), tile on Main Dash (Guest Rooms).
- **TV Light** (MohuanLED Bluetooth strip, shows as BJ_LED_M): BJ_LED BLE custom integration from the Marketplace
  (2026-11-10). It doesn't report its real state. Only one connection at a time, so close the MohuanLED app.

## 6. Automations (UI-managed, not in git)

All automations point at Tuya Local entities (repointed 2026-10-18).

| Name | Entity | State | Notes |
| --- | --- | --- | --- |
| Wake up call | `automation.wake_up_call` | off | |
| Garage Light Activate | `automation.garage_light_activate` | on | Garage light 05:00–09:00 on weekdays |
| Garage Door Automation Light Swicth | `automation.garage_light_auto` | on | Garage light on for 10 min when the door opens |
| Garage Door Open/Close | `automation.new_automation_2` | on | Switch on opens, switch off closes (no door-state check). Ignores unavailable/unknown (no opening on restarts) and switch changes made by an automation, so the sync below never moves the door. 5-second lockout after each command: wait ~5 s between presses |
| Garage Switch Follows Door | `automation.garage_switch_follows_door` | on | Keeps the garage wall switch (gang 3) in step with the door. Re-checks on door change, HA start, switch or door coming back online, and every 2 minutes; only acts when they disagree and the switch wasn't pressed in the last 90 s (updated 2026-11-08) |
| Water Tank Notification | `automation.water_tank_notification_2` | on | Safety net on the ESPHome probe: alerts if the tank is below 10% for 10 min, or the probe reports a fault for 15 min (2026-11-20) |
| Grow Lights Timer | `automation.grow_lights_timer` | on | Drives `switch.plant_fan_1` (Grow Domes) |
| Great Room bulbs auto power cycle | `automation.great_room_bulbs_auto_power_cycle` | off | Turned off 2026-11-16; see below |
| Main Bedroom bulbs auto power cycle | `automation.main_bedroom_bulbs_auto_power_cycle` | off | Turned off 2026-11-16; see below |
| Guest Bedroom bulb auto power cycle | `automation.guest_bedroom_bulb_auto_power_cycle` | off | Turned off 2026-11-16; see below |
| Tania's Office bulb auto power cycle | `automation.tania_s_office_bulb_auto_power_cycle` | off | Turned off 2026-11-16; see below |
| Bulbs: reconnect when their wall switch turns on | `automation.great_room_bulbs_reconnect_when_switch_turns_on` | on | When Great Room gang 1, Main Bedroom or Guest Bedroom switch turns on, reloads the Tuya Local entry of any of its bulbs still unavailable at 5, 20 and 45 s. Never touches power (added 2026-11-16) |
| Wall switches act as buttons (keep bulbs powered) | `automation.great_room_switch_acts_as_a_button_keep_bulbs_powered` | on | When one of those three switches turns off, turns it straight back on and toggles its bulbs: if any were on, saves them in `scene.last_on_<switch>` and turns all off; if all were off, restores that scene (if none is saved, e.g. after a restart, it uses `scene.great_room_default`). Bulbs blink briefly on each press because the relay cuts power for a moment. Disable to get the old behaviour (added 2026-11-16) |
| Tania's office switch acts as a button (keep bulb powered) | `automation.tania_s_office_switch_acts_as_a_button_keep_bulb_powered` | on | Config package; toggles Tania Office Light LB 1 at once on each press (added 2026-11-18) |
| Light switches: seed remembered state with room defaults | `automation.light_switches_seed_remembered_state_with_room_defaults` | on | Config package; after a restart, fills `scene.last_on_*` with room defaults |
| Git pull: restart if stopped | `automation.git_pull_restart_if_stopped` | on | Starts the Git pull add-on again when it has been stopped 15 min (it exits when GitHub is unreachable; it was stopped 20 Nov 11:20 to 21 Nov 05:37). Also checks every 30 min (added 2026-11-21) |
| Irrigation: run zone schedules | `automation.irrigation_run_zone_schedules` | on | Config package; runs the Rain Bird zones (section 5) |
| Update available - ask to install | `automation.update_available_ask_to_install` | on | Phone notification with Install / Not now; backs up first |
| Water Tank: store reading from probe | `automation.water_tank_store_reading_after_measuring_window` | on | Every 10 min copies the probe level (`sensor.outside_water_tank_water_level`) into `input_number.water_tank_level`; at 05:30, 07:30, 19:30 and 22:30 also fires `water_tank_level_stored` (drives the low-level alert and the water balance). Skips while the probe reports a fault (2026-11-20) |
| Water Tank: low level alert with fill option | `automation.water_tank_low_level_alert_with_fill_option` | on | After each stored reading below 20%, asks on the JDK phone to fill; Yes runs Rain Bird "Municipal Tank Input" for 30 min |
| Jacques onthou: huis toe vir rugby | `automation.jacques_onthou_huis_toe_vir_rugby` | on | One-off reminder made by Alfred: 12:30 on 2026-11-07, phone notification if Jacques isn't home (rugby). Expired; safe to delete |

**Bulb auto power cycle (all four off since 2026-11-16, replaced by the two automations above):** when the room's wall switch turns on, watch its bulbs for up to 5 minutes and stop as
soon as all connect. Only if a bulb is still unavailable after 5 minutes **and the switch stayed on the whole
time** does it notify the JDK phone and power-cycle the switch once (10 s off), then wait 10 minutes. It never
turns on a switch the user turned off (reworked 2026-10-20 after it toggled lights needlessly with a 2-minute check). The Great Room switch feeds 23 bulbs,
including the kitchen pendants. Added after a second outage on 2026-10-18 (~10:15) where bulbs refused
connections (error 901) with no IP change until their power was cycled. Avoid using the Smart Life app on the
home Wi-Fi, which can lock bulbs out of local connections.

**Updates:** Home Assistant does not auto-install Core/OS updates; the notification above asks first.

Scripts: `script.living_room_tv_type_text`, `script.bedroom_tv_type_text` (send text to the TVs); `script.netflix_living_room`,
`script.dstv_stream_living_room`, `script.dstv_sport_living_room` (open those apps on the living room TV, seen 2026-11-07);
`script.dstv_supersport_grandstand_living_room`, `..._cricket_...`, `..._rugby_...` (open those SuperSport channels, seen 2026-11-14);
19 `script.mika_*` Huckleberry voice-logging scripts (section 5).

Added 2026-11-10: **Gas low alert** (`automation.gas_low_alert`, notifies Jacques and Tania when the filtered level stays at
10% or less for 30 minutes, with kg left; changed 2026-11-12 after a false alert)
and **TV Light follows Living Room TV** (`automation.tv_light_follows_living_room_tv`, on/off with the TV, ignores drop-outs).

## 7. Open items

- Gas: double echoes (about 650 mm instead of 325 mm) still happen; config PR #12 (merged 2026-11-17) halves impossible distances,
  so the gauge reads correctly (69%, about 13.1 kg on 2026-11-17; battery 94%). Optional: re-seat the sensor dead centre on a clean spot.
- Gas sensor silent since 2026-11-20: its CR2032 went flat in 6 days (fitted 2026-11-14; the cell before also dropped fast), so
  something drains it. The bridge is fine. Jacques to check for moisture or corrosion inside, fit a name-brand cell and remount dead
  centre; then watch the battery for a day or two. If it falls more than a few percent a day, it's a warranty claim with eyeGas.
- Delete the dead helper `light.kitchen_grow_domes_socket_1` (no longer used).
- TVs can't be turned on from HA (see section 5).
- Garage switch: back online (seen 2026-11-08). On 2026-11-08 a press did nothing because the switch was on while the door was shut;
  the 2-minute re-sync above should stop that. Confirm the next press works (user).
- Alfred app: waiting on the user to create the Azure Speech key and try the Willem and offline voices (2026-11-05).
- Kitchen DL1: no bulb fitted; entity disabled until one is (see section 2).
- Wi-Fi drop-outs: on 2026-11-21 and 2026-11-22 (17:52) many Tuya devices (wall switches, bulbs, Ginny) dropped at once. Both drops
  came right after Home Assistant restarted itself following a config merge (Git pull restarts HA). Bulbs come back after power
  returns, but the wall switches, Ginny and similar devices lock up and need a power cycle. All switches and bulbs were back on
  2026-11-22 20:39. Proposed: apply config merges with a restart at 3 AM instead of straight away, and test it; waiting on Jacques's go.
- Still offline on 2026-11-22: Ginny (re-dock or power cycle), baby camera and its Camera On/Off plug, Ring (may need signing in again),
  Tuya cloud devices (blinds motor, two cloud bulbs, temperature/humidity sensor; Tuya cloud failing, probably error 1108 below).
- Braai Room DL 2 was deleted by accident with Water Tenk 1 on 2026-11-22 and re-added with its old name and group. Braai Room DL 1 has no bulb.
- Water tank probe is live (2026-11-20, config PR #17); per-zone flow rates re-measured with it (config PRs #20/#21, 2026-11-22).
  The ESPHome `eyegas` entry is the LPG gas bridge: do not delete it.
- Home Energy Plan: phone layout done 2026-11-23; waiting on Jacques to say if any chip label is still cut off on his phone.
- Config PR #23 (irrigation run logger fix: wrong tank sensor id) is open, from the garden project; waiting on Jacques to merge.
- DHCP reservations for the bulbs and switches (plus the Mika baby monitor plug at .150): list at `tuya-local/dhcp-reservations.md` in the project files, for Jacques to add in the Deco app.
  The Tuya IoT Core free plan needs renewing every few months (error 28841002 means expired; renewed 2026-11-10). Since about
  2026-11-21 the watchdog's re-pairing fails with error 1108 ("uri path invalid"): renew or check the IoT project.
- Alfred app PR #17 ("look" tool: TV screenshots and camera snapshots) ready to merge; seeing the TV needs the
  Android Debug Bridge integration set up on the living room TV (user step).
- Garage door was seen reporting "open, 100%" while shut on 2026-10-26; if the switch light drifts from the door again, check the door's reported position.
- Bulbs: root cause is wall switches cutting power to smart bulbs. On 2026-11-16 Jacques chose switches-as-buttons for Great Room
  gang 1, Main Bedroom and Guest Bedroom (automations above); power cycling is off. Confirm it behaves well in daily use
  (Tania's Office got the same on 2026-11-18). Alfred app PR #25 (Alfred diagnoses offline bulbs instead of power cycling) is merged (seen 2026-11-22).
  The four power-cycle automations are off, not deleted; delete them if Jacques agrees.
- Remotes: DStv Sport button doesn't open DStv (needs the DStv app ID) and the Keyboard doesn't type yet;
  that thread was closed on 2026-11-08 (DStv Sport now has its own script). Optional restyle of both remotes offered.
- Load Shedding: area "Malmesbury (12)" has a legacy v2 ID and is skipped until re-added.
- `configuration.yaml` still references `samsungtv_tizen` (not installed) with placeholder host/MAC.
- NAS password moved to `secrets.yaml` (config PR #2), but it is still in git history: change the NAS password.
- Ring camera FFmpeg timeout / push client "Incorrect padding" errors.
- Aircon model number needed to choose a local integration.
- GitHub token for Git pull expires about **2027-10-17** (1-year fine-grained PAT, Contents read/write,
  only `home-assistant-config`). Renew before then and paste the new one into the Git pull password field.
  Regenerated on 2026-10-17 after it appeared in a screenshot.
- Alfred: no GitHub release yet (HACS shows a commit id); no `main` branch.

## 8. Change log

- 2026-11-25: Gas sensor found silent since 2026-11-20 (battery drained in 6 days; check at the tank, possible warranty claim).
- 2026-11-24: Config PRs #24 (Powercalc room estimates) and #25 (oven kWh) merged; Home Energy Plan reworked ("On now", glowing chips, phone layout); config PR #23 open.
- 2026-11-23: Alfred 0.18.2 (PR #36); Wi-Fi drop-outs traced to HA restarts after merges (3 AM restart proposed); downstairs switches back; Water Tenk 1 removed, Braai Room DL 2 re-added; gas reads from the ESPHome bridge only; Comfee aircon ESP32 plan.
- 2026-11-22: Irrigation run logger (`packages/irrigation_runs.yaml`, config PR #20): each valve run's litres measured from the tank probe, 15 min after the valve closes. Measured per 10-min run: Front Gardens ~525 L, Back Yard ~490 L, Front Grass ~420 L, tank fill ~380 L in. Water stats use the measured flow rates (PR #21, Front Grass 38 L/min).
- 2026-11-22: "Git pull: restart if stopped" automation; Downstairs Light Switch 3 diagnosis (likely the downstairs Deco); Tuya re-pairing error 1108.
- 2026-11-21: Water tank moved from the Tuya ultrasonic sensor ("Water Tenk 1", removed) to the ESPHome pressure probe `water-tank` (config PR #17, merged 2026-11-20). Tank automations, House Stats water cards and the water balance use it; the balance uses probe litres instead of 91.4 L per 1%.
- 2026-11-20: Irrigation work moved to a separate garden and irrigation project (reminders there).
- 2026-11-19: Irrigation moved into HA (config PRs #15/#16, Irrigation dashboard, controller programs deleted); light switch package (PRs #13/#14: bedroom defaults, Tania's office as a button); Alfred 0.18.1 (PR #33); Mika baby monitor plug; Home Energy Plan dashboard.
- 2026-11-18: Config PR #12 merged (gas double-echo fix, gauge correct); Kitchen DL1 socket empty and entity disabled; `scene.great_room_default` fallback for the switch buttons; switch drop-outs raised as a problem.
- 2026-11-17: Alfred 0.18.0 (PR #32, `add_tuya_local_device`); wall switches act as buttons and bulbs reconnect after 5 s (power-cycle automations off); Kitchen DL1 replacement pending; gas battery replaced, double-echo fix in config PR #12; Downstairs Light Switch 3 offline; DHCP reservation list.
- 2026-11-16: Alfred 0.17.0 (PR #31, `alfred.tv` TV control with fresh screenshots); Alfred app PR #20 merged.
- 2026-11-15: Gas bridge back online, sensor battery at 7%; separate Alfred voice app hand-over doc noted.
- 2026-11-14: Three DStv SuperSport channel scripts (Grandstand, Cricket, Rugby) for the living room TV.
- 2026-11-13: Filtered gas level sensor (config PR #11) and 30-minute hold on the gas alert; gas bridge offline and low sensor battery added to open items.
- 2026-11-12: Alfred 0.16.0 (PR #29, watchdog re-pairs bulbs itself, taught switch feeds) installed; Smart Life and re-pairing notes moved into section 2; app PR #20.
- 2026-11-10: Gas level via Mopeka/ESP32 proxy and Gas low alert; House Stats dashboard (solar, water, gas) with config packages `water_stats` and `solar_stats` (PRs #7–#9); TV Light (MohuanLED, BJ_LED) following the TV; Alfred 0.15.0 (dashboards, Marketplace, re-pairing, Bluetooth); Mika voice logging confirmed.
- 2026-11-09: Garage Switch Follows Door now re-syncs every 2 min and on reconnect; garage switch back online; bulb and remote threads closed.
- 2026-11-08: New one-off automation "Jacques onthou: huis toe vir rugby" (7 Nov reminder, now expired) noted.
- 2026-11-07: Alfred 0.12.2 (Smart Life settings tools, watchdog fixes); re-pairing PR #25 and app look tool open; living room TV app scripts; Main Bedroom bulbs fixed (removed from open items); standing rules added.
- 2026-11-06 (later): Alfred 0.12.0 (PR #22) adds a **watchdog**: every 5 min, entities unavailable 10+ min get their integration reloaded (back-off 10m→6h, max 5 per check); Tuya Local devices that don't announce (no power) are skipped; it learns which wall switch powers which bulb and ignores bulbs while their switch is off; notifies after 3 failed tries; stops reloading integrations reloads never fix. Tools: get_watchdog_status, set_watchdog_ignore, set_watchdog_feed. TVs/phones ignored by default. Data in `.storage/alfred.watchdog`.
- 2026-11-06: Main Bedroom DL 1–5 were unavailable since the HA 2026.11.0 update (bulbs refused the saved local key, tinytuya 914). Alfred 0.11.0/0.11.1 (PRs #20, #21) now copies fresh local keys from the Tuya cloud integration into Tuya Local automatically (5-min check) and via `update_tuya_local_device refresh_local_key`. Bedroom bulbs fixed. All Tuya IPs are reserved in the Deco (confirmed by Jacques).
- 2026-11-06: HA 2026.11.0; standalone Alfred app (`JacDK777/Alfred_App`, updater, voices) documented; Main Bedroom bulbs offline added to open items.
- 2026-11-04: Voice logging for medication, temperature, growth and pumping (4 new Mika scripts; Alfred PR #17 for medication/temperature), plus bath and tummy time timers (Alfred PR #18); Alfred 0.10.1 installed (PR #19 fixed device-registry deprecation warnings). Alfred can now restart HA itself via call_action.
- 2026-11-04: Mika voice scripts confirmed exposed to Assist; open item narrowed to sensors and a test log.
- 2026-10-31: Two new water tank automations (stored readings in quiet windows; low-level alert with a 30-minute municipal fill option).
- 2026-10-27: Alfred 0.8.0 (scenes, delete, call_action) and butler icon; garage wall switch offline; standalone Alfred app planned.
- 2026-10-26: Garage fixed after the door closed itself again: switch on opens, off closes, automation-made switch changes are ignored, 5-second lockout, and Switch Follows Door is back on (confirmed working); Mika breastfeeding voice scripts added, bottles are breast milk.
- 2026-10-25: Huckleberry integration (v0.5.5), Mika dashboard and six Mika voice-logging scripts for Alfred added.
- 2026-10-23: Garage Door Open/Close fixed (it sent open then close, so the door stopped partway after the move to Tuya Local).
- 2026-10-21: Bulb power cycle automations reworked (5-minute watch, never re-enable a switch turned off); bulb root-cause options added; scripts listed.
- 2026-10-19: Alfred 0.7.0 (HACS and dashboard resource tools); Universal Remote Card 4.12.1 with live keyboard; Android TV Remote for both TVs; open remote issues and load-shedding area noted.
- 2026-10-18 (11:15): Ginny and all automations on Tuya Local; Tuya cloud mostly disabled; garage switch follows door; 4 bulb auto-power-cycle automations; update-available notification.
- 2026-10-18 (later): Alfred 0.6.0 (dashboard, rename and Tuya Local tools, auto IP follow); Main Dash fully on Tuya Local; 28 bulbs reconnected after IP changes; Tania office names.
- 2026-10-18: Rain Bird IP and fix, TV power-on cause, Grow Lights Timer failing on Tuya cloud, network/remote-access details, GitHub token regenerated.
- 2026-10-17: Hand-over doc created. Credentials collected; NAS mount command moved to secrets.yaml (config PR #2). Garage door no longer opens on HA/Tuya restarts.
