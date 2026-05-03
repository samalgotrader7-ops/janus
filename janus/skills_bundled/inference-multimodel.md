---
name: inference-multimodel
description: Orchestrate inference.sh-style multi-provider AI tools — image, video, LLM via one CLI.
state: quarantined
capabilities:
  shell.exec:
    - "infsh *"
  web.fetch:
    - "https://api.inference.sh/*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running inference-multimodel.

You access a wide catalog of AI applications (image gen, video gen,
LLMs, search, audio, 3D) through a unified API like inference.sh,
Replicate, or fal.ai — one auth token, many providers.

Steps:
1. Detect the backend: `which infsh`, or env vars `INFERENCE_SH_KEY`,
   `REPLICATE_API_TOKEN`, `FAL_KEY`. If none configured, say so and stop.
2. List available apps: `infsh list` (or the equivalent for the chosen
   provider). Filter by category if the user has an intent (e.g., image
   gen, video, audio).
3. For RUN: confirm the app, the inputs, the cost estimate (these
   platforms bill per-call). Show the user the JSON payload BEFORE
   submitting.
4. Capture the result: download the artifact (image / audio / video /
   text), save to a descriptive filename, record the input prompt and
   model id alongside.
5. For BATCH: run sequentially with rate-limit awareness. Don't
   parallelize without an explicit budget.

Never run a paid call without a cost estimate confirmed by the user.
Never auto-iterate (re-running with tweaks until "good enough") — each
iteration is a charge. Cap to one run per request unless the user
explicitly authorizes more.
