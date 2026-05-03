---
name: demo-swarm
type: swarm
version: 1
description: Generic 2-phase demo for v1.4 runner tests; not a real-world spec.

budget:
  max_usd: 0.10
  max_wallclock_s: 60
  max_subagents: 5
  max_recursion_depth: 1
  max_total_tool_calls: 20
  max_completion_tokens_per_role: 200

inputs:
  count:
    type: int
    required: true
    min: 1
    max: 5
  label:
    type: string
    default: "demo"

output:
  format: json

permissions:
  default_mode: plan
  per_role:
    reporter: acceptEdits

phases:
  collect:
    pattern: map_reduce
    role: collector
    model: test-model-cheap
    tool_names:
      - add_one
    capabilities:
      math.compute:
        - "*"
    input_partition: per_item
    max_per_batch: 5
    aggregator: concat
  report:
    pattern: single
    role: reporter
    depends_on: collect
    model: test-model-strong
    aggregator: llm_summarize
    aggregator_args:
      template: "Summarize {n} collected values."
---

# Demo swarm body

You are the {role} sub-agent for the {spec_name} swarm.
The phase is {phase}. Your input is {input}.

If you are the collector, call add_one once and return your result.
If you are the reporter, summarize the inputs you receive.
