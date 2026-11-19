# Executable Haiku handoffs

This is an **opt-in execution contract**, not a second judge or autonomous agent. Opus
makes the correctness decisions, Haiku executes bounded edits, a Python runner
checks reproducible evidence, and an outside reviewer approves high-risk changes.
The feature is inactive unless `CLAUDE_ROUTER_HANDOFF` is set.

## Workflow

1. Opus creates a small JSON work package **outside** the target repository
   (the repo must be clean at preflight). Record a real full commit SHA, exact
   files and symbols investigated, constraints, ordered edits, and a minimal
   allowed-path list. Only the machine-checkable fields are consumed by the
   runner; the step-by-step implementation guidance stays in the accompanying
   human handoff.
2. Opus or the dispatcher runs `python router/handoff.py preflight PACKAGE.json --repo REPO`
   *before edits*. READY means baseline/file/scope checks and any red-first tests
   actually succeeded. A test with `expect: "failure"` must return nonzero.
3. Activate enforcement in the **Claude Code process environment**:
   `CLAUDE_ROUTER_HANDOFF=/absolute/path/PACKAGE.json` and, optionally,
   `CLAUDE_ROUTER_HANDOFF_REPO=/absolute/path/REPO`. Launch `claude-router`
   normally. The guard denies edits outside the package, including edits from
   otherwise unrestricted architect/implementer/deep-haiku agents.
   `CLAUDE_ROUTER_HANDOFF_STATE` may override the default state path.
4. Haiku performs the specified edits; independent runner:
   `python router/handoff.py verify PACKAGE.json --repo REPO`.
   PASS means checked. RETRY_LOCAL permits a limited local correction.
   ESCALATE means stop and return evidence to Opus. REVIEW_REQUIRED is *not*
   an accepted implementation.
5. For a high-risk task, a **different reviewer** inspects the diff and
   prepares a review evidence JSON. Finish with
   `python router/handoff.py review PACKAGE.json REVIEW.json --repo REPO`.
   The runner checks that the reviewed change fingerprint is still current,
   that all acceptance criteria were addressed, and **reruns** the verification
   commands independently. Only PASS_REVIEWED is an accepted high-risk result.
   The evidence file is an *attestation*, not proof of reviewer identity; a
   supervisor must actually assign a distinct reviewer.
6. Inspect cumulative measured results with `python router/handoff.py report`.
   The JSONL is at `~/.claude-router/handoff-metrics.jsonl` (override with
   `CLAUDE_ROUTER_METRICS`). It records outcome, retry count, duration, and
   check counts, **not invented model spend**. Billing cost must be joined from
   provider usage separately before making cost-improvement claims.

## Example package

Replace `baseline_commit` with the actual verified `git rev-parse HEAD`
for a clean checkout. All commands run without a shell, using argv arrays.
This is an illustrative package, not a ready-to-run claim.

```json
{
  "version": 1,
  "task_id": "continue-preserves-haiku",
  "baseline_commit": "0123456789abcdef0123456789abcdef01234567",
  "verified_files": ["router/judge_hook.py", "tests/test_router.py"],
  "allowed_paths": ["router/judge_hook.py", "tests/test_router.py"],
  "risk": "medium",
  "max_local_retries": 1,
  "preflight_checks": [
    {
      "id": "red-continuation",
      "command": ["python", "-m", "unittest", "tests.test_router.ContinuationRegression"],
      "expect": "failure"
    }
  ],
  "verification_checks": [
    {
      "id": "green-continuation",
      "command": ["python", "-m", "unittest", "tests.test_router.ContinuationRegression"]
    },
    {
      "id": "unit-suite",
      "command": ["python", "-m", "unittest", "discover", "-s", "tests"]
    }
  ],
  "acceptance": [
    {"id": "AC1-continue-retains-preference", "checks": ["green-continuation"]},
    {"id": "AC2-preserve-other-routing", "checks": ["unit-suite"]}
  ]
}
```

Important: The example assumes a **regression test has already been installed**
on the verified baseline and fails there. If it does not yet exist, Opus must
arrange the red test separately; do not fake a failed test or list a nonexistent
test command. Evidence returned by preflight is authoritative.

## Review evidence

For a REVIEW_REQUIRED result, use the exact `change_digest` and
`acceptance` map emitted by verification. After the external review:

```json
{
  "task_id": "continue-preserves-haiku",
  "reviewer": "independent-opus-review",
  "decision": "approved",
  "change_digest": "<digest reported by verify>",
  "acceptance": {
    "AC1-continue-retains-preference": true,
    "AC2-preserve-other-routing": true
  },
  "findings": []
}
```

An unresolved finding or `changes_requested` blocks acceptance. Any diff
change after verification invalidates the review and requires a new
preflight/verification cycle (or a new baseline package if the commit changed).

## Actual enforcement and limits

- File edits via Claude Code Edit, Write, MultiEdit, NotebookEdit go through the
  scope hook **only when the router is active and a package is activated**.
  The hook verifies active preflight state and rejects changed packages.
- Verification uses git status (tracked plus untracked) and allowed-path checks,
  then runs acceptance commands. Out-of-scope edits stop before checks run.
- A verification failure gets at most `max_local_retries` local retries (0–3);
  a scope or baseline mismatch escalates immediately.
- A high-risk package always requires external review; `review_required: true`
  can demand that even for lower risks. The reviewer attestation **cannot
  authenticate its author**. The supervisor must ensure a truly separate review.
- **Bash file writes and modifications of gitignored files are not blocked by
  the PreToolUse edit hook**. This is a correctness gate and audit, not an OS
  sandbox. For adversarial executors use actual filesystem isolation and
  Claude Code permission restrictions.
- The runner does not launch, schedule, select, or pay for a model. It checks
  the artifact and produces evidence; an orchestration layer must issue the
  delegated prompts. Plain `claude` is unaffected.
- A passing test suite is evidence for specified checks, not a proof of all
  behavior. The acceptance mapping must be designed by Opus and reviewed.

## Benchmark protocol

Use a representative set of real repository tasks, fix each starting commit and
acceptance commands, and compare the old vs new handoff under similar conditions.
Measure first-pass acceptance, retries, outside-scope edits, escalations,
verified time, regressions found by independent review, and provider-reported
total spend. Don't count REVIEW_REQUIRED as complete, or confuse rerunning tests
with proof that a separate reviewer examined the diff.
