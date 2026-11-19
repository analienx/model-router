"""Executable handoff contract and PreToolUse enforcement regression tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "router"))
sys.dont_write_bytecode = True
import handoff  # noqa: E402


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handoff-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.pkg_path = self.tmp / "package.json"
        self.state = self.tmp / "state.json"
        self.metrics = self.tmp / "metrics.jsonl"
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Handoff Tests")
        (self.repo / "feature.py").write_text("old = True\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "baseline")
        self.head = self._git("rev-parse", "HEAD")
        self.pkg = {
            "version": 1,
            "task_id": "feature-change",
            "baseline_commit": self.head,
            "verified_files": ["feature.py"],
            "allowed_paths": ["feature.py", "tests/**"],
            "risk": "medium",
            "max_local_retries": 1,
            "preflight_checks": [
                {"id": "red", "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
                 "expect": "failure"}
            ],
            "verification_checks": [
                {"id": "green", "command": [sys.executable, "-c", "import sys; sys.exit(0)"]}
            ],
            "acceptance": [{"id": "AC1", "checks": ["green"]}],
        }
        self._save()

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _git(self, *args):
        p = subprocess.run(["git", "-C", str(self.repo)] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def _save(self):
        self.pkg_path.write_text(json.dumps(self.pkg), encoding="utf-8")

    def _preflight(self):
        return handoff.preflight(self.pkg_path, self.repo, self.state)

    def _verify(self):
        old = os.environ.get("CLAUDE_ROUTER_METRICS")
        os.environ["CLAUDE_ROUTER_METRICS"] = str(self.metrics)
        try:
            return handoff.verify(self.pkg_path, self.repo, self.state)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_ROUTER_METRICS", None)
            else:
                os.environ["CLAUDE_ROUTER_METRICS"] = old

    def test_regression_first_preflight_scope_and_pass(self):
        self.assertEqual(self._preflight()["status"], "READY")
        self.assertTrue(handoff.edit_allowed(self.pkg_path, "feature.py", self.repo, self.state))
        self.assertTrue(handoff.edit_allowed(self.pkg_path, "tests/test_case.py", self.repo, self.state))
        self.assertFalse(handoff.edit_allowed(self.pkg_path, "../elsewhere.py", self.repo, self.state))
        self.assertFalse(handoff.edit_allowed(self.pkg_path, "private.txt", self.repo, self.state))
        (self.repo / "feature.py").write_text("old = False\n", encoding="utf-8")
        report = self._verify()
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["acceptance"]["AC1"])
        self.assertEqual(report["changed_files"], ["feature.py"])
        metrics = handoff.metrics_report(self.metrics)
        self.assertEqual(metrics["passed"], 1)
        self.assertEqual(metrics["tasks"], 1)
        with self.assertRaises(handoff.HandoffError):
            handoff.edit_allowed(self.pkg_path, "feature.py", self.repo, self.state)

    def test_outside_scope_escalates_without_running_checks(self):
        self._preflight()
        (self.repo / "outside.py").write_text("unauthorized\n", encoding="utf-8")
        report = self._verify()
        self.assertEqual(report["status"], "ESCALATE")
        self.assertEqual(report["reason"], "scope_breach")
        self.assertEqual(report["checks"], [])
        self.assertFalse(report["acceptance"]["AC1"])

    def test_retry_once_then_escalate(self):
        self.pkg["verification_checks"][0]["command"] = [sys.executable, "-c", "import sys; sys.exit(3)"]
        self._save()
        self._preflight()
        first = self._verify()
        second = self._verify()
        self.assertEqual(first["status"], "RETRY_LOCAL")
        self.assertEqual(second["status"], "ESCALATE")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["checks"][0]["returncode"], 3)

    def test_high_risk_never_self_approves(self):
        self.pkg["risk"] = "high"
        self._save()
        self._preflight()
        (self.repo / "feature.py").write_text("changed\n", encoding="utf-8")
        report = self._verify()
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertTrue(report["review_required"])

    def test_external_review_requires_fresh_diff_and_reruns_checks(self):
        self.pkg["risk"] = "high"
        self._save()
        self._preflight()
        (self.repo / "feature.py").write_text("reviewed = True\n", encoding="utf-8")
        verified = self._verify()
        review_file = self.tmp / "review.json"
        evidence = {"task_id": self.pkg["task_id"], "reviewer": "opus-independent",
                    "decision": "approved", "change_digest": verified["change_digest"],
                    "acceptance": verified["acceptance"], "findings": []}
        review_file.write_text(json.dumps(evidence), encoding="utf-8")
        (self.repo / "feature.py").write_text("stale = True\n", encoding="utf-8")
        with self.assertRaisesRegex(handoff.HandoffError, "worktree changed"):
            handoff.review(self.pkg_path, self.repo, review_file, self.state)
        (self.repo / "feature.py").write_text("reviewed = True\n", encoding="utf-8")
        prior = os.environ.get("CLAUDE_ROUTER_METRICS")
        os.environ["CLAUDE_ROUTER_METRICS"] = str(self.metrics)
        try:
            reviewed = handoff.review(self.pkg_path, self.repo, review_file, self.state)
        finally:
            if prior is None:
                os.environ.pop("CLAUDE_ROUTER_METRICS", None)
            else:
                os.environ["CLAUDE_ROUTER_METRICS"] = prior
        self.assertEqual(reviewed["status"], "PASS_REVIEWED")
        self.assertTrue(all(x["passed"] for x in reviewed["independent_checks"]))
        self.assertEqual(handoff.metrics_report(self.metrics)["passed"], 1)

    def test_preflight_fails_closed_on_dirty_or_mismatched_baseline(self):
        (self.repo / "feature.py").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(handoff.HandoffError, "clean"):
            self._preflight()
        (self.repo / "feature.py").write_text("old = True\n", encoding="utf-8")
        self.pkg["baseline_commit"] = "0" * 40
        self._save()
        with self.assertRaisesRegex(handoff.HandoffError, "baseline"):
            self._preflight()

    def test_package_mutation_invalidates_edit_scope(self):
        self._preflight()
        self.pkg["allowed_paths"].append("secret.py")
        self._save()
        with self.assertRaisesRegex(handoff.HandoffError, "changed since preflight"):
            handoff.edit_allowed(self.pkg_path, "secret.py", self.repo, self.state)

    def test_invalid_manifest_rejected_before_execution(self):
        self.pkg["allowed_paths"] = ["../"]
        self._save()
        with self.assertRaises(handoff.HandoffError):
            self._preflight()
        self.pkg["allowed_paths"] = ["feature.py"]
        self.pkg["acceptance"][0]["checks"] = ["unknown"]
        self._save()
        with self.assertRaises(handoff.HandoffError):
            self._preflight()

    def test_hook_enforces_scope_but_plain_claude_unaffected(self):
        self._preflight()
        def call(tool_path, active=True):
            env = dict(os.environ)
            env.update({"CLAUDE_ROUTER_HOME": str(self.home),
                        "CLAUDE_ROUTER_HANDOFF": str(self.pkg_path),
                        "CLAUDE_ROUTER_HANDOFF_STATE": str(self.state),
                        "CLAUDE_ROUTER_HANDOFF_REPO": str(self.repo)})
            if active:
                env["CLAUDE_ROUTER"] = "1"
            else:
                env.pop("CLAUDE_ROUTER", None)
            payload = {"session_id": "testing", "cwd": str(self.repo),
                       "tool_name": "Edit", "agent_type": "architect",
                       "tool_input": {"file_path": tool_path, "old_string": "old",
                                      "new_string": "new"}}
            return subprocess.run([sys.executable, str(ROOT / "router" / "guard_hook.py")],
                                  input=json.dumps(payload), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, timeout=20)
        allowed = call(str(self.repo / "feature.py"))
        denied = call(str(self.repo / "private.py"))
        plain = call(str(self.repo / "private.py"), active=False)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed.stdout.strip(), "")
        self.assertIn("scope violation", denied.stdout)
        self.assertEqual(plain.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
