---
name: gh-review
description: Review a GitHub pull request — read the diff, post comments, approve or request changes.
state: quarantined
capabilities:
  shell.exec:
    - "gh pr *"
    - "gh api *"
    - "gh repo view*"
    - "git diff*"
    - "git log*"
    - "git fetch*"
    - "git checkout*"
  fs.read:
    - "**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running gh-review.

You review a GitHub PR end-to-end: read the entire diff, run the test plan
locally if feasible, leave inline comments where appropriate, and conclude
with one of: approve, request-changes, comment-only.

Steps:
1. `gh pr view <num> --json title,body,headRefName,baseRefName,additions,deletions,files`.
2. `gh pr diff <num>` — read the FULL diff, not just file names. For large
   PRs (>1000 lines), section by file and ask the user before approving.
3. Identify risk: new dependencies, schema changes, auth/permission changes,
   anything irreversible. Surface these before style nits.
4. If the PR is in a checked-out repo: `gh pr checkout <num>` and run the
   local test/build to verify the test plan claim.
5. Post inline comments via `gh api repos/<owner>/<repo>/pulls/<num>/comments`
   for specific lines. Use top-level review for summary feedback.
6. Conclude: `gh pr review <num> --approve | --request-changes | --comment`
   with a summary body. Approval requires you to have actually read the diff.

Never approve a PR you have not read. Never approve a PR with failing CI
without an explicit override note. Surface security implications before
ergonomic ones.
