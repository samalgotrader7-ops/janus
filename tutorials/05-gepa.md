# Tutorial 05 — GEPA: Offline Skill Evolution

**Goal**: take a skill you've already used a few times, run GEPA on
it, read the proposed body, and accept the improvement. ~15 minutes.

**Prereq**: [Tutorial 02](02-your-first-skill.md) — you have at
least one skill that you've actually invoked from your CLI a handful
of times so the log has replay records.

---

## What GEPA is

GEPA is Janus's offline evolutionary engine for skill bodies. It
spawns a population of variant bodies, scores each by **LLM-judged
replay** against historical log records that invoked the skill, and
returns the best-fit variant with full provenance.

It's the missing piece in Janus's brand promise — "self-improving,
with a learning loop." Before v1.44:

- `skill_proposer.py` could **draft** new skills from observed
  patterns ✓
- Memory + skills persisted across sessions ✓
- `memory_prune` + `skill_prune` ran in the daemon background ✓
  (from v1.43)
- But **skills never improved** without you running `/skill review`
  yourself, and `/skill review` is just a single-shot LLM revision
  with y/N — no comparison of alternatives, no fitness signal.

GEPA closes that loop.

## How it works

```
baseline body
   │
   ▼
score baseline   ← LLM-judges body against N replay records
   │
   ▼
spawn population  → rewrite / simplify / specialize / crossover
   │
   ▼
score each variant
   │
   ▼
select top-K  ← (fitness DESC, body_length ASC — anti-bloat)
   │
   ▼
(repeat for G generations)
   │
   ▼
return best — recommendation = apply | no_change | no_signal
```

**P4 invariant:** GEPA never auto-applies the new body. The output
is a proposal artifact + a summary; you read the diff and decide.

## 1. Pick a target skill

You need a skill that's actually been used. Check `/skills`:

```
› /skills
- git-pr            trusted-auto         runs=12 ★★·
- py-refactor       trusted-supervised   runs=6  ★··
- data-explore      quarantined          runs=0  —
```

GEPA can run on `git-pr` (12 runs of history) but will return
`no_signal` for `data-explore` (no history to score against).

## 2. Run /skill gepa

```
› /skill gepa git-pr
running GEPA on 'git-pr' (pop=6, gen=3, records≤10)…
  baseline fitness=58.5
  gen=0 op=rewrite fitness=72.0 budget=183
  gen=0 op=simplify fitness=51.0 budget=173
  gen=0 op=specialize fitness=76.5 budget=163
  gen=0 op=crossover fitness=64.0 budget=153
  ...
  gen=0 survivors: g0_specialize_2=76.5, g0_rewrite_1=72.0, baseline=58.5
  gen=1 op=rewrite fitness=80.5 budget=143
  ...
```

The progress lines stream as the population evolves. With defaults
(pop=6, gen=3, records=10), a run is ~200 LLM calls and takes a few
minutes on cloud Ollama Turbo.

## 3. Read the result

```
GEPA run a1b2c3d4 on skill 'git-pr'
baseline fitness=58.5 best fitness=80.5 improvement=+22.0
config: pop=6 gen=3 records=10 calls_remaining=130
recommendation: apply

--- current body ---
1. Run git status
2. Stage relevant files
3. Open PR

--- proposed body ---
1. Run git status and review unstaged changes
2. Stage with explicit paths (avoid `git add .` which can include
   secrets)
3. Run git diff --cached to verify staged content
4. Open PR with HEREDOC body and a `Test plan:` section
...

full artifact: ~/.janus/skills/_gepa/git-pr/a1b2c3d4.json

apply this GEPA-proposed body? [y/N]:
```

`improvement` is in score-points (0..100 scale). The default promote
margin is **5.0** — anything below that gets `recommendation:
no_change` regardless of the diff.

## 4. Decide

If the diff looks correct, type `y`. Janus writes the new body
atomically (write-tmp + os.replace) and logs a `skill_gepa_applied`
audit record.

If you'd rather inspect more, the artifact file at
`~/.janus/skills/_gepa/<skill>/<run>.json` has:

- baseline body + per-record scores + rationale per score
- every variant from every generation, including the rejected ones
- selection trail (which IDs survived which generation)
- the recommendation gate decision

You can rerun GEPA with a different seed or higher population if
you want a wider search:

```
› /skill gepa git-pr
```

Each run gets a fresh run_id, so artifacts don't collide.

## 5. From outside the CLI (MCP)

GEPA is exposed via MCP as `janus_skill_gepa` for orchestrators
that drive Janus from Claude Code:

```jsonc
{
  "tool": "janus_skill_gepa",
  "input": {
    "skill": "git-pr",
    "generations": 4,
    "population": 8,
    "apply": false
  }
}
```

Returns the same text summary the CLI renders. Set `apply: true` to
have the MCP tool persist the new body when recommendation is
`apply` — useful for autonomous loops that should run their own
post-hoc validation before promotion.

## Cost guards

- `JANUS_GEPA_MAX_LLM_CALLS` (default **250**) — hard cap on total
  judge + mutate calls per run.
- `JANUS_GEPA_RECORDS_PER_RUN` (default **10**) — replay corpus cap.
- `JANUS_GEPA_PROMOTE_MARGIN` (default **5.0**) — improvement
  threshold before recommendation flips to `apply`.

A body-hash cache means identical variants don't re-judge — useful
when mutation operators occasionally regenerate the parent body.

## When GEPA says no_signal

You'll see `recommendation: no_signal` when:

- The skill doesn't exist on disk
- The skill has zero replay records in `~/.janus/log.jsonl`

The second is the common case for freshly-promoted skills. Run the
skill 3-5 times in real use first, then revisit GEPA.

## What GEPA does NOT do

- Auto-apply (P4): never. You're always the gate.
- Modify capabilities: GEPA evolves the prompt body only. Capability
  evolution is gated by the `evolve-capabilities` frontmatter flag
  on `/skill review`, not here.
- Promote between trust states (quarantined → trusted-*): still
  `/promote`.
- Mass-evolve every skill: deliberately. Run GEPA on one skill at a
  time so you can read each diff. (A `--all` flag is a future
  consideration if you find yourself running it many times.)
