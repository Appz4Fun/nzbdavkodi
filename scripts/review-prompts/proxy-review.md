# Proxy Review Prompt

Reusable template for an independent code-reviewer pass over any proxy plan doc in this repo. Works for:

- a specific `plans/REMAINING_*.md` one-pager before starting that work
- a `plans/PROXY_EPIC_*.md` stub before kicking off the epic
- a proposed major edit to `TODO.md` or `DONE.md` (supply the diff as the "plan under review")

---

## When to run

- Before starting a non-trivial piece of work whose plan is new or materially changed.
- After folding reviewer feedback into a plan, to confirm the integration is internally consistent.
- NOT needed for routine checkbox updates, progress notes, or typos.

---

## What to paste into the reviewer

1. `TODO.md` — active worklist + file map (context)
2. `DONE.md` — completed-work archive (so the reviewer knows what NOT to relitigate)
3. The **plan under review** — one of:
   - a `plans/REMAINING_*.md` one-pager
   - a `plans/PROXY_EPIC_*.md` stub
   - a diff of proposed edits to `TODO.md` or `DONE.md`
4. Source under review — any files cited by `file:line` in the plan.

Fill in the `<< plan under review >>` marker in the prompt so the model knows which doc is the target vs. which are context.

---

## Prompt

```
You are a senior code reviewer running an independent pass on the nzbdav
Kodi proxy plan. Plans already implemented and verified live in DONE.md —
do NOT relitigate them unless you find a concrete error.

Files provided:
- TODO.md — active worklist + file map (context only)
- DONE.md — completed-work archive (context only)
- << plan under review >> — the single document this pass evaluates
- source under review — any files cited by file:line in the plan

Your output has two parts.

PART A — Consistency check.

Verify the plan under review is internally consistent AND consistent with
TODO.md and DONE.md:
- Every action item in the plan has an owner slot (or explicit
  "_unassigned_") and an entry criterion.
- Every feature flag mentioned has a default value AND an enablement
  condition.
- Every acceptance gate is testable (maps to a listed test, a CoreELEC
  smoke check, a CI gate, or a concrete log/output assertion).
- The plan does not duplicate or contradict work already in DONE.md.
- The plan's `Depends` edges resolve to items that exist (in TODO.md active
  worklist, in another plan, or in DONE.md).

Report each inconsistency as one bullet. No bullet = consistent.

PART B — New findings.

Report anything NEW this pass surfaces that the plan misses. Candidates:
- Source-code hotspots not cited in the plan but touched by its work
- Dependency loops or ordering errors
- Feature-flag interactions (e.g., two flags that conflict under edge
  conditions)
- Test coverage gaps (especially for multi-valued settings)
- Settings-schema concerns (name collisions, missing clamps)
- Rollback-path holes (a change that cannot be cleanly rolled back by flag
  flip or revert)
- Cross-subsystem dependencies not flagged in Entry criteria

For each new finding, give a file:line reference only if you can verify it
against the source provided. If you cannot verify a reference, say so
explicitly — do not fabricate.

Format:
- Markdown.
- Part A: numbered list of inconsistencies (or the single line "consistent").
- Part B: bulleted list of findings.
- Length cap: 400 lines.
- No prose intro, no summary at the end.

Where your output goes:
- If the plan under review has an explicit landing zone (a "Codex review"
  section or a `## Review` placeholder), append there.
- Otherwise, return your output inline for the user to paste.
- Do NOT append review output to DONE.md — DONE.md is an immutable archive.
```
