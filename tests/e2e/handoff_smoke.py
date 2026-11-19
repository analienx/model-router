#!/usr/bin/env python3
"""Cross-platform CLI-level proof of the opt-in Haiku handoff execution contract.

Exercises the actual handoff CLI, a real temporary git repository, the Claude
Code PreToolUse hook entry point, checks, escalation, and review. No LLM calls,
network credentials, private machines, or mock acceptance reports are needed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "router" / "handoff.py"
GUARD = ROOT / "router" / "guard_hook.py"


def command(argv, *, cwd=None, env=None, input_text=None):
    return subprocess.run(
        argv, cwd=str(cwd) if cwd else None, env=env,
        input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", timeout=45,
    )


def ensure(condition, reason):
    if not condition:
        raise AssertionError(reason)


def git(repo, *args):
    result = command(["git", "-C", str(repo)] + list(args))
    ensure(result.returncode == 0, "git %r: %s" % (args, result.stderr))
    return result.stdout.strip()


def make_repo(root, name):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "ci@example.invalid")
    git(repo, "config", "user.name", "GitHub Actions Handoff")
    (repo / "feature.py").write_text("old = True\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    return repo, git(repo, "rev-parse", "HEAD")


def make_package(path, task_id, commit, risk="medium", failing=False):
    red = "from pathlib import Path; assert Path('feature.py').read_text() == 'new = True\\n'"
    green = red if not failing else "import sys; sys.exit(5)"
    package = {
        "version": 1,
        "task_id": task_id,
        "baseline_commit": commit,
        "verified_files": ["feature.py"],
        "allowed_paths": ["feature.py"],
        "risk": risk,
        "max_local_retries": 1,
        "preflight_checks": [{"id": "red-before", "command": [sys.executable, "-c", red],
                              "expect": "failure"}],
        "verification_checks": [{"id": "green-after", "command": [sys.executable, "-c", green]}],
        "acceptance": [{"id": "AC1", "checks": ["green-after"]}],
    }
    path.write_text(json.dumps(package), encoding="utf-8")
    return path


def cli(env, action, package=None, repo=None, state=None, extra=None, expected=0):
    argv = [sys.executable, str(RUNNER), action]
    if package is not None:
        argv.append(str(package))
    if extra is not None:
        argv.append(str(extra))
    if repo is not None:
        argv += ["--repo", str(repo)]
    if state is not None:
        argv += ["--state", str(state)]
    result = command(argv, env=env)
    ensure(result.returncode == expected,
           "%s exit %s != %s; stdout=%s stderr=%s" % (
               action, result.returncode, expected, result.stdout, result.stderr))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AssertionError("%s emitted non-JSON: %s" % (action, result.stdout))


def hook(env, package, repo, state, filename):
    e = dict(env)
    e.update({
        "CLAUDE_ROUTER": "1",
        "CLAUDE_ROUTER_HANDOFF": str(package),
        "CLAUDE_ROUTER_HANDOFF_REPO": str(repo),
        "CLAUDE_ROUTER_HANDOFF_STATE": str(state),
    })
    payload = {"session_id": "handoff-runner-proof", "cwd": str(repo),
               "tool_name": "Edit", "agent_type": "architect",
               "tool_input": {"file_path": str(repo / filename),
                              "old_string": "old", "new_string": "new"}}
    result = command([sys.executable, str(GUARD)], env=e, input_text=json.dumps(payload))
    ensure(result.returncode == 0, "guard crashed: %s" % result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="Path to the generated CI evidence JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="haiku-runner-") as td:
        home = Path(td)
        env = dict(os.environ)
        env.update({
            "CLAUDE_ROUTER_HOME": str(home / "router-home"),
            "CLAUDE_ROUTER_METRICS": str(home / "metrics.jsonl"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        evidence = {"platform": sys.platform, "python": sys.version.split()[0], "cases": {}}

        # Case A: regression red on baseline; hook scope; verified acceptance.
        repo, commit = make_repo(home, "normal")
        pkg = make_package(home / "normal.json", "normal", commit)
        state = home / "normal-state.json"
        ready = cli(env, "preflight", pkg, repo, state)
        ensure(ready["status"] == "READY" and ready["preflight"][0]["passed"],
               "regression-first preflight was not actually red")
        allowed = hook(env, pkg, repo, state, "feature.py")
        denied = hook(env, pkg, repo, state, "outside.py")
        ensure(allowed == {}, "allowed edit unexpectedly denied")
        ensure(denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
               "out-of-scope edit not denied for the otherwise exempt architect")
        (repo / "feature.py").write_text("new = True\n", encoding="utf-8")
        done = cli(env, "verify", pkg, repo, state)
        ensure(done["status"] == "PASS" and done["acceptance"]["AC1"],
               "normal acceptance failed: %r" % done)
        evidence["cases"]["normal"] = {
            "preflight": ready["status"], "scope_guard": "deny_outside",
            "status": done["status"], "change_digest": done["change_digest"],
        }

        # Case B: high risk cannot self-approve; review is tied to exact bytes.
        repo, commit = make_repo(home, "high-risk")
        pkg = make_package(home / "high-risk.json", "high-risk", commit, "high")
        state = home / "high-state.json"
        cli(env, "preflight", pkg, repo, state)
        (repo / "feature.py").write_text("new = True\n", encoding="utf-8")
        pending = cli(env, "verify", pkg, repo, state)
        ensure(pending["status"] == "REVIEW_REQUIRED", "high risk bypassed review")
        review_path = home / "review.json"
        review_path.write_text(json.dumps({
            "task_id": "high-risk", "reviewer": "separate-reviewer-in-ci",
            "decision": "approved", "change_digest": pending["change_digest"],
            "acceptance": pending["acceptance"], "findings": [],
        }), encoding="utf-8")
        (repo / "feature.py").write_text("tampered = True\n", encoding="utf-8")
        stale = cli(env, "review", pkg, repo, state, review_path, expected=2)
        ensure(stale["status"] == "ESCALATE" and "worktree changed" in stale["error"],
               "stale review was accepted")
        (repo / "feature.py").write_text("new = True\n", encoding="utf-8")
        approved = cli(env, "review", pkg, repo, state, review_path)
        ensure(approved["status"] == "PASS_REVIEWED" and
               all(x["passed"] for x in approved["independent_checks"]),
               "review path failed: %r" % approved)
        evidence["cases"]["high_risk"] = {
            "gate": pending["status"], "stale_review": stale["status"],
            "status": approved["status"], "fresh_checks": len(approved["independent_checks"]),
        }

        # Case C: bounded local correction; second failed check escalates.
        repo, commit = make_repo(home, "retries")
        pkg = make_package(home / "retries.json", "retries", commit, failing=True)
        state = home / "retries-state.json"
        cli(env, "preflight", pkg, repo, state)
        (repo / "feature.py").write_text("new = True\n", encoding="utf-8")
        retry = cli(env, "verify", pkg, repo, state, expected=2)
        escalated = cli(env, "verify", pkg, repo, state, expected=2)
        ensure(retry["status"] == "RETRY_LOCAL" and escalated["status"] == "ESCALATE"
               and escalated["attempt"] == 2, "retry policy not enforced")
        evidence["cases"]["bounded_retries"] = {
            "first": retry["status"], "second": escalated["status"],
            "attempts": escalated["attempt"],
        }

        report = cli(env, "report", extra=None)
        # Report reads CLAUDE_ROUTER_METRICS from the test's isolated environment.
        ensure(report["tasks"] == 3 and report["passed"] == 2 and
               report["escalated"] == 1 and report["review_pending"] == 0,
               "metrics disagreed with actual outcomes: %r" % report)
        evidence["metrics"] = report

        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2, sort_keys=True))
        print("HANDOFF_CLI_E2E_PASS")


if __name__ == "__main__":
    main()
