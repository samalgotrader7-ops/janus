---
name: sandbox-spawner
description: Spin up a Docker container or git worktree for an isolated experiment; run; capture; tear down.
state: quarantined
capabilities:
  shell.exec:
    - "docker *"
    - "git worktree *"
    - "podman *"
  fs.read:
    - "**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running sandbox-spawner.

The user wants to "try X" without polluting the main workspace. You
spin up an isolated environment (Docker container or git worktree),
run the experiment, capture the result, and tear down.

Steps:
1. PICK the isolation level:
   - **git worktree**: same repo, different branch + filesystem path.
     Cheap, fast. Good for "try this refactor in parallel" or
     "experiment with a new file layout".
   - **Docker container**: full process isolation. Good for "try this
     install command" or "run untrusted code". Needs a base image —
     default to `python:3.12-slim`, `node:20-slim`, `ubuntu:24.04`,
     match the task.
   - **podman / containerd**: same shape as Docker; pick whichever
     the env has installed (use environment-probe).
2. SPAWN. For worktrees: `git worktree add ../<branch>-experiment <branch>`.
   For containers: `docker run --rm -it -v <pwd>:/workspace -w /workspace
   <image> <cmd>`. Bind-mount only what the experiment needs.
3. EXECUTE the experiment. Capture stdout, stderr, exit code, files
   created. Time-bound it — pass `timeout` for safety.
4. REPORT outcomes back to the main session. Diff the worktree against
   the source branch. Diff the container's filesystem against its
   base image (selectively — don't dump /usr).
5. TEAR DOWN: `git worktree remove --force <path>` or `docker rm -f`.
   Confirm cleanup before claiming "done".

Never bind-mount sensitive directories (~/.ssh, ~/.aws, ~/.janus)
into a sandbox without explicit confirmation. Never run a sandbox
with `--privileged`. Tear down on success AND failure — orphan
sandboxes accumulate fast.
