"""Tests for claude-model-router. Run from the package folder:

    python -m unittest discover -s tests -v

No network, no real model calls: `claude` is replaced by tests/fake_claude.py.
Tests named test_review_* are regressions for the code review findings (v1.1.0).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "router"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
sys.path.insert(0, str(ROUTER))
sys.dont_write_bytecode = True
import common  # noqa: E402
import judge_hook  # noqa: E402
import launch  # noqa: E402

BIG = "x\n" * 40


def _alive(pid):
    """True if pid exists (and, on Linux, is not a zombie)."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


class RouterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-test-"))
        self.home = self.tmp / "router-home"
        self.home.mkdir()
        self.record = self.tmp / "calls.jsonl"
        self.config = json.loads((ROUTER / "router.config.example.json").read_text(encoding="utf-8"))
        self.config["foundry"]["resource"] = "test-resource"
        self.config["judge"]["claude_path"] = [sys.executable, str(FAKE)]
        self.config["judge"]["include_git_signals"] = False
        for step in self.config["judge"]["chain"]:
            if step["backend"] != "rules":
                step["timeout_s"] = 5
        self.write_config()
        self.base_env = {k: v for k, v in os.environ.items()
                         if not k.startswith(("ANTHROPIC_", "CLAUDE", "FAKE_CLAUDE"))}
        self.base_env.update({"CLAUDE_ROUTER_HOME": str(self.home), "FAKE_CLAUDE_RECORD": str(self.record),
                              "PYTHONDONTWRITEBYTECODE": "1"})

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def write_config(self):
        (self.home / "router.config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def env(self, active=True, **extra):
        e = dict(self.base_env)
        if active:
            e["CLAUDE_ROUTER"] = "1"
        e.update(extra)
        return e

    def run_hook(self, script, payload, env):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        p = subprocess.run([sys.executable, str(ROUTER / script)], input=raw, capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.strip()
        return json.loads(out) if out else None

    def judge(self, prompt, session="s1", **env_extra):
        return self.run_hook("judge_hook.py", {"session_id": session, "prompt": prompt, "cwd": str(self.tmp),
                                               "hook_event_name": "UserPromptSubmit"}, self.env(**env_extra))

    def guard(self, tool, tool_input, session="s1", agent=None, transcript=None, **env_extra):
        payload = {"session_id": session, "tool_name": tool, "tool_input": tool_input, "hook_event_name": "PreToolUse"}
        if agent:
            payload["agent_type"] = agent
        if transcript:
            payload["transcript_path"] = str(transcript)
        return self.run_hook("guard_hook.py", payload, self.env(**env_extra))

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(l) for l in self.record.read_text(encoding="utf-8").splitlines() if l.strip()]

    def state(self, session="s1"):
        return json.loads((self.home / "state" / (session + ".json")).read_text(encoding="utf-8"))

    def set_state(self, data, session="s1"):
        d = self.home / "state"
        d.mkdir(exist_ok=True)
        (d / (session + ".json")).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def set_tier(self, tier, used=0, session="s1"):
        self.set_state({"tier": tier, "main_lines_used": used}, session)

    @staticmethod
    def context(out):
        return out["hookSpecificOutput"]["additionalContext"]

    @staticmethod
    def denied(out):
        return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    @staticmethod
    def reason(out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]


class JudgeHookTests(RouterTestCase):
    def test_inactive_without_launcher_variable(self):
        out = self.run_hook("judge_hook.py", {"session_id": "s1", "prompt": "design a schema"}, self.env(active=False))
        self.assertIsNone(out)
        self.assertEqual(self.calls(), [])

    def test_recursion_guard_inside_judge_process(self):
        out = self.judge("design a schema", CLAUDE_ROUTER_JUDGE="1")
        self.assertIsNone(out)
        self.assertEqual(self.calls(), [])

    def test_deep_verdict_from_subscription(self):
        out = self.judge("Change the storage schema and migrate old data", FAKE_CLAUDE_MODE_SUBSCRIPTION="deep")
        ctx = self.context(out)
        self.assertIn("DEEP", ctx)
        self.assertIn("`architect`", ctx)
        self.assertIn("blocked this turn", ctx)
        self.assertEqual(self.state()["tier"], "deep")
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["backend"], "subscription")

    def test_subscription_call_has_no_provider_variables(self):
        self.judge("hello there, please refactor the parser", FAKE_CLAUDE_MODE_SUBSCRIPTION="standard",
                   CLAUDE_CODE_USE_FOUNDRY="1", ANTHROPIC_FOUNDRY_RESOURCE="r", ANTHROPIC_FOUNDRY_API_KEY="secret",
                   ANTHROPIC_API_KEY="sk-test", ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-5-5",
                   CLAUDE_CODE_MAX_RETRIES="10")
        call = self.calls()[0]
        self.assertEqual(call["backend"], "subscription")
        leaked = [k for k in call["env"] if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_")]
        self.assertEqual(leaked, [])
        self.assertEqual(call["env"].get("CLAUDE_ROUTER_JUDGE"), "1")
        self.assertEqual(call["env"].get("CLAUDE_CODE_MAX_RETRIES"), "1")
        self.assertNotIn("CLAUDE_ROUTER", call["env"])

    def test_judge_arguments_and_prompt_only_on_stdin(self):
        secret_prompt = "refactor the parser module; token=abc123"
        self.judge(secret_prompt, FAKE_CLAUDE_MODE_SUBSCRIPTION="standard")
        call = self.calls()[0]
        args = call["args"]
        for flag, value in (("--model", "sonnet"), ("--effort", "max"), ("--output-format", "json"),
                            ("--max-turns", "1"), ("--disallowedTools", "*")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--no-session-persistence", args)
        self.assertIn("--system-prompt-file", args)
        self.assertFalse(any("abc123" in a for a in args), "prompt text must not appear in argv")
        self.assertIn("abc123", call["stdin"])
        self.assertEqual(Path(call["cwd"]).resolve(), (self.home / "judge-cwd").resolve())

    def test_falls_back_to_foundry_with_foundry_env(self):
        out = self.judge("add a dry-run flag", FAKE_CLAUDE_MODE_SUBSCRIPTION="is_error",
                         FAKE_CLAUDE_MODE_FOUNDRY="standard")
        self.assertIn("STANDARD", self.context(out))
        self.assertIn("source: foundry:sonnet", self.context(out))
        calls = self.calls()
        self.assertEqual([c["backend"] for c in calls], ["subscription", "foundry"])
        fenv = calls[1]["env"]
        self.assertEqual(fenv.get("CLAUDE_CODE_USE_FOUNDRY"), "1")
        self.assertEqual(fenv.get("ANTHROPIC_FOUNDRY_RESOURCE"), "test-resource")
        self.assertEqual(fenv.get("ANTHROPIC_DEFAULT_SONNET_MODEL"), "claude-sonnet-5-5")
        self.assertEqual(fenv.get("CLAUDE_ROUTER_JUDGE"), "1")

    def test_all_models_fail_then_rules_route_up(self):
        out = self.judge("please update the readme and the changelog", FAKE_CLAUDE_MODE="garbage")
        ctx = self.context(out)
        self.assertIn("STANDARD", ctx)
        self.assertIn("source: rules", ctx)
        self.assertIn("Judge problems", ctx)

    def test_rules_detect_deep_keywords(self):
        out = self.judge("there is a race condition in the uploader", FAKE_CLAUDE_MODE="crash")
        self.assertIn("DEEP", self.context(out))

    def test_rules_short_question_is_fast(self):
        out = self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="crash")
        self.assertIn("FAST", self.context(out))

    def test_timeout_falls_through(self):
        self.config["judge"]["chain"] = [
            {"backend": "subscription", "model": "sonnet", "effort": "max", "timeout_s": 1},
            {"backend": "rules"}]
        self.write_config()
        out = self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="sleep")
        self.assertIn("timed out", self.context(out))
        self.assertIn("FAST", self.context(out))

    def test_chain_without_rules_routes_up_when_everything_fails(self):
        self.config["judge"]["chain"] = [{"backend": "subscription", "model": "sonnet", "effort": "max", "timeout_s": 5}]
        self.write_config()
        out = self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="crash")
        self.assertIn("STANDARD", self.context(out))
        self.assertIn("every judge failed", self.context(out))

    def test_low_confidence_routes_up(self):
        out = self.judge("rename a variable in utils", FAKE_CLAUDE_MODE_SUBSCRIPTION="fast_low")
        self.assertIn("STANDARD", self.context(out))
        self.assertIn("routed up from fast", self.context(out))

    def test_prose_wrapped_json_and_synonyms_and_arrays_parse(self):
        self.assertIn("STANDARD", self.context(self.judge("add tests for the cli", FAKE_CLAUDE_MODE="prose_json")))
        self.assertIn("DEEP", self.context(self.judge("rework the job scheduler", session="s2", FAKE_CLAUDE_MODE="synonym")))
        self.assertIn("DEEP", self.context(self.judge("rework the job scheduler", session="s3", FAKE_CLAUDE_MODE="json_array")))
        self.assertIn("FAST", self.context(self.judge("rework the job scheduler", session="s4", FAKE_CLAUDE_MODE="plain_text_verdict")))

    def test_invalid_tier_is_rejected_not_trusted(self):
        out = self.judge("please update the readme and the changelog", FAKE_CLAUDE_MODE="bad_tier")
        ctx = self.context(out)
        self.assertIn("unknown tier", ctx)
        self.assertIn("source: rules", ctx)

    def test_override_prefix_skips_judge(self):
        out = self.judge("::deep just look at this function", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("DEEP", self.context(out))
        self.assertIn("source: override", self.context(out))
        self.assertEqual(self.calls(), [])

    def test_follow_up_phrase_is_sticky(self):
        self.judge("Change the storage schema and migrate old data", FAKE_CLAUDE_MODE="deep")
        n = len(self.calls())
        out = self.judge("yes", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("DEEP", self.context(out))
        self.assertIn("source: sticky", self.context(out))
        self.assertEqual(len(self.calls()), n)

    def test_missing_deep_deployment_maps_to_standard(self):
        self.config["tiers"]["deep"]["deployment"] = None
        self.write_config()
        out = self.judge("Change the storage schema", FAKE_CLAUDE_MODE="deep")
        ctx = self.context(out)
        self.assertIn("STANDARD", ctx)
        self.assertIn("no Foundry deployment for deep", ctx)
        self.assertIn("`implementer`", ctx)

    def test_invalid_config_turns_routing_off_with_message(self):
        (self.home / "router.config.json").write_text("{not json", encoding="utf-8")
        out = self.judge("anything", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("Routing is OFF", self.context(out))

    def test_wrong_shaped_config_is_reported(self):
        (self.home / "router.config.json").write_text(json.dumps({"judge": {"chain": "subscription"}}), encoding="utf-8")
        out = self.judge("anything", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("judge.chain must be a non-empty list", self.context(out))

    def test_malformed_hook_input_does_not_crash(self):
        for raw in ("", "not json", "[1, 2]", json.dumps({"prompt": 42, "session_id": ["x"]})):
            p = subprocess.run([sys.executable, str(ROUTER / "judge_hook.py")], input=raw, capture_output=True,
                               text=True, encoding="utf-8", timeout=60, env=self.env(FAKE_CLAUDE_MODE="crash"))
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_decision_is_logged_without_prompt_text(self):
        self.judge("refactor the parser; secret-word-xyz", FAKE_CLAUDE_MODE="standard")
        log = (self.home / "logs" / "decisions.jsonl").read_text(encoding="utf-8")
        self.assertIn('"tier": "standard"', log)
        self.assertNotIn("secret-word-xyz", log)

    def test_non_ascii_prompt(self):
        out = self.judge("Oprav export do CSV – duplicitní řádky 😅", FAKE_CLAUDE_MODE="standard")
        self.assertIn("STANDARD", self.context(out))
        self.assertIn("řádky", self.calls()[0]["stdin"])

    # --- review regressions -------------------------------------------------------------------

    def test_review_judge_is_isolated_from_user_memory_hooks_and_settings(self):
        self.judge("refactor the parser module", FAKE_CLAUDE_MODE_SUBSCRIPTION="standard")
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(call["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"), "1")
        self.assertEqual(args[args.index("--setting-sources") + 1], "project,local")
        settings = json.loads(Path(args[args.index("--settings") + 1]).read_text(encoding="utf-8"))
        self.assertIs(settings.get("disableAllHooks"), True)
        self.assertNotIn("--bare", args, "bare mode never reads the subscription login")

    def test_review_foundry_judge_uses_bare_and_overrides(self):
        self.config["tiers"]["standard"]["deployment"] = "sonnet-prod"
        self.write_config()
        self.judge("add a dry-run flag", FAKE_CLAUDE_MODE_SUBSCRIPTION="crash", FAKE_CLAUDE_MODE_FOUNDRY="standard")
        call = [c for c in self.calls() if c["backend"] == "foundry"][0]
        args = call["args"]
        self.assertIn("--bare", args)
        self.assertEqual(call["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"), "1")
        settings = json.loads(Path(args[args.index("--settings") + 1]).read_text(encoding="utf-8"))
        self.assertEqual(settings, {"disableAllHooks": True, "modelOverrides": {"claude-sonnet-5-5": "sonnet-prod"}})

    def test_review_short_new_task_is_judged_not_sticky(self):
        self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="fast")
        out = self.judge("redesign the auth schema", FAKE_CLAUDE_MODE="deep")
        self.assertIn("DEEP", self.context(out))
        self.assertIn("source: subscription:sonnet", self.context(out))

    def test_review_escalation_phrases_route_up(self):
        self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="fast")
        for i, phrase in enumerate(("still failing", "Try again!", "didn't work, same error")):
            self.set_tier("fast")
            out = self.judge(phrase, FAKE_CLAUDE_MODE="fail_if_called")
            self.assertIn("STANDARD", self.context(out), phrase)
            self.assertIn("source: escalation", self.context(out), phrase)
        self.set_tier("standard")
        self.assertIn("DEEP", self.context(self.judge("still broken", FAKE_CLAUDE_MODE="fail_if_called")))

    def test_review_path_prompt_is_not_a_slash_command(self):
        self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="fast")
        out = self.judge("/src/app.py has a race condition between the two writer threads", FAKE_CLAUDE_MODE="crash")
        self.assertIn("DEEP", self.context(out))
        self.assertIn("source: rules", self.context(out))
        out = self.judge("/review", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("source: slash-command", self.context(out))

    def test_review_os_error_in_judge_chain_still_gives_verdict_and_fresh_state(self):
        self.config["tiers"]["standard"]["deployment"] = "sonnet-prod"  # forces a generated settings file
        self.write_config()
        (self.home / "generated").write_text("I am a file, not a folder", encoding="utf-8")
        self.set_state({"tier": "deep", "main_lines_used": 40})
        out = self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="fast")
        ctx = self.context(out)
        self.assertIn("FAST", ctx)
        self.assertIn("source: rules", ctx)
        self.assertIn("could not prepare the judge call", ctx)
        st = self.state()
        self.assertEqual((st["tier"], st["main_lines_used"]), ("fast", 0))

    def test_review_wrong_shaped_config_values_turn_routing_off(self):
        cases = [("logging", None), ("include_git_signals", "yes"), ("max_retries", True),
                 ("prompt_caching_1h", "no"), ("min_confidence", True), ("timeout_s", False)]
        for key, value in cases:
            cfg = json.loads(json.dumps(self.config))
            if key == "logging":
                cfg["logging"] = value
            elif key == "include_git_signals":
                cfg["judge"]["include_git_signals"] = value
            elif key == "max_retries":
                cfg["launcher"]["max_retries"] = value
            elif key == "prompt_caching_1h":
                cfg["foundry"]["prompt_caching_1h"] = value
            elif key == "min_confidence":
                cfg["judge"]["min_confidence"] = value
            else:
                cfg["judge"]["chain"][0]["timeout_s"] = value
            (self.home / "router.config.json").write_text(json.dumps(cfg), encoding="utf-8")
            out = self.judge("anything at all here", FAKE_CLAUDE_MODE="fail_if_called")
            self.assertIn("Routing is OFF", self.context(out), key)

    def test_review_wrong_shaped_state_does_not_crash(self):
        for bad in ({"tier": "deep", "reason": None, "main_lines_used": "abc"}, {"tier": 7}, [1, 2], "garbage"):
            self.set_state(bad)
            out = self.judge("please refactor the parser module", FAKE_CLAUDE_MODE="standard")
            self.assertIn("STANDARD", self.context(out))

    def test_e2e_background_agent_notification_is_not_a_new_prompt(self):
        self.judge("Change the storage schema and migrate old data", FAKE_CLAUDE_MODE="deep")
        st = self.state()
        st["main_lines_used"] = 7
        self.set_state(st)
        n = len(self.calls())
        out = self.judge("<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n</task-notification>",
                         FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("DEEP", self.context(out))
        self.assertIn("source: notification", self.context(out))
        self.assertEqual(len(self.calls()), n, "a notification must not call the judge")
        self.assertEqual(self.state()["main_lines_used"], 7, "a notification must not reset the edit budget")

    def test_review_judge_timeout_not_blocked_by_grandchild_holding_output(self):
        self.config["judge"]["chain"] = [
            {"backend": "subscription", "model": "sonnet", "effort": "max", "timeout_s": 1},
            {"backend": "rules"}]
        self.write_config()
        pid_file = self.tmp / "grandchild.pid"
        t0 = time.time()
        out = self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="grandchild_hang",
                         FAKE_CLAUDE_PID_FILE=str(pid_file))
        self.assertLess(time.time() - t0, 12, "the timeout must bound the whole process tree")
        self.assertIn("timed out", self.context(out))
        if os.name == "nt" or Path("/proc").is_dir():
            pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while _alive(pid) and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(_alive(pid), "the judge's grandchild must be killed with it, not left running")

    def test_review_resource_with_trailing_newline_rejected(self):
        self.config["foundry"]["resource"] = "my-resource\n"
        self.write_config()
        with self.assertRaises(common.RouterError):
            common.load_config(self.home / "router.config.json")

    def test_review_example_config_matches_defaults(self):
        example = json.loads((ROUTER / "router.config.example.json").read_text(encoding="utf-8"))
        example.pop("_readme", None)
        example["foundry"]["resource"] = ""
        self.assertEqual(example, common.DEFAULT_CONFIG)


class GuardHookTests(RouterTestCase):
    def test_inactive_allows_everything(self):
        self.set_tier("deep")
        out = self.run_hook("guard_hook.py", {"session_id": "s1", "tool_name": "Write",
                                              "tool_input": {"content": BIG}}, self.env(active=False))
        self.assertIsNone(out)

    def test_deep_blocks_any_main_thread_edit(self):
        self.set_tier("deep")
        out = self.guard("Edit", {"old_string": "a", "new_string": "b"})
        self.assertTrue(self.denied(out))
        self.assertIn("`architect`", self.reason(out))

    def test_fast_budget_is_cumulative_per_turn(self):
        self.set_tier("fast")
        for _ in range(4):  # 4 x 10 = 40 lines = budget
            self.assertIsNone(self.guard("Write", {"content": "x\n" * 10}))
        self.assertEqual(self.state()["main_lines_used"], 40)
        self.assertTrue(self.denied(self.guard("Edit", {"old_string": "a", "new_string": "b"})))

    def test_new_prompt_resets_budget(self):
        self.set_tier("fast", used=40)
        self.judge("where is the config loaded?", FAKE_CLAUDE_MODE="fast")
        self.assertIsNone(self.guard("Edit", {"old_string": "a", "new_string": "b"}))

    def test_escalation_agents_are_unlimited(self):
        self.set_tier("deep")
        for agent in ("architect", "implementer", "deep-haiku"):
            self.assertIsNone(self.guard("Write", {"content": BIG * 5}, agent=agent))

    def test_other_subagents_are_limited(self):
        self.set_tier("fast")
        self.assertTrue(self.denied(self.guard("Write", {"content": BIG}, agent="general-purpose")))
        self.assertIsNone(self.guard("Write", {"content": "x\n" * 5}, agent="general-purpose"))

    def test_model_override_on_pinned_agents_is_denied(self):
        out = self.guard("Agent", {"subagent_type": "architect", "model": "haiku", "prompt": "x"})
        self.assertTrue(self.denied(out))
        self.assertIsNone(self.guard("Agent", {"subagent_type": "architect", "prompt": "x"}))
        self.assertIsNone(self.guard("Agent", {"subagent_type": "Explore", "model": "haiku", "prompt": "x"}))

    def test_missing_state_uses_standard_budget(self):
        self.assertIsNone(self.guard("Edit", {"old_string": "a", "new_string": "x\n" * 10}, session="fresh"))
        self.assertTrue(self.denied(self.guard("Edit", {"old_string": "a", "new_string": "b"}, session="fresh")))

    def test_multiedit_and_notebook_counted(self):
        self.set_tier("standard")
        self.assertTrue(self.denied(self.guard("MultiEdit", {"edits": [{"old_string": "a", "new_string": "x\n" * 6},
                                                                        {"old_string": "b", "new_string": "y\n" * 6}]})))
        self.assertTrue(self.denied(self.guard("NotebookEdit", {"new_source": "z\n" * 11})))

    def test_other_tools_ignored(self):
        self.set_tier("deep")
        self.assertIsNone(self.guard("Bash", {"command": "ls"}))
        self.assertIsNone(self.guard("Read", {"file_path": "/x"}))

    def test_malformed_inputs(self):
        self.set_tier("deep")
        for payload in ("", "[]", json.dumps({"tool_name": "Edit", "tool_input": "oops"}),
                        json.dumps({"tool_name": "MultiEdit", "tool_input": {"edits": "nope"}}),
                        json.dumps({"tool_name": 5, "agent_type": {"x": 1}}),
                        json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": 3}, "transcript_path": 9})):
            p = subprocess.run([sys.executable, str(ROUTER / "guard_hook.py")], input=payload, capture_output=True,
                               text=True, encoding="utf-8", timeout=60, env=self.env())
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_corrupt_state_file(self):
        self.set_state("{corrupt")
        self.assertIsNone(self.guard("Edit", {"old_string": "a", "new_string": "b"}))

    # --- review regressions -------------------------------------------------------------------

    def test_review_empty_write_and_cell_delete_blocked_under_deep(self):
        self.set_tier("deep")
        target = self.tmp / "core.py"
        target.write_text("x\n" * 100, encoding="utf-8")
        self.assertTrue(self.denied(self.guard("Write", {"file_path": str(target), "content": ""})))
        self.assertTrue(self.denied(self.guard("NotebookEdit", {"notebook_path": str(self.tmp / "n.ipynb"),
                                                                "cell_id": "c1", "edit_mode": "delete"})))

    def test_review_write_counts_the_file_it_replaces(self):
        self.set_tier("standard")
        target = self.tmp / "big.py"
        target.write_text("line\n" * 300, encoding="utf-8")
        out = self.guard("Write", {"file_path": str(target), "content": "tiny\n"})
        self.assertTrue(self.denied(out))
        self.assertIn("300", self.reason(out))
        self.assertIsNone(self.guard("Write", {"file_path": str(self.tmp / "new.py"), "content": "x\n" * 5}))

    def test_review_notebook_delete_counts_cell(self):
        self.set_tier("standard")
        nb = self.tmp / "n.ipynb"
        nb.write_text(json.dumps({"cells": [{"id": "c1", "source": ["a\n"] * 30}]}), encoding="utf-8")
        self.assertTrue(self.denied(self.guard("NotebookEdit", {"notebook_path": str(nb), "cell_id": "c1",
                                                                "edit_mode": "delete"})))

    def test_review_replace_all_counts_every_occurrence(self):
        self.set_tier("standard")
        target = self.tmp / "calls.py"
        target.write_text("foo(1)\n" * 30, encoding="utf-8")
        edit = {"file_path": str(target), "old_string": "foo(", "new_string": "bar("}
        out = self.guard("Edit", dict(edit, replace_all=True))
        self.assertTrue(self.denied(out))
        self.assertIn("30", self.reason(out))
        self.assertIsNone(self.guard("Edit", edit))  # a single replacement is 1 line

    def test_review_agent_choice_must_match_verdict(self):
        self.set_tier("deep")
        out = self.guard("Agent", {"subagent_type": "implementer", "prompt": "x"})
        self.assertTrue(self.denied(out))
        self.assertIn("architect", self.reason(out))
        self.assertTrue(self.denied(self.guard("Agent", {"subagent_type": "deep-haiku", "prompt": "x"})))
        self.assertIsNone(self.guard("Agent", {"subagent_type": "architect", "prompt": "x"}))
        self.set_tier("standard")
        self.assertTrue(self.denied(self.guard("Agent", {"subagent_type": "deep-haiku", "prompt": "x"})))
        self.assertIsNone(self.guard("Agent", {"subagent_type": "architect", "prompt": "x"}))
        self.set_tier("fast")
        self.assertIsNone(self.guard("Agent", {"subagent_type": "deep-haiku", "prompt": "x"}))

    def _transcript(self, *records):
        tr = self.tmp / "transcript.jsonl"
        tr.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return tr

    @staticmethod
    def _agent_call(tid, sub):
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": sub, "prompt": "fix"}}]}}

    @staticmethod
    def _notification(tid, summary):
        # Shape written by Claude Code 2.1.327 for a background agent that stopped (seen in the e2e run).
        return {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": (
            "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>failed</status>\n"
            "<summary>%s</summary>\n</task-notification>" % (tid, summary))}}

    def test_review_deep_haiku_allowed_after_throttled_pinned_agent(self):
        self.set_tier("deep")
        call = {"subagent_type": "deep-haiku", "prompt": "x"}
        prompt = {"type": "user", "message": {"role": "user", "content": "redesign the export locking"}}
        launched = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_A", "content": [{"type": "text", "text": "Async agent launched"}]}]}}
        failed_429 = self._notification("toolu_A", 'Agent "x" failed: API Error: Request rejected (429) rate_limit')
        # background agent failed with 429 -> allowed
        tr = self._transcript(prompt, self._agent_call("toolu_A", "architect"), launched, failed_429)
        self.assertIsNone(self.guard("Agent", call, transcript=tr))
        # still running (no notification yet) -> denied
        tr = self._transcript(prompt, self._agent_call("toolu_A", "architect"), launched)
        self.assertTrue(self.denied(self.guard("Agent", call, transcript=tr)))
        # the router's own directive mentions "429": must not count as evidence
        directive = {"type": "api-request-blob", "message": {"role": "system", "content": [
            {"type": "text", "text": "[router] ... failed with a rate-limit (429) or overloaded (529) error ..."}]}}
        tr = self._transcript(prompt, self._agent_call("toolu_A", "architect"), launched, directive)
        self.assertTrue(self.denied(self.guard("Agent", call, transcript=tr)))
        # a 429 notification for some OTHER tool call does not count
        tr = self._transcript(prompt, self._agent_call("toolu_A", "architect"), launched,
                              self._notification("toolu_OTHER", "API Error (429)"))
        self.assertTrue(self.denied(self.guard("Agent", call, transcript=tr)))
        # an agent that failed for another reason does not count
        tr = self._transcript(prompt, self._agent_call("toolu_A", "architect"), launched,
                              self._notification("toolu_A", "Agent failed: file not found"))
        self.assertTrue(self.denied(self.guard("Agent", call, transcript=tr)))
        # synchronous agent: 429 in the tool_result of that call -> allowed
        sync = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_A", "is_error": True, "content": "API Error: 529 overloaded"}]}}
        tr = self._transcript(prompt, self._agent_call("toolu_A", "implementer"), sync)
        self.assertIsNone(self.guard("Agent", call, transcript=tr))
        # junk lines and a cut first line are ignored, not fatal
        tr = self.tmp / "junk.jsonl"
        tr.write_text('ial line"}\nnot json\n[1,2]\n' + json.dumps(self._agent_call("toolu_A", "architect")) + "\n"
                      + json.dumps(failed_429) + "\n", encoding="utf-8")
        self.assertIsNone(self.guard("Agent", call, transcript=tr))

    def test_e2e_notification_of_throttled_pinned_call_unlocks_deep_haiku(self):
        # Found by the end-to-end run with real Claude Code 2.1.327: subagents run in the background, the
        # 429 arrives as a <task-notification> prompt, and the transcript may not be flushed in time.
        self.judge("Change the storage schema and migrate old data", FAKE_CLAUDE_MODE="deep")
        call = {"subagent_type": "deep-haiku", "prompt": "x"}
        self.assertTrue(self.denied(self.guard("Agent", call)))
        payload = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_ARCH", "hook_event_name": "PreToolUse",
                   "tool_input": {"subagent_type": "architect", "prompt": "design"}}
        self.assertIsNone(self.run_hook("guard_hook.py", payload, self.env()))
        self.assertEqual(self.state()["pinned_calls"], ["toolu_ARCH"])
        notif = ("<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>failed</status>\n"
                 "<summary>Agent failed: API Error: Request rejected (429) rate_limit</summary>\n</task-notification>")
        # a 429 for some other call does not unlock it
        self.judge(notif % "toolu_OTHER", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertTrue(self.denied(self.guard("Agent", call)))
        # a successful completion of the pinned call does not unlock it
        self.judge((notif % "toolu_ARCH").replace("failed", "completed").replace("(429) rate_limit", "ok"),
                   FAKE_CLAUDE_MODE="fail_if_called")
        self.assertTrue(self.denied(self.guard("Agent", call)))
        # the 429 failure of exactly that call unlocks it
        out = self.judge(notif % "toolu_ARCH", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIn("deep-haiku` is now accepted", self.context(out))
        self.assertIsNone(self.guard("Agent", call))
        # a new real prompt starts over
        self.judge("Change the storage schema again", FAKE_CLAUDE_MODE="deep")
        self.assertTrue(self.denied(self.guard("Agent", call)))

    def test_e2e_429_in_a_notification_path_or_id_is_not_evidence(self):
        # Found by CI: a random background-agent id contained "429". Ids and the output-file path (which
        # carries the project folder, e.g. ".../issue-429/") are not error text and must never unlock deep-haiku.
        self.judge("Change the storage schema and migrate old data", FAKE_CLAUDE_MODE="deep")
        call = {"subagent_type": "deep-haiku", "prompt": "x"}
        payload = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_ARCH", "hook_event_name": "PreToolUse",
                   "tool_input": {"subagent_type": "architect", "prompt": "design"}}
        self.assertIsNone(self.run_hook("guard_hook.py", payload, self.env()))
        notif = ("<task-notification>\n<task-id>a73c74297540d6f7e</task-id>\n<tool-use-id>toolu_ARCH</tool-use-id>\n"
                 "<output-file>/tmp/claude-1000/-home-me-issue-429/s1/tasks/a73c74297540d6f7e.output</output-file>\n"
                 "<status>failed</status>\n<summary>Agent failed: %s</summary>\n</task-notification>")
        other_failure = notif % "API Error: authentication failed"
        # judge hook: a failure for another reason in a "429" folder does not mark the call as throttled
        self.judge(other_failure, FAKE_CLAUDE_MODE="fail_if_called")
        self.assertEqual(self.state().get("throttled_calls", []), [])
        self.assertTrue(self.denied(self.guard("Agent", call)))
        # guard's transcript fallback: same rule
        prompt = {"type": "user", "message": {"role": "user", "content": "redesign the export locking"}}
        tr = self._transcript(prompt, self._agent_call("toolu_ARCH", "architect"),
                              {"type": "user", "message": {"role": "user", "content": other_failure}})
        self.assertTrue(self.denied(self.guard("Agent", call, transcript=tr)))
        # a real rate-limit error in the same notification still counts
        self.judge(notif % "API Error: Request rejected (429) rate_limit", FAKE_CLAUDE_MODE="fail_if_called")
        self.assertIsNone(self.guard("Agent", call))

    def test_review_other_subagent_limit_is_cumulative(self):
        self.set_tier("fast")
        for _ in range(4):
            self.assertIsNone(self.guard("Write", {"content": "x\n" * 5}, agent="general-purpose"))
        self.assertTrue(self.denied(self.guard("Write", {"content": "x\n" * 5}, agent="general-purpose")))


class LauncherTests(RouterTestCase):
    def cfg(self):
        return common.load_config(self.home / "router.config.json")

    def test_env_and_command(self):
        with mock.patch.dict(os.environ, {"CLAUDE_ROUTER_HOME": str(self.home)}):
            cmd, env = launch.build_launch(self.cfg(), ["--resume"], {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-old",
                                                                      "CLAUDE_CODE_USE_BEDROCK": "1"})
        self.assertEqual(cmd[-3:], ["--model", "haiku", "--resume"])
        self.assertEqual(env["CLAUDE_CODE_USE_FOUNDRY"], "1")
        self.assertEqual(env["ANTHROPIC_FOUNDRY_RESOURCE"], "test-resource")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-haiku-5-5")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5-5")
        self.assertEqual(env["CLAUDE_ROUTER"], "1")
        self.assertEqual(env["CLAUDE_CODE_MAX_RETRIES"], "3")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("ANTHROPIC_FOUNDRY_API_KEY", env)

    def test_custom_deployment_names_write_model_overrides(self):
        self.config["tiers"]["deep"]["deployment"] = "team-opus-prod"
        self.write_config()
        with mock.patch.dict(os.environ, {"CLAUDE_ROUTER_HOME": str(self.home)}):
            cmd, env = launch.build_launch(self.cfg(), [], {})
        settings = Path(cmd[cmd.index("--settings") + 1])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")),
                         {"modelOverrides": {"claude-opus-5-5": "team-opus-prod"}})
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-5-5")

    def test_no_haiku_deployment_maps_alias_to_next_tier(self):
        self.config["tiers"]["fast"]["deployment"] = ""
        self.write_config()
        cmd, env = launch.build_launch(self.cfg(), [], {})
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "claude-sonnet-5-5")

    def test_api_key_auth_requires_variable(self):
        self.config["foundry"]["auth"] = "api_key_env"
        self.write_config()
        with self.assertRaises(common.RouterError):
            launch.build_launch(self.cfg(), [], {})
        cmd, env = launch.build_launch(self.cfg(), [], {"FOUNDRY_API_KEY": "k-123"})
        self.assertEqual(env["ANTHROPIC_FOUNDRY_API_KEY"], "k-123")

    def test_resource_must_not_be_url(self):
        self.config["foundry"]["resource"] = "https://x.services.ai.azure.com"
        self.write_config()
        with self.assertRaises(common.RouterError):
            self.cfg()

    def test_dry_run_masks_secrets(self):
        self.config["foundry"]["auth"] = "api_key_env"
        self.write_config()
        env = dict(self.base_env, FOUNDRY_API_KEY="supersecretvalue", CLAUDE_CODE_UNRELATED_SETTING="keep-quiet",
                   ANTHROPIC_API_KEY="sk-old-key")
        p = subprocess.run([sys.executable, str(ROUTER / "launch.py"), "--router-dry-run"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("set     ANTHROPIC_FOUNDRY_API_KEY=***", p.stdout)
        self.assertIn("set     CLAUDE_CODE_USE_FOUNDRY=1", p.stdout)
        self.assertIn("removed ANTHROPIC_API_KEY", p.stdout)
        self.assertNotIn("supersecretvalue", p.stdout + p.stderr)
        self.assertNotIn("sk-old-key", p.stdout + p.stderr)
        self.assertNotIn("keep-quiet", p.stdout, "inherited, unchanged variables must not be printed")

    def test_review_posix_launch_execs_instead_of_wrapping(self):
        seen = {}
        rc = launch.run_claude(["claude", "--model", "haiku"], {"A": "1"}, windows=False,
                               execvpe=lambda f, a, e: seen.update(file=f, args=a, env=e))
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"file": "claude", "args": ["claude", "--model", "haiku"], "env": {"A": "1"}})

    def test_review_windows_launch_survives_ctrl_c(self):
        class FakeProc:
            def __init__(self):
                self.calls = 0
                self.killed = False

            def wait(self):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return 7

            def kill(self):
                self.killed = True
        proc = FakeProc()
        rc = launch.run_claude(["claude"], {}, popen=lambda cmd, env: proc, windows=True)
        self.assertEqual((rc, proc.calls, proc.killed), (7, 2, False))


class InstallerTests(RouterTestCase):
    def run_install(self, *args, home=None):
        uh = str(home or self.tmp / "userhome")
        env = dict(self.base_env, HOME=uh, USERPROFILE=uh)
        return subprocess.run([sys.executable, str(ROOT / "install.py")] + list(args), capture_output=True,
                              text=True, encoding="utf-8", timeout=120, env=env)

    def git(self, project, *args):
        return subprocess.run(["git", "-C", str(project)] + list(args), capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def run_installed_hook(self, settings_path, event, payload, **extra):
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        hook = [h for g in data["hooks"][event] for h in g["hooks"] if "router" in json.dumps(h)][0]
        p = subprocess.run([hook["command"]] + hook.get("args", []), input=json.dumps(payload), capture_output=True,
                           text=True, encoding="utf-8", timeout=60, env=self.env(**extra))
        return p.returncode, p.stdout.strip()

    def test_project_install_merges_and_is_idempotent(self):
        (self.home / "router.config.json").unlink()
        project = self.tmp / "proj"
        (project / ".claude").mkdir(parents=True)
        existing = {"permissions": {"allow": ["Bash(ls)"]},
                    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}
        (project / ".claude" / "settings.local.json").write_text(json.dumps(existing), encoding="utf-8")
        for _ in range(2):
            p = self.run_install("install", "--scope", "project", "--project", str(project))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"], existing["permissions"])
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(sum(1 for g in pre for h in g["hooks"] if "guard_hook.py" in json.dumps(h)), 1)
        self.assertTrue(any(h.get("command") == "echo mine" for g in pre for h in g["hooks"]))
        guard_group = [g for g in pre if "guard_hook.py" in json.dumps(g)][0]
        self.assertIn("Task", guard_group["matcher"].split("|"))
        judge = [h for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]][0]
        self.assertGreaterEqual(judge["timeout"], 90 + 45 + 30)
        self.assertEqual(judge["args"][0], "-c")
        self.assertIn((self.home / "router" / "judge_hook.py").as_posix(), judge["args"][1])
        for name in ("architect.md", "implementer.md", "deep-haiku.md"):
            self.assertTrue((project / ".claude" / "agents" / name).exists())
        self.assertTrue((self.home / "router.config.json").exists())
        self.assertTrue((self.home / "bin" / "claude-router.cmd").exists())

        p = self.run_install("uninstall", "--scope", "project", "--project", str(project))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = json.loads((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(data, existing)
        self.assertFalse((project / ".claude" / "agents" / "architect.md").exists())
        self.assertTrue((self.home / "router.config.json").exists())

    def test_invalid_existing_settings_aborts_without_changes(self):
        project = self.tmp / "proj2"
        (project / ".claude").mkdir(parents=True)
        bad = "{ this is not json"
        (project / ".claude" / "settings.local.json").write_text(bad, encoding="utf-8")
        p = self.run_install("install", "--scope", "project", "--project", str(project))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((project / ".claude" / "settings.local.json").read_text(encoding="utf-8"), bad)
        self.assertFalse((self.home / "router").exists())

    def test_wrong_shaped_hooks_abort_before_copying(self):
        uh = self.tmp / "uh-hooks"
        (uh / ".claude").mkdir(parents=True)
        (uh / ".claude" / "settings.json").write_text(json.dumps({"hooks": ["not", "an", "object"]}), encoding="utf-8")
        p = self.run_install("install", home=uh)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.home / "router").exists())

    def test_user_agent_files_are_not_overwritten(self):
        uh = self.tmp / "uh"
        (uh / ".claude" / "agents").mkdir(parents=True)
        mine = "---\nname: architect\ndescription: my own\n---\nmine\n"
        (uh / ".claude" / "agents" / "architect.md").write_text(mine, encoding="utf-8")
        p = self.run_install("install", home=uh)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((uh / ".claude" / "agents" / "architect.md").read_text(encoding="utf-8"), mine)
        self.assertIn("left unchanged", p.stdout)

    def test_doctor_flags_provider_env_in_settings(self):
        uh = self.tmp / "uh2"
        p = self.run_install("install", home=uh)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        s = json.loads((uh / ".claude" / "settings.json").read_text(encoding="utf-8"))
        s["env"] = {"CLAUDE_CODE_USE_FOUNDRY": "1"}
        (uh / ".claude" / "settings.json").write_text(json.dumps(s), encoding="utf-8")
        p = self.run_install("doctor", home=uh)
        self.assertIn("CLAUDE_CODE_USE_FOUNDRY", p.stdout)
        self.assertIn("claude --version: 9.9.9", p.stdout)
        self.assertIn("router hooks installed", p.stdout)

    def test_test_judge_command(self):
        p = subprocess.run([sys.executable, str(ROOT / "install.py"), "test-judge", "--backend", "all"],
                           capture_output=True, text=True, encoding="utf-8", timeout=120,
                           env=dict(self.base_env, FAKE_CLAUDE_MODE_SUBSCRIPTION="deep", FAKE_CLAUDE_MODE_FOUNDRY="crash"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)  # foundry failure must be reported
        self.assertIn("[ok]   subscription:sonnet", p.stdout)
        self.assertIn("[fail] foundry:sonnet", p.stdout)

    def test_report(self):
        self.judge("Change the storage schema", FAKE_CLAUDE_MODE="deep")
        self.judge("yes", FAKE_CLAUDE_MODE="fail_if_called")
        p = subprocess.run([sys.executable, str(ROOT / "install.py"), "report", "--last", "5"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60, env=self.base_env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("decisions: 2", p.stdout)
        self.assertIn("deep=2", p.stdout)

    # --- review regressions -------------------------------------------------------------------

    def test_review_uninstalling_one_scope_keeps_code_for_the_other(self):
        uh = self.tmp / "uh-multi"
        project = self.tmp / "projA"
        project.mkdir()
        self.assertEqual(self.run_install("install", home=uh).returncode, 0)
        self.assertEqual(self.run_install("install", "--scope", "project", "--project", str(project), home=uh).returncode, 0)
        p = self.run_install("uninstall", "--scope", "project", "--project", str(project), home=uh)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("router code kept", p.stdout)
        self.assertTrue((self.home / "router" / "judge_hook.py").exists())
        rc, out = self.run_installed_hook(uh / ".claude" / "settings.json", "UserPromptSubmit",
                                          {"session_id": "m", "prompt": "where is main?", "cwd": str(self.tmp)},
                                          FAKE_CLAUDE_MODE="fast")
        self.assertEqual(rc, 0)
        self.assertIn("FAST", out)
        p = self.run_install("uninstall", home=uh)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((self.home / "router").exists())

    def test_review_hooks_do_nothing_when_router_code_is_missing(self):
        uh = self.tmp / "uh-missing"
        self.assertEqual(self.run_install("install", home=uh).returncode, 0)
        shutil.rmtree(str(self.home / "router"))
        for event, payload in (("UserPromptSubmit", {"session_id": "m", "prompt": "x"}),
                               ("PreToolUse", {"session_id": "m", "tool_name": "Write", "tool_input": {"content": BIG}})):
            rc, out = self.run_installed_hook(uh / ".claude" / "settings.json", event, payload)
            self.assertEqual((rc, out), (0, ""), event)

    def test_review_same_named_user_hooks_survive(self):
        uh = self.tmp / "uh-names"
        (uh / ".claude").mkdir(parents=True)
        mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python ~/tools/guard_hook.py"}]}]}}
        (uh / ".claude" / "settings.json").write_text(json.dumps(mine), encoding="utf-8")
        self.assertEqual(self.run_install("install", home=uh).returncode, 0)
        self.assertEqual(self.run_install("install", home=uh).returncode, 0)
        self.assertEqual(self.run_install("uninstall", home=uh).returncode, 0)
        self.assertEqual(json.loads((uh / ".claude" / "settings.json").read_text(encoding="utf-8")), mine)

    def test_review_project_install_keeps_personal_files_out_of_git_and_repo(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        project = self.tmp / "repo"
        project.mkdir()
        self.assertEqual(self.git(project, "init", "-q").returncode, 0)
        (project / ".claude").mkdir()
        original = {"permissions": {"allow": ["Bash(ls)"]}}
        (project / ".claude" / "settings.local.json").write_text(json.dumps(original), encoding="utf-8")
        for _ in range(2):  # twice within the same second
            self.assertEqual(self.run_install("install", "--scope", "project", "--project", str(project)).returncode, 0)
        status = self.git(project, "status", "--porcelain", "--untracked-files=all").stdout
        self.assertEqual(status.strip(), "", "nothing from the router may show up as committable")
        self.assertEqual(list((project / ".claude").glob("*backup*")), [])
        backups = list((self.home / "backups").iterdir())
        self.assertEqual(len(backups), 2, "two installs in the same second must not overwrite each other's backup")
        self.assertIn(original, [json.loads(b.read_text(encoding="utf-8")) for b in backups])
        self.assertEqual(self.run_install("uninstall", "--scope", "project", "--project", str(project)).returncode, 0)
        exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertNotIn("claude-model-router", exclude)

    def test_review_launchers_have_exact_line_endings(self):
        self.assertEqual(self.run_install("install", home=self.tmp / "uh-nl").returncode, 0)
        sh = (self.home / "bin" / "claude-router").read_bytes()
        cmd = (self.home / "bin" / "claude-router.cmd").read_bytes()
        self.assertNotIn(b"\r", sh)
        self.assertTrue(sh.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r\r", cmd)
        self.assertTrue(cmd.endswith(b"%*\r\n"))

    def test_review_wrong_shaped_registry_does_not_crash(self):
        uh = self.tmp / "uh-reg"
        for bad in ("[1,2]", '{"installs": "x"}', '{"installs": [5, {"settings": 3}]}', "not json"):
            (self.home / "installs.json").write_text(bad, encoding="utf-8")
            p = self.run_install("install", home=uh)
            self.assertEqual(p.returncode, 0, bad + p.stdout + p.stderr)
            p = self.run_install("uninstall", home=uh)
            self.assertEqual(p.returncode, 0, bad + p.stdout + p.stderr)


class BundleTests(unittest.TestCase):
    def test_offline_bundle_appendix_rebuilds_identical_files(self):
        import hashlib, re, zipfile
        out = Path(tempfile.mkdtemp(prefix="router-bundle-"))
        try:
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_bundle.py"), "--out", str(out)],
                               capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            md = (out / "SETUP-offline.md").read_text(encoding="utf-8")
            fence = re.escape("`" * 6)
            blocks = re.findall(r"### `([^`]+)`\n\n" + fence + r"[a-z]*\n(.*?)\n" + fence, md, re.S)
            table = dict(re.findall(r"\| `([^`]+)` \| `([0-9a-f]{64})` \|", md))
            self.assertGreaterEqual(len(blocks), 15)
            for name, body in blocks:
                real = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                self.assertEqual(hashlib.sha256((body + "\n").encode("utf-8")).hexdigest(), real, name)
                self.assertEqual(table.get(name), real, name)
            zips = list(out.glob("claude-model-router-*.zip"))
            self.assertEqual(len(zips), 1)
            names = zipfile.ZipFile(str(zips[0])).namelist()
            self.assertTrue(any(n.endswith("/install.py") for n in names) and any(n.endswith("/SETUP.md") for n in names))
        finally:
            shutil.rmtree(str(out), ignore_errors=True)


class SourceRules(unittest.TestCase):
    def test_review_no_platform_dependent_text_writes(self):
        # Path.write_text translates "\n" to "\r\n" on Windows, which breaks the sh launcher and doubles
        # the .cmd line endings. All file writes must go through write_exact / atomic_write_text.
        for path in list(ROUTER.glob("*.py")) + [ROOT / "install.py"]:
            self.assertNotIn(".write_text(", path.read_text(encoding="utf-8"), path.name)

    def test_every_text_subprocess_pins_encoding(self):
        for path in list(ROUTER.glob("*.py")) + [ROOT / "install.py"]:
            src = path.read_text(encoding="utf-8")
            for chunk in src.split("subprocess.run(")[1:]:
                call = chunk.split(")\n", 1)[0]
                if "text=True" in call:
                    self.assertIn("encoding=", call, "%s: subprocess.run with text=True must pin encoding" % path.name)


if __name__ == "__main__":
    unittest.main()
