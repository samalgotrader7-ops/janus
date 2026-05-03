---
name: media-spotify
description: Spotify search, playlist management, playback control via Spotify Web API or MCP.
state: quarantined
capabilities:
  web.fetch:
    - "https://api.spotify.com/*"
    - "https://accounts.spotify.com/*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running media-spotify.

You interact with the user's Spotify account: search tracks, manage
playlists, control playback. Detect the backend at the start — MCP
spotify server preferred; raw web API as fallback (requires an OAuth
token).

Steps:
1. Detect: mcp_spotify_* tools in the registry, or `SPOTIFY_TOKEN` env
   var for raw API. If neither, say so and stop.
2. For SEARCH: track / album / artist / playlist scopes. Return top
   matches with names + ids; don't dump the full JSON payload.
3. For PLAYLIST CREATE: confirm name, public/private, collaborative,
   description. Add tracks in batches of 100 (API limit).
4. For PLAYLIST EDIT: read current contents first, propose the diff,
   confirm with the user before applying.
5. For PLAYBACK: control the active device. List devices first if
   ambiguous. Confirm volume/skip actions before sending — playback is
   a side effect on the user's environment.

Never delete a playlist or remove tracks without explicit confirmation —
mutations to user libraries are hard to reverse. Don't write to
collaborative playlists you don't own without confirmation.
