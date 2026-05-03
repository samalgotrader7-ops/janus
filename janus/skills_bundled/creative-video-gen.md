---
name: creative-video-gen
description: Generate videos and motion graphics — Manim, ffmpeg compositing, video models.
state: quarantined
capabilities:
  shell.exec:
    - "manim *"
    - "ffmpeg *"
    - "ffprobe *"
  fs.write:
    - "**/*.mp4"
    - "**/*.mov"
    - "**/*.gif"
    - "**/*.py"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running creative-video-gen.

You produce short videos — explainer animations, motion graphics,
compositions from clips. Three modes: Manim (programmatic), ffmpeg
(compositing existing clips), or generative video model via API.

Steps:
1. Clarify intent: animation (Manim) / composition (ffmpeg) / generated
   (model). Each takes a different brief.
2. **MANIM**: write a Scene class. Start with a 5-second proof of
   concept before the full thing. Render at low fps/resolution first
   for iteration: `manim -ql script.py SceneName`. Final render
   at -qh once the design is locked.
3. **FFMPEG**: confirm input files, target resolution, codec, fps.
   Use `-c:v libx264 -preset slow -crf 18` for quality, `-preset ultrafast`
   for speed. Always preview a 5s clip before re-encoding the full thing.
4. **GENERATED**: detect the backend. State the model's known limits
   (max duration, resolution). Don't generate without explicit user
   confirmation — generation is slow and expensive.
5. Save with descriptive filename. Record the source script / ffmpeg
   command alongside so the result is reproducible.

Never re-encode the user's source video without keeping the original.
Never overwrite an output file without a versioned name. Generation
costs add up — budget the run before kicking it off.
