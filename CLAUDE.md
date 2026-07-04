@AGENTS.md

## Model routing policy (Claude-specific)

Applies to subagent spawns: the `Agent` tool `model` param and Workflow `opts.model`.
Default: omit the override so agents inherit the session model. Deviate only per
the tiers below. If the session model is mismatched to the task tier, say so and
suggest `/model` — never silently proceed on a mismatch you noticed.

- **fable** (judgment-heavy, high cost of being wrong): architecture/decomposition
  decisions; identity/dedup/sort contracts (pubdate, dupe keys, name+size matching);
  fallback-tier and liveness/terminal-state logic; timing-flake debugging;
  security triage (real vs FP); workflow verify/judge/synthesis stages.
- **opus** (default for substantive code): implementing features/fixes with a
  decided design; review finder passes; pattern-following multi-file refactors;
  nontrivial tests.
- **sonnet** (mechanical, low ambiguity, high volume): exploration/search fan-out;
  mechanical migrations (renames, applying a decided pattern across files);
  summarizing files/logs/PR comments; test scaffolding from an explicit spec.
- **haiku** (bulk trivial fan-out): per-file classify/grep-and-report; formatting checks.

Workflow stages: finders/sweepers → sonnet (opus if the code is subtle);
verify/judge/synthesize → fable. Never run a >5-agent stage on fable unless it
is a verify/judge stage. `log()` non-default routing choices.

**Prefer agents as the routing mechanism.** The session model cannot be switched
mid-run, so in autonomous/bypass-permissions runs, delegation is the only way to
apply this policy: when a chunk of work is self-contained enough to hand off
(exploration, migrations, review passes, per-file sweeps, verification), spawn an
agent with an explicit `model` from the tiers above rather than doing it inline.
Keep inline only the work that depends on accumulated conversation context or is
too small to be worth a handoff — a delegated task must carry its full context in
the prompt. Orchestration, judgment calls, and final synthesis stay in the main
loop on the session model.
