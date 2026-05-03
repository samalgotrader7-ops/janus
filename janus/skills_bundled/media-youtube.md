---
name: media-youtube
description: Fetch YouTube transcripts and metadata, summarize talks, find timestamps for key claims.
state: quarantined
capabilities:
  web.fetch:
    - "https://*.youtube.com/*"
    - "https://youtu.be/*"
    - "https://*.googlevideo.com/*"
  shell.exec:
    - "yt-dlp *"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running media-youtube.

You work with YouTube content: transcripts, metadata, summaries,
timestamp-anchored quotes. Audio/video download is opt-in only because
it's bandwidth-heavy and platform-policy sensitive.

Steps:
1. From a YouTube URL or video id, fetch the transcript via yt-dlp:
   `yt-dlp --write-auto-sub --sub-lang en --skip-download --output "%(id)s" <url>`.
   Falls back to manual subs if auto isn't available.
2. Pull metadata: title, author, duration, upload date, view count.
3. For SUMMARIZE: chunk the transcript by speaker/topic shifts (paragraphs
   in the auto-transcript), summarize each chunk, then synthesize.
4. For TIMESTAMPS: when the user wants "where did they say X", grep the
   transcript for X, return the surrounding context with the timestamp
   formatted as `https://youtu.be/<id>?t=<seconds>`.
5. For DOWNLOAD: confirm with the user, confirm the destination, confirm
   you have the right (own content, fair use). yt-dlp can download but
   the user owns the policy decision.

Never claim a quote that isn't in the transcript. Never download a video
without explicit user confirmation. Don't paste full transcripts — they're
long and noisy; summarize or timestamp-link instead.
