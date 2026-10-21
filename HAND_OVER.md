# Home Assistant hand-over

Everything someone (a person or Claude) needs to pick up managing this Home Assistant setup.
The same file lives in both GitHub repositories and in the Claude project's shared files at
`handover/HAND_OVER.md`. A daily scheduled task refreshes it when something has changed.

**Last updated:** 2026-10-21 (06:55)

> **Passwords and tokens are not in this file.** They are kept only in the Claude project's
> shared files at `handover/credentials.md`, which is never committed to git. This file only
> says where each credential lives.

---

## 1. The setup at a glance

| Item | Value |
| --- | --- |
| Owner | Jacques De Kock (GitHub `JacDK777`) |
| Home Assistant version | 2026.10.2 (from `.HA_VERSION`) |
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
- Contents: `configuration.yaml`, `blueprints/`, `esphome/` (empty), `www/community/` (HACS frontend
  cards: button-card, mushroom, template-entity-row, tv-card, flex-horseshoe, atomic-calendar-revive,
  HTML Jinja2 template card).
- **Rule:** all YAML changes go as pull requests for Jacques to approve. Git pull applies them
  within 5 minutes of merge and restarts HA if needed.

### `JacDK777/Home-Assistant-Alfred`: the Alfred custom integration
- Default branch: `claude/amazing-bell-51b2u2` (there is no `main`).
- Alfred = Claude as an Assist conversation agent (HACS custom repository, type Integration).
  Version 0.7.0. Default model `claude-haiku-5-5`, requires `anthropic>=0.125.0` (HA's pin).
- Merged PRs: #1 CI fixes + MIT license, #2 `voluptuous-openapi` requirement,
  #3 Alfred admin tools (error log, automations/scripts, GitHub issues), #4 tool-format fix for HA 2026.9+,
  #7 dashboard tools (list/get/save_dashboard) + find_entities, #8 check_tuya_local_devices /
  update_tuya_local_device, #9 auto-reconnect of Tuya Local devices to their new IP every 5 min,
  #10 rename_entity / rename_device, #12 HACS tools (list_hacs_repositories, download_hacs_repository)
  and dashboard resource tools (list/remove_dashboard_resource). Alfred always names the exact version and
  waits for a yes before downloading anything from HACS.
- After updating Alfred (HACS redownload + restart), start a **new Claude thread** to see the new tools.
- `scripts/tuya_swap.py` (merged in #7): swaps Tuya cloud entities
  on a dashboard for their Tuya Local twins by Tuya device ID. Dry run by default, `--apply` backs up
  the dashboard first, then restart HA. Run in the Terminal add-on:
  `curl -sL https://raw.githubusercontent.com/JacDK777/Home-Assistant-Alfred/3e492a6a44986e53616657a7cc435a63ce3d07e6/scripts/tuya_swap.py -o /tmp/tuya_swap.py && python3 /tmp/tuya_swap.py`
  (`apk add python3` if python3 is missing).

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
  The **Remotes** dashboard water tank uses `sensor.water_tenk_1_level`.
  - Old cloud **Tuya** integration is kept but most of its entities are disabled. Still enabled: Temp & Humidity
    Sensor (no local copy) and an unnamed A60 bulb.
- **Samsung aircons (3)**: probably SmartThings (cloud). Local options depend on the model number
  (climate_ip for older Wi-Fi units, ESPHome samsung_ac + RS485 for WindFree). Model number not yet given.
- **TCL TVs (3)** via Chromecast/DLNA: Living Room TCL TV at 192.168.68.101, Bedroom TV at 192.168.68.167. Cast can turn
  them off but not on (they drop off Wi-Fi in standby). **Android TV Remote** integration is now added for both
  (`remote.living_room_tcl_tv` / `media_player.living_room_tcl_tv_2`, `remote.bedroom_tv` / `media_player.bedroom_tv`).
  Living Room TV shows as three duplicate entities.
  - **Remotes** dashboard: TV remotes use Universal Remote Card 4.12.1 (HACS); old android-tv-card / tv-card
    resources removed. Keyboard buttons use live typing; scripts `script.living_room_tv_type_text` and
    `script.bedroom_tv_type_text` send text. Live typing needs **Enable IME** on (Android TV Remote → Configure).
- **Rain Bird** (6 zones, controller at 192.168.68.168, reserved in the Deco; all zones went unavailable on 2026-10-17 until the connection was fixed), **Ring** camera, **Baby Camera** motion sensor, garage door, ESPHome
  device `eyegas` at 192.168.68.115 (often unreachable).

## 6. Automations (UI-managed, not in git)

All automations point at Tuya Local entities (repointed 2026-10-18).

| Name | Entity | State | Notes |
| --- | --- | --- | --- |
| Wake up call | `automation.wake_up_call` | off | |
| Garage Light Activate | `automation.garage_light_activate` | on | Garage light 05:00–09:00 on weekdays |
| Garage Door Automation Light Swicth | `automation.garage_light_auto` | on | Garage light on for 10 min when the door opens |
| Garage Door Open/Close | `automation.new_automation_2` | on | Wall switch pulses the door; ignores unavailable/unknown (no opening on restarts) and has a loop guard |
| Garage Switch Follows Door | `automation.garage_switch_follows_door` | on | Keeps the garage wall switch ("Open Switch", gang 3) in step with the door |
| Water Tank Notification | `automation.water_tank_notification_2` | off | Uses `sensor.water_tenk_1_level` |
| Grow Lights Timer | `automation.grow_lights_timer` | on | Drives `switch.plant_fan_1` (Grow Domes) |
| Great Room bulbs auto power cycle | `automation.great_room_bulbs_auto_power_cycle` | on | See below |
| Main Bedroom bulbs auto power cycle | `automation.main_bedroom_bulbs_auto_power_cycle` | on | See below |
| Guest Bedroom bulb auto power cycle | `automation.guest_bedroom_bulb_auto_power_cycle` | on | See below |
| Tania's Office bulb auto power cycle | `automation.tania_s_office_bulb_auto_power_cycle` | on | See below |
| Update available - ask to install | `automation.update_available_ask_to_install` | on | Phone notification with Install / Not now; backs up first |

**Bulb auto power cycle:** when the room's wall switch turns on, watch its bulbs for up to 5 minutes and stop as
soon as all connect. Only if a bulb is still unavailable after 5 minutes **and the switch stayed on the whole
time** does it notify the JDK phone and power-cycle the switch once (10 s off), then wait 10 minutes. It never
turns on a switch the user turned off (reworked 2026-10-20 after it toggled lights needlessly with a 2-minute check). The Great Room switch feeds 23 bulbs,
including the kitchen pendants. Added after a second outage on 2026-10-18 (~10:15) where bulbs refused
connections (error 901) with no IP change until their power was cycled. Avoid using the Smart Life app on the
home Wi-Fi, which can lock bulbs out of local connections.

**Updates:** Home Assistant does not auto-install Core/OS updates; the notification above asks first.

Scripts: `script.living_room_tv_type_text`, `script.bedroom_tv_type_text` (send text to the TVs).

## 7. Open items

- Delete the dead helper `light.kitchen_grow_domes_socket_1` (no longer used).
- Reserve IPs in the Deco for all Tuya devices.
- TVs can't be turned on from HA (see section 5).
- Bulbs: the power cycle automations are a workaround; the root cause is wall switches cutting power to smart
  bulbs. Options offered 2026-10-20 (permanent supply with switches as buttons, recommended; leave switches on;
  or Zigbee/dumb bulbs). Waiting on the user in the "Fix Tuya Local bulbs" thread.
- Remotes: DStv Sport button doesn't open DStv (needs the DStv app ID) and the Keyboard doesn't type yet;
  waiting on the user in the "Remote card update and live typing" thread. Optional restyle of both remotes offered.
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

- 2026-10-21: Bulb power cycle automations reworked (5-minute watch, never re-enable a switch turned off); bulb root-cause options added; scripts listed.
- 2026-10-19: Alfred 0.7.0 (HACS and dashboard resource tools); Universal Remote Card 4.12.1 with live keyboard; Android TV Remote for both TVs; open remote issues and load-shedding area noted.
- 2026-10-18 (11:15): Ginny and all automations on Tuya Local; Tuya cloud mostly disabled; garage switch follows door; 4 bulb auto-power-cycle automations; update-available notification.
- 2026-10-18 (later): Alfred 0.6.0 (dashboard, rename and Tuya Local tools, auto IP follow); Main Dash fully on Tuya Local; 28 bulbs reconnected after IP changes; Tania office names.
- 2026-10-18: Rain Bird IP and fix, TV power-on cause, Grow Lights Timer failing on Tuya cloud, network/remote-access details, GitHub token regenerated.
- 2026-10-17: Hand-over doc created. Credentials collected; NAS mount command moved to secrets.yaml (config PR #2). Garage door no longer opens on HA/Tuya restarts.
