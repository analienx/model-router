"""UserPromptSubmit hook: judge how hard the prompt is and tell Haiku where to route it.

Flow per prompt:
  1. `::fast` / `::standard` / `::deep` prefix         -> user override, no judge call
  2. a slash command (`/review`, not `/src/file.py`)     -> keep the previous verdict (or standard)
  3. an exact follow-up phrase ("yes", "continue")       -> keep the previous verdict
  4. an escalation phrase ("still failing", "try again") -> previous verdict + one tier
  5. judge chain from router.config.json                 -> subscription Sonnet, then Foundry, then rules
A background-agent <task-notification> is not a new prompt: it keeps the verdict AND the edit counters.
The verdict is saved per session for guard_hook.py and injected into Claude's context.
The hook never blocks the prompt and never crashes the session: any unexpected error
still produces a STANDARD verdict, a fresh state and a directive.
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
import common  # noqa: E402  (same directory)
import ui  # noqa: E402

JUDGE_INSTRUCTION = "Classify the coding task given on standard input. Reply with the JSON object only."

OVERRIDE_RE = re.compile(r"^\s*::(fast|standard|std|deep)\b", re.IGNORECASE)
# A slash command is "/name" followed by whitespace or the end; "/src/app.py ..." is a path, not a command.
SLASH_RE = re.compile(r"^/[A-Za-z][\w:.-]*(\s|$)")
TIER_SYNONYMS = {
    "fast": "fast", "easy": "fast", "simple": "fast", "trivial": "fast", "low": "fast", "haiku": "fast",
    "standard": "standard", "std": "standard", "medium": "standard", "moderate": "standard", "sonnet": "standard",
    "deep": "deep", "hard": "deep", "complex": "deep", "high": "deep", "opus": "deep",
}

DEEP_PATTERNS = [
    r"\barchitect", r"\bdesign\b", r"\bschema", r"\bmigrat", r"\bsecurity\b", r"\bauth",
    r"\bencrypt", r"\bcrypto", r"\bpermission", r"\brace condition", r"\bconcurren", r"\bdeadlock",
    r"\bthread", r"\bperformance\b", r"\bmemory leak", r"\bflaky\b", r"\bintermittent", r"\broot cause",
    r"\bdata loss", r"\bcorrupt", r"\bbreaking change", r"\binterface change", r"\bapi change",
    r"\bstill (fails|failing|broken)", r"\bdoesn'?t work again\b",
]
FAST_PATTERNS = [
    r"^\s*(what|where|which|who|when|why is|how do i|how does|explain|show|list|find|summari[sz]e|describe)\b",
]


def classify_with_rules(prompt):
    text = prompt.lower()
    for pat in DEEP_PATTERNS:
        if re.search(pat, text):
            return {"tier": "deep", "confidence": None, "reason": "rules: matched '%s'" % pat.replace("\\b", "")}
    if len(text) < 200:
        for pat in FAST_PATTERNS:
            if re.search(pat, text):
                return {"tier": "fast", "confidence": None, "reason": "rules: short question"}
    return {"tier": "standard", "confidence": None, "reason": "rules: no clear signal, routed up to standard"}


def normalize_phrase(text):
    t = re.sub(r"\s+", " ", text.strip().lower())
    return re.sub(r"[\s.!?,;:)]+$", "", t)


def is_escalation(norm, phrases):
    if len(norm) > 80:
        return False
    for p in phrases:
        p = normalize_phrase(p)
        if p and (norm == p or norm.startswith(p + " ") or norm.startswith(p + ",")):
            return True
    return False


def parse_verdict(text):
    """Find the first JSON object with a valid `tier` in model output. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("no text")
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "tier" in obj:
            tier = TIER_SYNONYMS.get(str(obj.get("tier", "")).strip().lower())
            if not tier:
                raise ValueError("unknown tier %r" % obj.get("tier"))
            conf = obj.get("confidence")
            conf = max(0.0, min(1.0, float(conf))) if common.is_number(conf) else None
            reason = obj.get("reason")
            reason = reason.strip()[:300] if isinstance(reason, str) else ""
            return {"tier": tier, "confidence": conf, "reason": reason}
        idx = text.find("{", idx + 1)
    raise ValueError("no JSON verdict found")


def git_signals(cwd):
    def run(args):
        try:
            p = subprocess.run(["git", "--no-optional-locks", "-C", cwd] + args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3)
            return p.stdout.strip() if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch is None:
        return "not a git repository"
    status = run(["status", "--porcelain"]) or ""
    changed = len([ln for ln in status.splitlines() if ln.strip()])
    return "branch %s, %d uncommitted file(s)" % (branch, changed)


def build_payload(prompt, cwd, prev, cfg):
    limit = cfg["judge"]["max_prompt_chars"]
    task = prompt if len(prompt) <= limit else prompt[:limit] + "\n[... truncated ...]"
    lines = ["<task>", task, "</task>", "<context>"]
    lines.append("repository folder: %s" % (os.path.basename(cwd.rstrip("/\\")) if cwd else "unknown"))
    if cfg["judge"]["include_git_signals"] and cwd:
        lines.append("git: %s" % git_signals(cwd))
    if prev.get("tier") in common.TIERS:
        # Only the tier, never the previous reason: that text may quote an earlier prompt, possibly one that was
        # judged by a different provider.
        lines.append("previous verdict in this session: %s" % prev["tier"])
    else:
        lines.append("previous verdict in this session: none")
    lines.append("</context>")
    return "\n".join(lines) + "\n"


def kill_tree(proc):
    """Kill the judge process and everything it started (npm .cmd shims start node as a grandchild)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_bounded(cmd, payload, env, cwd, timeout):
    """Run cmd with payload on stdin; return (rc, stdout, stderr). Raises RuntimeError on timeout.

    stdin/stdout/stderr are temporary files, not pipes, so a grandchild that keeps a handle
    open cannot make us wait past the timeout.
    """
    with tempfile.TemporaryFile() as fin, tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        fin.write(payload.encode("utf-8"))
        fin.flush()
        fin.seek(0)
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, env=env, cwd=cwd, **kwargs)
        except OSError as exc:
            raise RuntimeError("could not start claude: %s" % exc)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise RuntimeError("timed out after %ss" % timeout)
        fout.seek(0)
        ferr.seek(0)
        return rc, fout.read().decode("utf-8", "replace"), ferr.read().decode("utf-8", "replace")


def call_claude(step, payload, cfg, environ=None):
    """Run one `claude -p` judge call. Returns (verdict, cost_usd). Raises RuntimeError."""
    environ = os.environ if environ is None else environ
    backend = step["backend"]
    try:
        if backend == "subscription":
            env = common.subscription_env(environ)
            extra = []
        else:
            env = common.judge_env(common.foundry_env(cfg, environ))
            extra = ["--bare"]  # bare mode keeps reading Foundry credentials and skips hooks, CLAUDE.md, plugins
        settings = common.judge_settings_file(cfg, backend)
        cmd = common.resolve_claude_cmd(cfg) + [
            "-p", JUDGE_INSTRUCTION,
            "--model", common.judge_model_arg(step["model"]),
            "--output-format", "json",
            "--max-turns", "1",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "project,local",
            "--settings", str(settings),
            "--disallowedTools", "*",
            "--system-prompt-file", str(common.judge_system_prompt_path()),
        ] + extra
        if step.get("effort"):
            cmd += ["--effort", step["effort"]]
        work = common.router_home() / "judge-cwd"
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError("could not prepare the judge call: %s" % exc)
    rc, out, err = run_bounded(cmd, payload, env, str(work), float(step.get("timeout_s", 60)))
    out = out.strip()
    text, cost, obj = out, None, None
    try:
        obj = json.loads(out)
    except ValueError:
        obj = None
    if isinstance(obj, list):
        obj = next((o for o in reversed(obj) if isinstance(o, dict) and o.get("type") == "result"), None)
    if isinstance(obj, dict) and "tier" in obj and "result" not in obj:
        obj = None  # the verdict itself was printed without the result wrapper; parse `out` as text
    if isinstance(obj, dict):
        if obj.get("is_error"):
            raise RuntimeError("claude reported an error: %s" % str(obj.get("result") or obj.get("subtype"))[:300])
        text = obj.get("result") if isinstance(obj.get("result"), str) else ""
        c = obj.get("total_cost_usd")
        cost = float(c) if common.is_number(c) else None
    if rc != 0 and not text:
        raise RuntimeError("claude exited %d: %s" % (rc, err.strip()[:300]))
    try:
        return parse_verdict(text), cost
    except ValueError as exc:
        raise RuntimeError("unusable judge output (%s): %s" % (exc, text[:200].replace("\n", " ")))


NOTIF_ID_RE = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")
NOTIF_STATUS_RE = re.compile(r"<status>\s*([^<]+?)\s*</status>")


def throttled_call_id(prompt, state):
    """tool_use_id of a pinned agent call of THIS prompt that the notification reports as failed with 429/529.

    The notification must say <status>failed</status>: a successful agent whose summary mentions "rate limit"
    is not evidence. pinned_calls holds only the current prompt's calls, so an older call never counts.
    """
    m = NOTIF_ID_RE.search(prompt)
    if not m or m.group(1) not in state.get("pinned_calls", []):
        return None
    st = NOTIF_STATUS_RE.search(prompt)
    if not st or st.group(1).lower() not in common.FAILED_STATUSES:
        return None
    return m.group(1) if common.THROTTLE_RE.search(common.notification_error_text(prompt)) else None


def is_notification(prompt):
    """Claude Code delivers background-agent results as a prompt starting with <task-notification>."""
    return prompt.lstrip().startswith("<task-notification>")


def _shortcut(tier, source, reason, confidence=None):
    return {"tier": tier, "source": source, "reason": reason, "confidence": confidence,
            "attempts": [], "cost_usd": None}


def decide(prompt, cwd, prev, cfg, environ=None):
    """Return a decision dict: tier, source, reason, confidence, attempts, cost_usd."""
    if is_notification(prompt):
        has = prev.get("tier") in common.TIERS
        return _shortcut(prev["tier"] if has else "standard", "notification",
                         "background agent notification: keeps the current verdict")
    m = OVERRIDE_RE.match(prompt)
    if m:
        return _shortcut(TIER_SYNONYMS[m.group(1).lower()], "override", "user override ::%s" % m.group(1).lower(), 1.0)
    has_prev = prev.get("tier") in common.TIERS
    stripped = prompt.strip()
    if SLASH_RE.match(stripped):
        return _shortcut(prev["tier"] if has_prev else "standard", "slash-command",
                         "slash command: keeps the previous verdict (or standard)")
    norm = normalize_phrase(stripped)
    if is_escalation(norm, cfg["judge"]["escalate_phrases"]):
        base = prev["tier"] if has_prev else "fast"
        return _shortcut(common.bump_tier(base), "escalation",
                         "follow-up says the last attempt failed: %s -> one tier up" % (prev.get("tier") or "none"))
    if has_prev and norm in {normalize_phrase(p) for p in cfg["judge"]["sticky_phrases"]}:
        return _shortcut(prev["tier"], "sticky", "follow-up keeps previous verdict", prev.get("confidence"))

    attempts = []
    payload = build_payload(prompt, cwd, prev, cfg)
    total_cost = None
    for step in cfg["judge"]["chain"]:
        label = step["backend"] if step["backend"] == "rules" else "%s:%s" % (step["backend"], step["model"])
        t0 = time.time()
        if step["backend"] == "rules":
            verdict = classify_with_rules(prompt)
            attempts.append({"step": label, "ok": True, "ms": int((time.time() - t0) * 1000)})
            return dict(verdict, source="rules", attempts=attempts, cost_usd=total_cost)
        try:
            verdict, cost = call_claude(step, payload, cfg, environ)
        except Exception as exc:  # any failure of one judge moves on to the next one
            attempts.append({"step": label, "ok": False, "ms": int((time.time() - t0) * 1000),
                             "error": ("%s" % exc)[:300] or exc.__class__.__name__})
            continue
        attempts.append({"step": label, "ok": True, "ms": int((time.time() - t0) * 1000)})
        if cost is not None:
            total_cost = (total_cost or 0.0) + cost
        decision = dict(verdict, source=label, attempts=attempts, cost_usd=total_cost)
        conf = verdict.get("confidence")
        if conf is not None and conf < cfg["judge"]["min_confidence"] and verdict["tier"] != "deep":
            decision["reason"] = "%s (confidence %.2f below %.2f, routed up from %s)" % (
                verdict["reason"], conf, cfg["judge"]["min_confidence"], verdict["tier"])
            decision["tier"] = common.bump_tier(verdict["tier"])
        return decision
    # Chain without a rules step and every model failed: route up.
    return {"tier": "standard", "source": "none", "reason": "every judge failed; routed up to standard",
            "confidence": None, "attempts": attempts, "cost_usd": total_cost}


def _budget_text(budget):
    if budget == 0:
        return "Main-thread file edits are blocked this turn."
    return "Main-thread file edits are limited to %d changed lines in total this turn." % budget


def directive(tier, decision, cfg):
    btxt = _budget_text(cfg["guard"]["main_edit_budget"][tier])
    conf = decision.get("confidence")
    conf_txt = "" if conf is None else ", confidence %.2f" % conf
    head = "[router] Verdict for this prompt: %s (source: %s%s). Reason: %s" % (
        tier.upper(), decision.get("source"), conf_txt, decision.get("reason") or "-")
    if decision.get("tier") != tier:
        head += " Note: no Foundry deployment for %s, using %s instead." % (decision.get("tier"), tier)
    failed = [a for a in decision.get("attempts", []) if not a.get("ok")]
    if failed:
        head += " Judge problems: " + "; ".join("%s: %s" % (a["step"], a.get("error", "")) for a in failed)[:400]
    if tier == "fast":
        body = ("Handle this yourself. %s If the work grows beyond that, delegate it to the `implementer` "
                "subagent." % btxt)
    elif tier == "standard":
        body = ("Delegate the implementation to the `implementer` subagent (or `architect` if it turns out to need "
                "design work) with a self-contained brief (goal, relevant files, constraints, how to verify). You may "
                "read and search first to write the brief. " + btxt)
    else:
        body = ("Before answering or editing, delegate to the `architect` subagent with a self-contained brief "
                "(goal, relevant files, constraints, what has been tried, how to verify). You may read and search first "
                "to write the brief. `implementer` is not accepted for this verdict. " + btxt)
    rules = ("Rules: never pass `model` on an Agent call to `architect` or `implementer`, and never use "
             "`general-purpose` or a fork for this work (they run on your own model); research agents such as `Explore` "
             "are fine for reading. `deep-haiku` is accepted only after `architect` or `implementer` failed in this "
             "prompt with a rate-limit (429) or overloaded (529) error; then run the "
             "same brief with `deep-haiku` and tell the user the result came from the fallback. Subagents cannot see "
             "this conversation. Report subagent results honestly, including failures and test output. The user can "
             "override the router by starting a prompt with ::fast, ::standard or ::deep.")
    return "\n".join([head, body, rules])


def user_line(tier, decision, cfg, ms):
    """The verdict line Claude Code shows the user, or None. Notifications stay quiet unless they unlock the fallback."""
    if not cfg["ui"]["verdict_line"]:
        return None
    try:
        if decision.get("source") == "notification":
            if "now accepted" not in (decision.get("reason") or ""):
                return None
            return "%s router: fallback unlocked %s `deep-haiku` is accepted (an architect/implementer call hit " \
                   "429/529)" % (ui.BOX_UNICODE["dot"], ui.BOX_UNICODE["sep"])
        return ui.verdict_line(tier, decision, cfg, ms)
    except Exception:
        return None


ROUTER_SOURCES = ("override", "sticky", "escalation", "slash-command", "notification", "rules", "none", "error")


def error_kind(message):
    """A short, prompt-free category for a judge error (the full text is logged only with log_judge_details)."""
    m = str(message or "")
    for prefix, kind in (("timed out", "timeout"), ("claude reported an error", "claude-error"),
                         ("claude exited", "exit-code"), ("unusable judge output", "unusable-output"),
                         ("could not start", "start-failed"), ("could not prepare", "prepare-failed")):
        if m.startswith(prefix):
            return kind
    return "error"


def log_record(cfg, session, tier, decision, prompt, ms):
    """Decision log entry. By default it holds no text derived from the prompt: the reason written by a model
    judge and raw error messages (which can quote judge output) are kept only with logging.log_judge_details."""
    details = cfg["logging"]["log_judge_details"]
    source = decision.get("source")
    reason = decision.get("reason", "")
    if not details and source not in ROUTER_SOURCES:
        reason = "(judge reason not logged; set logging.log_judge_details to keep it)"
    attempts = []
    for a in decision.get("attempts", []):
        a = dict(a)
        if "error" in a:
            a["error_kind"] = error_kind(a["error"])
            if not details:
                del a["error"]
        attempts.append(a)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session, "tier": tier,
        "judged_tier": decision["tier"], "source": source, "confidence": decision.get("confidence"),
        "reason": reason, "attempts": attempts, "cost_usd": decision.get("cost_usd"), "ms": ms,
        "judge_ran": any(isinstance(a, dict) and a.get("step") != "rules" for a in decision.get("attempts", [])),
        "prompt_chars": len(prompt), "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
    }
    if cfg["logging"]["log_prompt_text"]:
        record["prompt"] = prompt[:2000]
    return record


def emit_context(text, user_message=None):
    """additionalContext goes to the model; systemMessage is shown to the user only."""
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
    if user_message:
        out["systemMessage"] = user_message
    common.emit(out)


def main():
    if not common.router_active():
        return 0
    data = common.read_hook_input()
    if data is None:
        return 0
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
    session = common.safe_id(data.get("session_id"))
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
    prev = common.read_state(session)
    notification = is_notification(prompt) and prev.get("tier") in common.TIERS
    # Provisional state first: whatever happens below, the guard never enforces the previous
    # prompt's verdict or used-line count against this prompt. (A notification continues the
    # current prompt, so its state is kept as it is.) Each new prompt gets a new generation number,
    # so a slow update meant for an older prompt can never overwrite the state of a newer one.
    generation = prev.get("generation", 0) + (0 if notification else 1)
    if not notification:
        def provisional(cur):
            return {"tier": "standard", "main_lines_used": 0, "other_lines_used": 0, "provisional": True,
                    "reason": "router error before a verdict", "updated": time.time(),
                    "generation": max(generation, cur.get("generation", 0) + 1)}
        generation = common.update_state(session, provisional)["generation"]
    try:
        cfg = common.load_config()
    except common.RouterError as exc:
        emit_context("[router] Routing is OFF for this prompt because the router configuration is invalid: %s. "
                     "Tell the user." % exc,
                     "%s router: OFF for this prompt, the configuration is invalid (python install.py doctor)"
                     % ui.BOX_UNICODE["dot"])
        return 0
    t0 = time.time()
    try:
        decision = decide(prompt, cwd, prev, cfg)
        tier = common.effective_tier(decision["tier"], cfg)
    except Exception as exc:
        decision = {"tier": "standard", "source": "error", "reason": "router error: %s" % exc.__class__.__name__,
                    "confidence": None,
                    "attempts": [], "cost_usd": None}
        try:
            tier = common.effective_tier("standard", cfg)
        except common.RouterError:
            tier = "standard"
    new_state = {
        "tier": tier, "judged_tier": decision["tier"], "source": decision.get("source"),
        "reason": decision.get("reason", ""), "confidence": decision.get("confidence"),
        "main_lines_used": 0, "other_lines_used": 0, "updated": time.time(), "generation": generation,
        "prompt_id": data.get("prompt_id") if isinstance(data.get("prompt_id"), str) else None,
    }

    def commit(cur):
        if cur.get("generation", 0) > generation:
            return None  # a newer prompt owns the state now
        if not (decision.get("source") == "notification" and notification):
            return new_state
        # A notification continues the current prompt: keep its verdict, counters and recorded calls,
        # as they are NOW (a guard may have recorded a call since this hook started).
        state = dict(cur, updated=time.time())
        tid = throttled_call_id(prompt, cur)
        if tid and tid not in cur.get("throttled_calls", []):
            state["throttled_calls"] = (cur.get("throttled_calls", []) + [tid])[-common.MAX_TRACKED_CALLS:]
        if tid:
            decision["reason"] = ("background agent call %s failed with a rate-limit/overload error: `deep-haiku` "
                                  "is now accepted for this prompt" % tid)
        return state
    common.update_state(session, commit)
    ms = int((time.time() - t0) * 1000)
    common.append_log(cfg, log_record(cfg, session, tier, decision, prompt, ms))
    if decision.get("source") not in ("override", "sticky", "escalation", "slash-command", "notification"):
        common.cleanup_old_state()
    emit_context(directive(tier, decision, cfg), user_line(tier, decision, cfg, ms))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # last resort: never break the user's session
        sys.stderr.write("[router] judge hook error: %s\n" % exc)
        try:
            emit_context("[router] Router error (%s). Treat this prompt as STANDARD: delegate implementation to "
                         "`implementer`." % exc)
        except Exception:
            pass
        sys.exit(0)
