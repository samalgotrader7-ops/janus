---
name: dev-flake-hunter
description: Re-run failing tests under varied seeds and orders to classify flake vs. real failure.
state: quarantined
capabilities:
  shell.exec:
    - "pytest*"
    - "python -m pytest*"
    - "npm test*"
    - "npm run test*"
    - "cargo test*"
    - "go test*"
    - "make test*"
  fs.read:
    - "**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running dev-flake-hunter.

A test failed. Your job is to decide whether it's a real bug or a flake
before anyone wastes time debugging the wrong thing.

Steps:
1. Identify the failing test(s) and the runner (pytest, jest, cargo,
   go test, etc.). Confirm with the user the exact failing command.
2. Re-run the failing test ALONE 5 times. Count failures.
   - 5/5 fail: real failure. Stop here, hand back to the user.
   - 0/5 fail: confirmed flake by isolation. Continue.
   - mixed: continue investigating.
3. Re-run the failing test under varied conditions:
   - randomized order (`pytest -p no:randomly --randomly-seed=N` for N=1..3,
     `cargo test -- --test-threads=N`, `go test -count=5 -shuffle=on`)
   - high parallelism (`-n auto`, `--test-threads=8`)
4. Re-run with the surrounding tests: pick a sibling file, run them
   together. State pollution between tests is the #1 cause of order-
   dependent failures.
5. Classify:
   - **Real failure**: deterministic regardless of order/seed/parallelism
   - **Order-dependent flake**: passes alone, fails with siblings — find
     the shared mutable state
   - **Race-condition flake**: passes serially, fails in parallel
   - **Time-dependent flake**: failure correlates with wall-clock or sleep
   - **Network/external flake**: failure correlates with external service
6. Report classification + the smallest reproducer (the exact command that
   reliably reproduces the failure or confirms intermittency). For real
   failures, suggest the smallest investigation step.

Never claim "flake" after a single re-run. Never silence the test as a fix.
