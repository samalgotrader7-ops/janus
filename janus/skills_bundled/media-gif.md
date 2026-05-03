---
name: media-gif
description: Search and embed GIFs from Giphy or Tenor for chat, docs, social posts.
state: quarantined
capabilities:
  web.fetch:
    - "https://api.giphy.com/*"
    - "https://tenor.googleapis.com/*"
    - "https://media*.giphy.com/*"
    - "https://media*.tenor.com/*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running media-gif.

You find a GIF that matches the user's intent (mood, topic, reaction)
and return a usable URL. Lightweight — no asset download unless asked.

Steps:
1. Detect the backend: `GIPHY_API_KEY` or `TENOR_API_KEY` in env. If
   neither, say so and stop.
2. Construct the search:
   - Giphy: `https://api.giphy.com/v1/gifs/search?api_key=<k>&q=<terms>&limit=5`
   - Tenor: `https://tenor.googleapis.com/v2/search?q=<terms>&key=<k>&limit=5`
3. Return 3-5 candidates: thumbnail URL, full URL, dimensions, title.
   Default to high-rated / SFW results. Show the user the candidates;
   they pick.
4. For embed: return both the markdown `![alt](url)` and the raw URL.
   Don't auto-pick "the best" GIF — taste is the user's call.

Never embed a GIF inline in a tool result that goes to a chat platform
without confirmation. Never use the GIF for an audience the user didn't
specify (e.g., don't post to Slack from this skill).
