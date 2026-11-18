"""End-to-end proof: the REAL Claude Code CLI + the installed router + a local mock Anthropic/Foundry API.

    python tests/e2e/run_e2e.py [--claude PATH] [--keep] [--only deep,fast,...]

What it does, with no model, account or network:
  1. installs the router into a throw-away home folder with `install.py install`;
  2. points the router's Foundry settings at tests/e2e/mock_anthropic.py on 127.0.0.1;
  3. runs `claude-router -p "..."` (the installed launcher -> real `claude`) through scripted conversations;
  4. checks what actually reached the "API" and the disk: judge call, verdict injected by the hook,
     edits denied/allowed by the guard, which deployment each subagent used, the 429 fallback,
     and that missing router code never blocks Claude Code.

Child processes get a minimal allow-listed environment, so no real credential of the machine running
the test (subscription login, API keys, cloud tokens) can be used by accident.
Exit code 0 = every check passed. A Markdown report goes to stdout and to $GITHUB_STEP_SUMMARY.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
IS_WINDOWS = os.name == "nt"
KEEP_VARS = {k.upper() for k in (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "USERNAME", "USER", "LOGNAME", "SHELL", "TERM", "CLAUDE_CODE_GIT_BASH_PATH")}
DEPLOYMENTS = {"fast": "haiku-dep", "standard": "sonnet-dep", "deep": "opus-dep"}
MODEL_IDS = {"fast": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "deep": "claude-opus-5-5"}


class Run:
    def __init__(self, args):
        self.args = args
        self.tmp = Path(tempfile.mkdtemp(prefix="router-e2e-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.router_home = self.home / ".claude-router"
        self.claude = args.claude or shutil.which("claude")
        if not self.claude:
            sys.exit("claude CLI not found; pass --claude")
        self.checks = []          # (scenario, ok, title, detail)
        self.notes = []

    # ------------------------------------------------------------------ helpers
    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k.upper() in KEEP_VARS}
        e.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8", "MOCK_FOUNDRY_KEY": "dummy-key-for-the-local-mock",
        })
        if self.args.debug_hooks:
            e["CLAUDE_ROUTER_DEBUG_DIR"] = str(self.tmp / "hook-inputs")
        e.update(extra)
        return e

    def check(self, scenario, ok, title, detail=""):
        self.checks.append((scenario, bool(ok), title, detail))
        return bool(ok)

    def run(self, cmd, env, cwd=None, timeout=300):
        p = subprocess.run([str(c) for c in cmd], env=env, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p

    def start_mock(self, name, **mock_env):
        log = self.tmp / ("mock-%s.jsonl" % name)
        port_file = self.tmp / ("mock-%s.port" % name)
        work = self.tmp / ("work-%s" % name)
        work.mkdir()
        env = self.env(MOCK_LOG=str(log), MOCK_PORT_FILE=str(port_file), MOCK_WORKDIR=str(work), **mock_env)
        proc = subprocess.Popen([sys.executable, str(HERE / "mock_anthropic.py")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while not port_file.exists() and time.time() < deadline:
            time.sleep(0.1)
        if not port_file.exists():
            proc.kill()
            sys.exit("mock server did not start")
        return proc, int(port_file.read_text(encoding="utf-8")), log, work

    @staticmethod
    def records(log):
        if not log.exists():
            return []
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def write_config(self, port, chain=None):
        cfg = json.loads((self.router_home / "router.config.json").read_text(encoding="utf-8"))
        cfg["foundry"].update({"resource": "", "base_url": "http://127.0.0.1:%d/anthropic" % port,
                               "auth": "api_key_env", "api_key_env": "MOCK_FOUNDRY_KEY"})
        for tier in ("fast", "standard", "deep"):
            cfg["tiers"][tier] = {"model_id": MODEL_IDS[tier], "deployment": DEPLOYMENTS[tier]}
        cfg["judge"]["claude_path"] = self.claude
        cfg["judge"]["include_git_signals"] = False
        default_chain = [{"backend": "foundry", "model": "sonnet", "effort": "high", "timeout_s": 90}, {"backend": "rules"}]
        if self.args.subscription_judge == "expect-fail":
            default_chain.insert(0, {"backend": "subscription", "model": "sonnet", "effort": "high", "timeout_s": 60})
        cfg["judge"]["chain"] = chain or default_chain
        (self.router_home / "router.config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def launcher(self):
        b = self.router_home / "bin"
        return [b / "claude-router.cmd"] if IS_WINDOWS else [b / "claude-router"]

    def claude_args(self, prompt):
        return ["-p", prompt, "--permission-mode", "acceptEdits", "--output-format", "json", "--max-turns", "15"]

    def save_output(self, name, p):
        (self.tmp / ("claude-%s.out.txt" % name)).write_text(
            "exit %s\n--- stdout ---\n%s\n--- stderr ---\n%s\n" % (p.returncode, p.stdout, p.stderr), encoding="utf-8")

    def decisions(self):
        p = self.router_home / "logs" / "decisions.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def judged_decision(self, since):
        """First decision of this scenario that came from the judge chain (not a notification/sticky)."""
        rows = [r for r in self.decisions()[since:] if r.get("source") not in ("notification", "sticky")]
        return rows[0] if rows else {}

    @staticmethod
    def result_text(p):
        try:
            return json.loads(p.stdout.strip().splitlines()[-1]).get("result")
        except (ValueError, IndexError, AttributeError):
            return None

    @staticmethod
    def find_result(recs, tool, subagent_type=None):
        for r in recs:
            for t in r.get("tool_results") or []:
                if t.get("tool") == tool and (subagent_type is None or t.get("subagent_type") == subagent_type):
                    return t
        return None

    # ------------------------------------------------------------------ scenarios
    def install(self):
        p = self.run([sys.executable, ROOT / "install.py", "install"], self.env())
        self.check("install", p.returncode == 0, "`install.py install` into a clean home folder",
                   (p.stdout + p.stderr).strip().splitlines()[-1] if p.returncode else "")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        self.check("install", "UserPromptSubmit" in hooks and "PreToolUse" in hooks,
                   "hooks written to ~/.claude/settings.json")

    def scenario_deep(self):
        s = "DEEP"
        mock, port, log, work = self.start_mock("deep", MOCK_VERDICT="deep")
        since = len(self.decisions())
        try:
            self.write_config(port)
            p = self.run(self.launcher() + self.claude_args("[[e2e:deep]] Change the storage schema and migrate old data"),
                         self.env(), cwd=work)
            self.save_output("deep", p)
        finally:
            mock.kill()
        recs = self.records(log)
        judge = [r for r in recs if r.get("kind") == "judge"]
        main = [r for r in recs if r.get("actor") == "main"]
        sub = [r for r in recs if r.get("actor") == "subagent"]
        dec = self.judged_decision(since)
        self.check(s, p.returncode == 0 and self.result_text(p) == "main done", "real `claude` ran the whole scripted conversation",
                   "exit %s, result %r" % (p.returncode, self.result_text(p)))
        if self.args.subscription_judge == "expect-fail":
            sub_attempt = (dec.get("attempts") or [{}])[0]
            self.check(s, sub_attempt.get("step") == "subscription:sonnet" and not sub_attempt.get("ok"),
                       "subscription judge ran with every credential stripped, failed (no login on this machine) "
                       "and the chain moved on", (sub_attempt.get("error") or "")[:160])
        self.check(s, judge and judge[0]["model"] == DEPLOYMENTS["standard"],
                   "Foundry judge reached the API on the Sonnet deployment (modelOverrides applied)",
                   "model=%s" % (judge[0]["model"] if judge else None))
        self.check(s, judge and not judge[0]["system_has_default_prompt"] and not judge[0]["tools"],
                   "judge ran with judge_system.md instead of Claude Code's default system prompt, and with no tools",
                   "system prompt %s chars, tools=%s" % (judge[0]["system_len"], judge[0]["tools"]) if judge else "")
        self.check(s, dec.get("tier") == "deep" and dec.get("source") == "foundry:sonnet",
                   "router logged verdict DEEP from foundry:sonnet", "%s / %s" % (dec.get("tier"), dec.get("source")))
        self.check(s, len(judge) == 1, "judge called exactly once: the background-agent notification was not re-judged",
                   "%d judge request(s)" % len(judge))
        self.check(s, main and all(r["model"] == DEPLOYMENTS["fast"] for r in main),
                   "main session ran on the Haiku deployment", "models=%s" % sorted({r["model"] for r in main}))
        ctx = next((r["router_context"] for r in main if r.get("router_context")), None)
        self.check(s, ctx and "Verdict for this prompt: DEEP" in ctx,
                   "UserPromptSubmit hook injected the DEEP directive into Haiku's context", (ctx or "")[:110])
        w = self.find_result(main, "Write")
        self.check(s, w and "[router]" in w["text"] and not (work / "main_big.txt").exists(),
                   "guard DENIED Haiku's own 50-line Write under DEEP (file not created)", (w or {}).get("text", "")[:140])
        impl = self.find_result(main, "Agent", "implementer") or self.find_result(main, "Task", "implementer")
        self.check(s, impl and "verdict for this prompt is DEEP" in impl["text"],
                   "guard DENIED delegating DEEP work to `implementer`", (impl or {}).get("text", "")[:140])
        self.check(s, sub and all(r["model"] == DEPLOYMENTS["deep"] for r in sub),
                   "`architect` subagent ran on the Opus deployment", "models=%s" % sorted({r["model"] for r in sub}))
        f = work / "arch_big.txt"
        self.check(s, f.exists() and len(f.read_text(encoding="utf-8").splitlines()) == 200,
                   "`architect` was ALLOWED its 200-line Write (file created)")

    def scenario_fast(self):
        s = "FAST"
        mock, port, log, work = self.start_mock("fast", MOCK_VERDICT="fast")
        try:
            self.write_config(port)
            p = self.run(self.launcher() + self.claude_args("[[e2e:fast]] Where is the configuration loaded?"),
                         self.env(), cwd=work)
            self.save_output("fast", p)
        finally:
            mock.kill()
        recs = self.records(log)
        main = [r for r in recs if r.get("actor") == "main"]
        ctx = next((r["router_context"] for r in main if r.get("router_context")), None)
        self.check(s, p.returncode == 0, "real `claude` finished", "exit %s" % p.returncode)
        self.check(s, ctx and "Verdict for this prompt: FAST" in ctx, "FAST directive injected", (ctx or "")[:110])
        self.check(s, (work / "fast_small.txt").exists(), "guard ALLOWED a 5-line Write within the FAST budget (40)")
        full = max(main, key=lambda r: len(r.get("tool_results") or [])) if main else {}
        results = [t for t in full.get("tool_results") or [] if t.get("tool") == "Write"]
        big = results[1] if len(results) > 1 else None
        self.check(s, big and "[router]" in big["text"] and not (work / "fast_big.txt").exists(),
                   "guard DENIED the next 100-line Write (budget exceeded, file not created)", (big or {}).get("text", "")[:140])

    def scenario_throttle(self):
        s = "429 fallback"
        mock, port, log, work = self.start_mock("throttle", MOCK_VERDICT="deep", MOCK_THROTTLE_MODEL=DEPLOYMENTS["deep"])
        try:
            self.write_config(port)
            p = self.run(self.launcher() + self.claude_args("[[e2e:throttle]] Redesign the export locking"),
                         self.env(), cwd=work)
            self.save_output("throttle", p)
        finally:
            mock.kill()
        recs = self.records(log)
        main = [r for r in recs if r.get("actor") == "main"]
        throttled = [r for r in recs if r.get("response") == "429"]
        self.check(s, p.returncode == 0, "real `claude` finished", "exit %s" % p.returncode)
        self.check(s, throttled, "the Opus deployment answered HTTP 429 to `architect`",
                   "%d throttled request(s); retries capped by the launcher" % len(throttled))
        arch = self.find_result(main, "Agent", "architect") or self.find_result(main, "Task", "architect")
        self.notes.append("What Claude Code reported to Haiku for the throttled `architect`: %r"
                          % ((arch or {}).get("text", "")[:300]))
        fb = self.find_result(main, "Agent", "deep-haiku") or self.find_result(main, "Task", "deep-haiku")
        self.check(s, fb is not None and "[router]" not in fb["text"],
                   "guard ALLOWED `deep-haiku` because the transcript shows the 429", (fb or {}).get("text", "")[:140])
        self.check(s, (work / "fallback.txt").exists(), "`deep-haiku` did the work (file created)")
        fb_models = sorted({r["model"] for r in recs if r.get("actor") == "subagent" and r.get("response") != "429"})
        self.check(s, fb_models == [DEPLOYMENTS["fast"]], "`deep-haiku` ran on the Haiku deployment", "models=%s" % fb_models)

    def scenario_unaffected(self):
        s = "safety"
        mock, port, log, work = self.start_mock("plain", MOCK_VERDICT="deep")
        first_party = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % port, "ANTHROPIC_API_KEY": "dummy-key-for-the-local-mock"}
        try:
            p = self.run([self.claude] + self.claude_args("[[e2e:fast]] plain session"), self.env(**first_party), cwd=work)
            self.save_output("plain", p)
            plain_recs = self.records(log)
            plain_ok = p.returncode == 0 and (work / "fast_big.txt").exists()
            self.check(s, plain_ok and not any(r.get("router_context") for r in plain_recs),
                       "plain `claude` (not started by the launcher) is untouched: no verdict, no limits",
                       "exit %s" % p.returncode)
            # Delete the router code: hooks must do nothing instead of blocking every prompt and edit.
            shutil.move(str(self.router_home / "router"), str(self.tmp / "router-moved-away"))
            for f in ("fast_small.txt", "fast_big.txt"):
                if (work / f).exists():
                    (work / f).unlink()
            p = self.run([self.claude] + self.claude_args("[[e2e:fast]] router code missing"),
                         self.env(CLAUDE_ROUTER="1", **first_party), cwd=work)
            self.save_output("missing-code", p)
            self.check(s, p.returncode == 0 and (work / "fast_big.txt").exists(),
                       "with the router code deleted, hooks do nothing and Claude Code is NOT blocked",
                       "exit %s" % p.returncode)
        finally:
            mock.kill()
            moved = self.tmp / "router-moved-away"
            if moved.exists() and not (self.router_home / "router").exists():
                shutil.move(str(moved), str(self.router_home / "router"))

    # ------------------------------------------------------------------ report
    def report(self):
        ver = self.run([self.claude, "--version"], self.env(), timeout=60).stdout.strip()
        lines = ["## model-router end-to-end proof", "",
                 "Real Claude Code `%s` on %s, Python %s, driven against a local mock API (no model, no account, "
                 "no network)." % (ver, sys.platform, sys.version.split()[0]), "",
                 "| Scenario | Result | Check | Evidence |", "|---|---|---|---|"]
        for scen, ok, title, detail in self.checks:
            lines.append("| %s | %s | %s | %s |" % (scen, "PASS" if ok else "**FAIL**", title,
                                                   str(detail).replace("|", "/").replace("\n", " ")[:160]))
        passed = sum(1 for c in self.checks if c[1])
        lines += ["", "**%d / %d checks passed.**" % (passed, len(self.checks))]
        if self.notes:
            lines += [""] + ["- " + n.replace("\n", " ") for n in self.notes]
        text = "\n".join(lines) + "\n"
        sys.stdout.write(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(text)
        out = self.args.artifacts
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            (Path(out) / "e2e-report.md").write_text(text, encoding="utf-8")
            for f in list(self.tmp.glob("mock-*.jsonl")) + list(self.tmp.glob("claude-*.out.txt")) + \
                    list((self.tmp / "hook-inputs").glob("*.json")):
                shutil.copy2(str(f), str(Path(out) / f.name))
            dec = self.router_home / "logs" / "decisions.jsonl"
            if dec.exists():
                shutil.copy2(str(dec), str(Path(out) / "router-decisions.jsonl"))
        return passed == len(self.checks)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claude")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    ap.add_argument("--only", default="deep,fast,throttle,safety")
    ap.add_argument("--artifacts", help="copy the report, mock logs and claude output here")
    ap.add_argument("--debug-hooks", action="store_true", help="save every hook input (CLAUDE_ROUTER_DEBUG_DIR)")
    ap.add_argument("--subscription-judge", choices=("expect-fail", "skip"), default="expect-fail",
                    help="expect-fail (CI: no login exists, the step must fail and be skipped) or skip (machines that "
                         "have a Claude login, so the test never makes a real model call)")
    args = ap.parse_args()
    r = Run(args)
    try:
        r.install()
        wanted = set(args.only.split(","))
        for name, fn in (("deep", r.scenario_deep), ("fast", r.scenario_fast),
                         ("throttle", r.scenario_throttle), ("safety", r.scenario_unaffected)):
            if name in wanted:
                try:
                    fn()
                except subprocess.TimeoutExpired as exc:
                    r.check(name, False, "scenario finished in time", str(exc)[:160])
        ok = r.report()
    finally:
        if args.keep:
            sys.stdout.write("kept %s\n" % r.tmp)
        else:
            shutil.rmtree(str(r.tmp), ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
