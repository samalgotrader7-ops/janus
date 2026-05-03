---
name: formal-spec-translator
description: EXPERIMENTAL — translate natural language requirements into TLA+/Z3/Alloy and run the verifier.
state: quarantined
capabilities:
  fs.write:
    - "**/*.tla"
    - "**/*.cfg"
    - "**/*.als"
    - "**/*.smt2"
    - "**/*.py"
  shell.exec:
    - "tlc *"
    - "tlapm *"
    - "z3 *"
    - "alloy *"
  code.exec:
    - "python"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running formal-spec-translator (EXPERIMENTAL).

⚠ KNOWN LIMITATION: current frontier models (as of 2026) hit ~40-50%
on TLA+ generation in published benchmarks. Outputs from this skill
may be wrong in subtle ways. Treat the verifier's PASS/FAIL as the
ground truth, not the spec text. If you can't run the verifier, do
NOT trust the spec.

You translate natural-language requirements into a formal specification
(TLA+, Z3 SMT, Alloy) and run the verifier. Output is the spec + the
verification result + counterexamples.

Steps:
1. Pick the formalism that matches the requirement type:
   - **TLA+** — distributed systems, state machines, invariants
   - **Z3 SMT** — first-order logic, constraint satisfaction
   - **Alloy** — relational models, structural constraints
2. Translate the requirement into the chosen formalism. Be conservative:
   smaller specs are more likely to be correct. Don't try to formalize
   the whole system; pick the load-bearing invariant.
3. Run the verifier:
   - TLA+: `tlc Spec.tla` (model-check)
   - Z3: `z3 spec.smt2`
   - Alloy: GUI or `alloy.jar` CLI
4. Interpret the result:
   - PASS — spec satisfies the property; high confidence
   - FAIL — spec has a counterexample; show the counterexample,
     translate it back to the user's domain
   - TIMEOUT/UNDECIDED — the spec is too large or undecidable;
     suggest a smaller invariant
5. REPORT: the spec, the verifier output, the verdict, and a
   confidence note ("the spec compiled and ran; this does NOT mean
   the spec correctly captures the requirement — please review").

Don't ship a spec the verifier didn't accept. Don't claim "verified"
without actually running the verifier. Treat this skill's outputs as
research-grade, not production-ready.
