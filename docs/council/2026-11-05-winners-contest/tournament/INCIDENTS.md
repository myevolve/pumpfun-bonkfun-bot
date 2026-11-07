# Integrity incidents

## 1. Claude-CLI memory plugin shared context between fable and omp (rounds 5–9)

Found 2026-11-07 11:05Z while reviewing round 17's transcript audit; the hit itself was the plugin's banner.

**What happened.** fable and omp both run on the Claude CLI. A user-level memory plugin (claude-mem)
records session summaries and injects a project's memories at session start, keyed by the project path.
Until round 10 the contestant workspaces sat inside the polymarket-alpha-bot git checkout, so both
contestants' sessions were filed under one project, `polymarket-alpha-bot` (37 sessions, 732
observations). Each session could therefore start with summaries of the other contestant's work, including
work from earlier sessions in the same round, which continuation sessions would pick up.

**Measured exposure** (references to the other contestant's workspace in session-start injections):

| Round | omp's sessions mentioning fable's workspace | fable's sessions mentioning omp's workspace |
|---|---|---|
| 5 | 12 | 0 |
| 6 | 52 | 90 |
| 7 | 42 | 102 |
| 8 | 74 | 74 |
| 9 | 64 | 48 |
| 10–17 | 0 | 0 |

**Fixes.**
1. Round 10 (accidental): `GIT_CEILING_DIRECTORIES` isolated each workspace, giving each contestant its own
   memory project.
2. Round 18 (deliberate): the Claude CLI runs with `--setting-sources project,local --strict-mcp-config`,
   so no user plugins, hooks or MCP servers load. Tested: zero injections and login still works.

argon (Gemini CLI) and astra (Codex CLI) never used this store.

**Evidence of influence (inconclusive).** In rounds 6–8, eight omp claims share a market type and a price
band or time window with a fable claim from the same or an earlier round, several in the same round:
weather overnight favorites, 4-hour crypto morning carry, sports unknown-start carry and politics daytime
favorites. Both contestants are Claude models working from the same briefs, so similar reasoning is a
plausible alternative explanation.

**Effect on standings** (after round 17):

| Contestant | Current total | Counted | If fable's and omp's round 5–9 entries are voided | Counted |
|---|---|---|---|---|
| argon | 140 | 22 | 140 | 22 |
| astra | 60 | 5 | 60 | 5 |
| fable | 49 | 11 | 15 | 4 |
| omp | 14 | 5 | 4 | 2 |

The ranking order is the same under both options at this point.

**Ruling: the owner's.** The organizer's own model is one of the two affected contestants (omp), so the
organizer does not rule. Options: (a) keep the scores with this disclosure; (b) void fable's and omp's
round 5–9 claims and specs. Until the ruling, the standings are computed as before.
