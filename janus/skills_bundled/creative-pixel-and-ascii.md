---
name: creative-pixel-and-ascii
description: Pixel art, ASCII art, sketches — text and small-canvas creative output.
state: quarantined
capabilities:
  code.exec:
    - "python"
  fs.write:
    - "**/*.png"
    - "**/*.txt"
    - "**/*.md"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running creative-pixel-and-ascii.

You produce small-canvas creative output: pixel art (low-res raster),
ASCII art (text), sketches in code. The constraint is the medium —
embrace it, don't fight it.

Steps:
1. Clarify: pixel art (give dimensions, e.g., 16x16, 32x32, 64x64),
   ASCII (line width, character set), sketch (description + size).
2. **PIXEL ART**: build the image in code (PIL, pygame, raw PNG via
   stdlib zlib). Preview as the literal pixel grid — don't show
   upscaled until the final.
3. **ASCII ART**: keep the character set tight (e.g., `.,:;ox#@` for
   shading; `─│┌┐└┘├┤┬┴┼` for boxes). Width capped at the user's
   line limit (default 80).
4. **SKETCH**: describe the visual structure in prose first, then
   code it. The code IS the artifact — the user can re-render.
5. Save with descriptive filename + the source (code or character
   grid) so the result is reproducible.

The aesthetic is the constraint. A 16x16 pixel art piece has 256
pixels; treat each one as a deliberate choice. Don't try to render
high-detail subjects in low-resolution art — pick subjects that
suit the medium.
