"""Executable, opt-in Opus-to-Haiku work-package verification.

No LLM call or shell interpretation is performed here. Checks use argv arrays with
subprocess.run(shell=False). The runner does not make code changes; it produces
machine-checkable evidence for the delegated executor and independent reviewer.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

class HandoffError(Exception):
    pass


def _home():
    return Path(os.environ.get("CLAUDE_ROUTER_HOME", str(Path.home() / ".claude-router")))


def _state_path(task_id):
    return _home() / "handoffs" / (task_id + ".json")


def _metrics_path():
    return Path(os.environ.get("CLAUDE_ROUTER_METRICS", str(_home() / "handoff-metrics.jsonl")))


def _safe_path(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise HandoffError("scope paths must be nonempty strings")
    p = path.replace("\\\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise HandoffError("scope paths must be repository-relative: " + path)
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise HandoffError("invalid scope path: " + path)
    return p


def _scope_path(path):
    is_tree = isinstance(path, str) and path.endswith("/**")
    name = _safe_path(path[:-3] if is_tree else path)
    return name, is_tree


def _command_spec(check):
    if not isinstance(check, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(check.get("id", ""))):
        raise HandoffError("each check needs a short unique id")
    cmd = check.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x and "\x00" not in x for x in cmd):
        raise HandoffError("check %s requires a nonempty argv command list" % check["id"])
    if check.get("expect", "success") not in ("success", "failure"):
        raise HandoffError("check %s expect must be success or failure" % check["id"])
    timeout = check.get("timeout_s", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= 300):
        raise HandoffError("check %s timeout_s must be between 1 and 300" % check["id"])


def load_package(path):
    try:
        raw = Path(path).read_bytes()
        pkg = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise HandoffError("unable to load work package: %s" % exc)
    if not isinstance(pkg, dict) or pkg.get("version") != 1:
        raise HandoffError("work package version must be 1")
    tid = pkg.get("task_id")
    if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", tid):
        raise HandoffError("task_id is missing or invalid")
    sha = pkg.get("baseline_commit")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise HandoffError("baseline_commit must be a full 40-character git commit SHA")
    if not isinstance(pkg.get("verified_files"), list) or not pkg["verified_files"]:
        raise HandoffError("verified_files must contain existing repository-relative files")
    for file in pkg["verified_files"]:
        _safe_path(file)
    scope = pkg.get("allowed_paths")
    if not isinstance(scope, list) or not scope:
        raise HandoffError("allowed_paths must be nonempty")
    for pattern in scope:
        _scope_path(pattern)
    if pkg.get("risk", "medium") not in ("low", "medium", "high"):
        raise HandoffError("risk must be low, medium or high")
    if not isinstance(pkg.get("review_required", False), bool):
        raise HandoffError("review_required must be boolean")
    retries = pkg.get("max_local_retries", 1)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 3:
        raise HandoffError("max_local_retries must be an integer from 0 to 3")
    pre = pkg.get("preflight_checks", [])
    checks = pkg.get("verification_checks")
    if not isinstance(pre, list) or not isinstance(checks, list) or not checks:
        raise HandoffError("preflight_checks and verification_checks must be lists; verification cannot be empty")
    ids = set()
    for check in pre + checks:
        _command_spec(check)
        if check["id"] in ids:
            raise HandoffError("duplicate check id: " + check["id"])
        ids.add(check["id"])
    ac = pkg.get("acceptance")
    if not isinstance(ac, list) or not ac:
        raise HandoffError("acceptance must be nonempty")
    verified = {check["id"] for check in checks}
    ac_ids = set()
    for item in ac:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise HandoffError("acceptance item requires id")
        if item["id"] in ac_ids:
            raise HandoffError("duplicate acceptance id")
        ac_ids.add(item["id"])
        required = item.get("checks")
        if not isinstance(required, list) or not required or not all(isinstance(s, str) and s in verified for s in required):
            raise HandoffError("acceptance %s references unknown verification checks" % item["id"])
    digest = hashlib.sha256(json.dumps(pkg, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return pkg, digest


def _git(repo, *args):
    try:
        proc = subprocess.run(["git", "-C", str(repo)] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HandoffError("git unavailable: %s" % exc)
    if proc.returncode:
        raise HandoffError("git %s failed: %s" % (" ".join(args), proc.stderr.decode("utf-8", "replace")[:500]))
    return proc.stdout.decode("utf-8", "replace").strip()


def _repo_root(repo):
    return Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()


def _changed(repo):
    """Tracked and untracked changes; rename source AND destination are checked."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", "status",
                              "--porcelain=v1", "-z", "--untracked-files=all"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError("cannot inspect worktree: %s" % exc)
    parts = out.decode("utf-8", "replace").split("\x00")
    paths = []
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        if len(rec) < 4:
            raise HandoffError("malformed git status record")
        paths.append(rec[3:].replace("\\\\", "/"))
        if "R" in rec[:2] or "C" in rec[:2]:
            if i >= len(parts):
                raise HandoffError("malformed git rename record")
            paths.append(parts[i].replace("\\\\", "/"))
            i += 1
    return sorted(set(paths))


def path_allowed(relative, patterns):
    rel = _safe_path(relative)
    for pattern in patterns:
        target, tree = _scope_path(pattern)
        if os.name == "nt":
            target, rel_cmp = target.casefold(), rel.casefold()
        else:
            rel_cmp = rel
        if rel_cmp == target or (tree and rel_cmp.startswith(target + "/")):
            return True
    return False


def _state_file(pkg, explicit=None):
    return Path(explicit) if explicit else _state_path(pkg["task_id"])


def _read_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandoffError("no valid preflight state: %s" % exc)
    if not isinstance(data, dict):
        raise HandoffError("preflight state must be an object")
    return data


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent),
                                     prefix=".handoff-", suffix=".tmp", delete=False) as fh:
        tmp = fh.name
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(path))


def _run_check(check, repo):
    start = time.monotonic()
    try:
        proc = subprocess.run(check["command"], cwd=str(repo), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=check.get("timeout_s", 120), shell=False)
        rc = proc.returncode
        stdout = proc.stdout.decode("utf-8", "replace")[-2000:]
        stderr = proc.stderr.decode("utf-8", "replace")[-2000:]
        outcome = "success" if rc == 0 else "failure"
        passed = outcome == check.get("expect", "success")
    except (OSError, subprocess.TimeoutExpired) as exc:
        rc, stdout, stderr, passed = None, "", str(exc), False
    return {"id": check["id"], "passed": passed, "returncode": rc,
            "expected": check.get("expect", "success"), "stdout_tail": stdout,
            "stderr_tail": stderr, "seconds": round(time.monotonic() - start, 3)}


def _assert_baseline(pkg, repo):
    current = _git(repo, "rev-parse", "HEAD")
    if current.lower() != pkg["baseline_commit"].lower():
        raise HandoffError("baseline commit mismatch (expected %s; observed %s)" % (pkg["baseline_commit"], current))
    for filename in pkg["verified_files"]:
        if not (repo / _safe_path(filename)).is_file():
            raise HandoffError("verified file missing: " + filename)


def preflight(package, repo, state_file=None):
    pkg, digest = load_package(package)
    root = _repo_root(repo)
    _assert_baseline(pkg, root)
    changed = _changed(root)
    if changed:
        raise HandoffError("worktree must be clean before preflight; changed: " + ", ".join(changed[:12]))
    results = [_run_check(check, root) for check in pkg.get("preflight_checks", [])]
    if any(not x["passed"] for x in results):
        raise HandoffError("preflight check failed: " + json.dumps(results))
    state = {"task_id": pkg["task_id"], "package_hash": digest, "repository": str(root),
             "baseline_commit": pkg["baseline_commit"], "status": "READY", "attempts": 0,
             "started_at": time.time(), "preflight": results}
    _write_json(_state_file(pkg, state_file), state)
    return {"status": "READY", "task_id": pkg["task_id"], "preflight": results}


def _require_active(pkg, digest, state, repo):
    if state.get("package_hash") != digest or state.get("repository") != str(repo):
        raise HandoffError("work package or repository changed since preflight; rerun preflight")
    if state.get("status") not in ("READY", "RETRY_LOCAL"):
        raise HandoffError("handoff is not active: " + str(state.get("status")))
    _assert_baseline(pkg, repo)


def edit_allowed(package, file_path, repo, state_file=None):
    """Used by PreToolUse to deny out-of-scope edits in an activated work package."""
    pkg, digest = load_package(package)
    root = _repo_root(repo)
    state = _read_state(_state_file(pkg, state_file))
    _require_active(pkg, digest, state, root)
    if not isinstance(file_path, str) or not file_path:
        return False
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        rel = candidate.relative_to(root).as_posix()
    except ValueError:
        return False
    return path_allowed(rel, pkg["allowed_paths"])


def _write_metric(pkg, result, state):
    line = {"task_id": pkg["task_id"], "status": result["status"],
            "attempts": state["attempts"], "duration_s": round(time.time() - state["started_at"], 2),
            "verified_count": len(result.get("checks", [])), "failed_count": sum(
                not item["passed"] for item in result.get("checks", [])),
            "changed_count": len(result.get("changed_files", [])), "timestamp": time.time()}
    path = _metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def verify(package, repo, state_file=None):
    pkg, digest = load_package(package)
    root = _repo_root(repo)
    path = _state_file(pkg, state_file)
    state = _read_state(path)
    _require_active(pkg, digest, state, root)
    changed = _changed(root)
    unauthorized = [p for p in changed if not path_allowed(p, pkg["allowed_paths"])]
    checks = [] if unauthorized else [_run_check(c, root) for c in pkg["verification_checks"]]
    state["attempts"] += 1
    failures = {c["id"] for c in checks if not c["passed"]}
    acceptance = {a["id"]: not unauthorized and all(c not in failures for c in a["checks"])
                  for a in pkg["acceptance"]}
    if unauthorized:
        status = "ESCALATE"
        reason = "scope_breach"
    elif failures:
        status = "RETRY_LOCAL" if state["attempts"] <= pkg.get("max_local_retries", 1) else "ESCALATE"
        reason = "verification_failure"
    else:
        status = "REVIEW_REQUIRED" if pkg.get("review_required", False) or pkg.get("risk") == "high" else "PASS"
        reason = "independent_review_pending" if status == "REVIEW_REQUIRED" else "verified"
    result = {"task_id": pkg["task_id"], "status": status, "reason": reason,
              "attempt": state["attempts"], "changed_files": changed, "unauthorized_files": unauthorized,
              "checks": checks, "acceptance": acceptance,
              "review_required": pkg.get("review_required", False) or pkg.get("risk") == "high"}
    state["status"] = status
    state["result"] = result
    _write_json(path, state)
    _write_metric(pkg, result, state)
    return result


def metrics_report(path=None):
    records = []
    path = Path(path) if path else _metrics_path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
            except ValueError:
                continue
    final = {}
    for item in records:
        final[item.get("task_id")] = item
    total = len(final)
    return {"tasks": total, "passed": sum(x.get("status") == "PASS" for x in final.values()),
            "review_pending": sum(x.get("status") == "REVIEW_REQUIRED" for x in final.values()),
            "escalated": sum(x.get("status") == "ESCALATE" for x in final.values()),
            "mean_attempts": round(sum(x.get("attempts", 0) for x in final.values()) / total, 2) if total else 0,
            "mean_duration_s": round(sum(x.get("duration_s", 0) for x in final.values()) / total, 2) if total else 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opt-in, evidence-based Opus-to-Haiku work packages")
    cmds = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "verify"):
        sub = cmds.add_parser(name)
        sub.add_argument("package")
        sub.add_argument("--repo", default=".")
        sub.add_argument("--state", default=None)
    sub = cmds.add_parser("report")
    sub.add_argument("--metrics", default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight(args.package, args.repo, args.state)
        elif args.command == "verify":
            result = verify(args.package, args.repo, args.state)
        else:
            result = metrics_report(args.metrics)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status", "PASS") in ("PASS", "READY", "REVIEW_REQUIRED") else 2
    except HandoffError as exc:
        print(json.dumps({"status": "ESCALATE", "reason": "precondition_or_contract", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
