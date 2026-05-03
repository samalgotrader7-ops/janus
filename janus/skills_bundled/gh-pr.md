---
name: gh-pr
description: Open a pull request from the current branch with a useful title and body.
state: quarantined
capabilities:
  shell.exec:
    - "git status*"
    - "git log*"
    - "git diff*"
    - "git branch*"
    - "git push*"
    - "gh pr *"
    - "gh repo view*"
  fs.read:
    - "**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running gh-pr.

You open a GitHub pull request from the current local branch. Title is short
(<70 chars). Body explains the WHY and includes a test plan.

Steps:
1. `git status` — confirm a clean working tree and a non-default branch.
2. `git log <base>..HEAD` and `git diff <base>...HEAD` — read every commit
   and the cumulative diff. Do not assume the latest commit summarizes the
   PR; the PR is the union of all commits since branching.
3. Draft title (imperative mood, no period) and body (Summary + Test plan).
4. If the branch is not pushed, `git push -u origin <branch>`.
5. `gh pr create --title "..." --body "..."` — pass the body via heredoc to
   preserve formatting. Capture and report the PR URL.

Never `--force` push. Never use `git add -A` here — the working tree should
already be clean (step 1 verifies). If draft mode is preferred, pass `--draft`.
