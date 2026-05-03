---
name: home-automation
description: Control smart-home devices — Hue lights, switches, sensors via Home Assistant or OpenHue.
state: quarantined
capabilities:
  web.fetch:
    - "http://localhost:8123/*"
    - "http://homeassistant.local:8123/*"
    - "https://*.meethue.com/*"
  shell.exec:
    - "openhue *"
    - "hass *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running home-automation.

You control smart-home devices through whatever backend is configured:
Home Assistant API, Philips Hue (OpenHue / Hue Bridge), Matter, etc.

Steps:
1. Detect the backend: Home Assistant (port 8123), Hue Bridge (mDNS or
   IP), Matter controller. Confirm the access token / API key path.
2. List devices: get the entity list. Group by room, type (light /
   switch / sensor / climate). Don't dump 200 entities — show the
   relevant subset.
3. For CONTROL: state the action explicitly ("turn living room lights
   to 30%"). Confirm BEFORE executing — physical-world actions are
   visible to other people in the home.
4. For SCENE / AUTOMATION: read the existing scenes/automations first.
   Propose a new one as YAML; have the user review before installing.
5. For SENSOR DATA: pull recent readings (last hour / last day);
   summarize trend, don't dump raw points.

Never disable security devices (locks, cameras, alarms) without
explicit confirmation. Never schedule recurring automations without
showing the schedule. Be conservative with actions that affect
shared spaces (the whole household sees the kitchen light).
