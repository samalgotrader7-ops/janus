---
name: confidence-self-report
description: EXPERIMENTAL — model verbalizes per-claim confidence; this is STATED confidence, not calibrated.
state: quarantined
capabilities: 
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running confidence-self-report (EXPERIMENTAL).

⚠ KNOWN LIMITATION: this skill produces STATED confidence — the
model's verbalized estimate of how confident it is in each claim.
This is NOT calibrated probability. Models are systematically
miscalibrated (over-confident on familiar topics, sometimes
under-confident on edges they actually know). Treat the numbers as
heuristics, not probabilities.

To produce calibrated confidence you would need logprobs or a
calibration layer trained on the model's outcomes — neither exists
in default Janus. See interpretability-probe for the logprobs case.

You annotate the assistant's response with per-claim confidence so
the user can see where the model is uncertain.

Steps:
1. IDENTIFY the discrete claims in the response. A claim is a
   verifiable assertion ("X is the capital of Y", "this function
   returns Z", "the file at path P contains pattern Q"). Filler text
   ("let me explain") is not a claim.
2. For each claim, the model self-rates:
   - HIGH (90%+): I am sure; an authoritative source agrees
   - MEDIUM (60-89%): I think this; I have seen it but couldn't cite
   - LOW (<60%): I'm guessing; treat as a hypothesis
3. ATTACH the rating inline near each claim (footnote-style or
   parenthetical). Don't disrupt the prose; the rating is the
   meta-layer.
4. For LOW claims, surface the hedging explicitly to the user and
   suggest a verification step (a command, a file to read, a query).
5. NEVER round LOW claims up to MEDIUM to look more authoritative.
   The whole point is honest signaling.

Read-only. The skill's output is annotation. Don't modify the
response itself; add the confidence layer alongside it. Honest
hedging > confident wrong.
