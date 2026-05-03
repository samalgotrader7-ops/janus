---
name: multi-model-jury
description: Ask N different models the same question; return majority answer + dissents preserved.
state: quarantined
capabilities:
  web.fetch:
    - "https://openrouter.ai/api/v1/*"
    - "https://api.openai.com/*"
    - "https://api.anthropic.com/*"
    - "https://generativelanguage.googleapis.com/*"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running multi-model-jury.

You ask N different models the same question, return the consensus
answer, and preserve dissents. Janus is model-agnostic (P6) and
typically routes through OpenRouter, so this is structurally cheap.

Steps:
1. CLARIFY the question. The jury is most useful for high-stakes calls
   where correctness > latency: a yes/no decision, a recommendation, a
   factual claim, a code-review verdict. NOT useful for open-ended
   creative tasks (you'd average toward bland).
2. PICK the panel — typically 3-5 models from different families
   (e.g., Opus, GPT-5, Gemini 3, Llama-3.3-70B, Grok-4). Diversity
   matters more than count. Confirm cost estimate with the user
   before calling — running a panel is O(N) the cost of one call.
3. RUN in parallel. Same prompt, same temperature (low: 0.0–0.3),
   same system message. Capture each model's response.
4. SCORE. For yes/no: count votes. For recommendations: cluster by
   semantic similarity. For factual claims: weight by stated
   confidence + cross-check against any tool result.
5. REPORT:
   - majority answer (with vote count: "4 of 5 agree")
   - dissenting models + their reasoning (verbatim, not summarized)
   - your judgement: when is the dissent worth attention? (a single
     dissenter often spots what the majority missed)

Never silently drop a dissent — that's the value the panel adds.
Never claim consensus you didn't compute. Be transparent about cost:
a 5-model panel can be 20-30× the cost of a single model call.
