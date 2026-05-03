---
name: interpretability-probe
description: EXPERIMENTAL — for models exposing logprobs/activations, probe WHY a token was chosen.
state: quarantined
capabilities:
  web.fetch:
    - "https://api.openai.com/*"
    - "https://api.anthropic.com/*"
    - "https://openrouter.ai/api/v1/*"
    - "http://localhost:*"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running interpretability-probe (EXPERIMENTAL).

⚠ KNOWN LIMITATION: most production model APIs do NOT expose
logprobs, attention weights, or activations. This skill is meaningful
only when:
- a self-hosted model (llama.cpp, vLLM, TGI) exposing logprobs is
  configured, OR
- a provider API explicitly returns logprobs (some OpenAI endpoints,
  some Together.ai endpoints)

If neither is available, refuse politely and explain. Do not
fabricate "interpretability" results from a model that doesn't
expose them.

You probe a model's internals to answer "why did the model choose
this token?" Useful for debugging surprising outputs, evaluating
calibration, and understanding refusals.

Steps:
1. CHECK BACKEND. Does the configured model + endpoint expose
   logprobs? Test with a tiny request that asks for logprobs in the
   response. If the response doesn't include them, stop here.
2. CONSTRUCT the probe: take the user's question, hit the API with
   `logprobs=true, top_logprobs=10` (or equivalent for the backend).
3. ANALYZE:
   - At each token, what was the chosen token's probability vs the
     runner-up?
   - Where in the response did probability collapse (model became
     confident)?
   - Where was probability spread (model was uncertain)?
   - Were there any surprising runner-ups?
4. INTERPRET (carefully):
   - high prob throughout → model was confident
   - prob drop at a specific token → that's where the model committed
     to a path; alternative paths were considered
   - high entropy throughout → model is uncertain, treat output as
     low-confidence
5. REPORT a per-token annotation. NEVER claim mechanistic
   understanding ("the model thinks X") — these probes show
   probabilities, not reasoning.

Don't extrapolate beyond what logprobs measure. Don't run probes on
production model calls without budget approval — `top_logprobs=10`
multiplies token cost. Read-only on the model.
