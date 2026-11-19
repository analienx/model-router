# Haiku plan: exact Markdown format

**Purpose:** Opus decides *what and how*; Haiku follows a small, unambiguous
implementation plan and produces verifiable evidence. Use this document as the
copy/paste template whenever Opus delegates a bounded coding task to Haiku.

A Haiku plan is **one Markdown file per task**. Keep it short (normally one
screen to two screens), specific to real repository files, and executable in
the order written. No generic advice, open-ended research, or "figure it out"
steps. If the task needs an architectural decision, Opus makes that decision
**before** the handoff. Split a large task into separate plans.

## Required plan (copy this and replace every placeholder)

```markdown
# Haiku plan: <one concrete task>

## 1. Goal
<One sentence describing the observable behavior or exact change.>

## 2. Starting point
- Repo: <owner/repo or checked-out directory>
- Baseline: <full commit SHA or an explicitly identified version>
- Files to read: `path/to/file` (<function/section to inspect>)
- Confirmed facts: <what Opus verified in the code/tests>
- Decision already made: <exact algorithm, rule, or implementation approach>

## 3. Scope and invariants
- May edit: `path/to/target`, `path/to/test`
- Must NOT edit: <specific files/systems or "everything else">
- Must preserve: <existing behavior/API/security/compatibility constraints>

## 4. Do these steps, in order
1. <Exact change to a specific function/section; say what to add/remove.>
2. <Exact regression test or additional narrowly scoped change.>
3. <Any necessary cleanup, or omit this step.>

## 5. Proof of completion
- Before edit: `<command>` -> <expected result, if a real red test exists>
- After edit: `<command>` -> <required result>
- Regression suite: `<command>` -> <required result>
- Acceptance:
  - [ ] <observable acceptance criterion tied to one check>
  - [ ] <second criterion, if needed>

## 6. Stop and escalate
- Stop if <specific ambiguity, incompatible code, or missing prerequisite>.
- Stop on any edit outside the allowed files or changed baseline.
- On failed verification: fix only a local, in-scope mistake; at most
  <0-3> retry/retries. Then return evidence to Opus. Do NOT redesign.
- Independent review required: <yes/no and why>. If yes, do not self-approve.

## 7. Return to Opus
- Changed files and brief summary of each change.
- Exact commands run and relevant PASS/FAIL output (including skipped tests).
- Acceptance checklist with evidence, not a claimed result.
- Blockers, unexpected changes, remaining uncertainty, or "none".
- Final status: PASS / RETRY_LOCAL / ESCALATE / REVIEW_REQUIRED;
  use the runner's actual status when the executable contract is active.
```

### Rules Opus must follow when writing the plan

1. **Investigate first.** Read the actual files, reproduce the relevant
   behavior where feasible, and write *confirmed facts*, not guesses.
2. **Resolve decisions.** Specify the algorithm, condition, insertion point,
   and compatibility constraints. Haiku should implement, not discover the design.
3. **Bound the task.** Prefer one small change, exact allowed paths, and a
   finite retry budget. If the work expands, return it to Opus.
4. **Give real checks.** Name commands that exist in the target repo. Include a
   failing-before/passing-after regression when it has genuinely been established;
   otherwise write "not available" rather than fabricating red-test evidence.
5. **Demand evidence.** A narrative saying "done" is not test output. A risky
   change requires a genuinely separate reviewer.

## Example of a completed plan (illustrative)

This demonstrates the *level of specificity*. The change and regression below
are **hypothetical**: do not execute without confirming the baseline and tests.

```markdown
# Haiku plan: preserve the chosen model on continuation

## 1. Goal
A continuation prompt must retain the model preference selected earlier in
the same session, rather than silently resetting to the default.

## 2. Starting point
- Repo: analienx/model-router
- Baseline: <paste verified full SHA>
- Files to read: `router/judge_hook.py` (continuation handling);
  `tests/test_router.py` (continuation tests, if present)
- Confirmed facts: <insert verified details from current source>
- Decision already made: Keep the existing session preference authoritative
  for continuation messages; do not rerun the initial model-selection logic.

## 3. Scope and invariants
- May edit: `router/judge_hook.py`, `tests/test_router.py`
- Must NOT edit: all other files
- Must preserve: initial-message routing and explicit user model overrides

## 4. Do these steps, in order
1. In the continuation branch of `router/judge_hook.py`, reuse the saved
   session preference instead of applying the initial default.
2. Add a regression covering initial preference -> continuation -> same preference.
3. Make no unrelated routing changes.

## 5. Proof of completion
- Before edit: <exact command, only after Opus installs/verifies a failing test>
- After edit: <exact command for that test>
- Regression suite: `python -m unittest discover -s tests` -> exit 0
- Acceptance:
  - [ ] Continuation retains preference (targeted regression passes).
  - [ ] Existing routing tests pass.

## 6. Stop and escalate
- Stop if the session preference is not persisted or the expected branch differs.
- Stop on out-of-scope changes or stale baseline.
- One local retry after failed verification; otherwise escalate.
- Independent review required: no (reassess if auth/security behavior changes).

## 7. Return to Opus
Changed files, tests and outputs, acceptance evidence, uncertainties, and
final verified status. Do not claim PASS if checks were not run.
```

## Relationship to the executable JSON contract

This `.md` file is the **human handoff**: it tells Haiku exactly what to do.
It is not parsed by the handoff runner. For an **enforced** handoff, Opus also
creates the machine-readable JSON package described in
[HAIKU_HANDOFF.md](HAIKU_HANDOFF.md). Keep the baseline, allowed files, check
commands, retry count, and review requirement consistent in **both** files.

Only the JSON package, activated with `CLAUDE_ROUTER_HANDOFF`, enables the
actual scope hook and CLI evidence gate. A Markdown-only plan is useful
guidance but does not enforce file boundaries or automatically launch Haiku.
