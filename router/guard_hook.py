"""PreToolUse hook: enforce the router verdict on file edits and Agent calls.

Who is acting: a hook input with `agent_id` comes from a subagent, and `agent_type` then says which one.
Without `agent_id` it is the main session, even when the session was started with `--agent NAME`
(Claude Code then sends agent_type=NAME but no agent_id).

Main session (Haiku):
  - Edit / Write / MultiEdit / NotebookEdit lines are counted per prompt against
    guard.main_edit_budget[tier]. Every call counts at least 1 line; Write counts the larger of the
    new content and the file it replaces; replace_all counts every occurrence (CRLF files included);
    a notebook insert counts the new cell, a delete the old one. Over budget -> denied.
  - Agent calls must match the verdict (see DELEGATES below and SETUP.md section 7).
Subagents:
  - implementer / architect / deep-haiku: unlimited edits.
  - any other subagent (general-purpose, forks, Explore, custom agents): one shared per-prompt limit.
  - their Agent calls: no `model` override on pinned agents, and `deep-haiku` only with throttle evidence.
`deep-haiku` evidence: an architect/implementer call of THIS prompt that failed with 429/529. The call's
tool_use_id is recorded here when the call is allowed; a failed background-agent notification for that id
(judge_hook.py), or an error tool_result for that id in the transcript, marks it as throttled.
Every state change runs under a per-session lock, so parallel hooks do not lose updates.
Bash commands are NOT inspected (see SETUP.md, "Limits").
"""
import json
import os
import re
import sys

sys.dont_write_bytecode = True
import common  # noqa: E402

LARGE = 10 ** 6            # unknown size: treat as "too large for any budget"
MAX_READ_BYTES = 20 * 1024 * 1024
THROTTLE_RE = common.THROTTLE_RE
NOTIF_STATUS_RE = re.compile(r"<status>\s*([^<]+?)\s*</status>")


def deny(reason):
    common.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                        "permissionDecision": "deny",
                                        "permissionDecisionReason": "[router] " + reason}})
    return 0


def _read_text(path):
    """File content, "" if the file does not exist, None if it exists but cannot be read."""
    if not isinstance(path, str) or not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > MAX_READ_BYTES:
            return None
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _lf(text):
    return text.replace("\r\n", "\n") if isinstance(text, str) else text


def _edit_lines(edit, file_text):
    old, new = edit.get("old_string"), edit.get("new_string")
    lines = max(common.count_lines(old), common.count_lines(new), 1)
    if edit.get("replace_all") is True:
        if file_text is None:
            return LARGE
        # Claude Code matches old_string against CRLF files as if they had LF line endings.
        occurrences = _lf(file_text).count(_lf(old)) if isinstance(old, str) and old else 1
        lines *= max(occurrences, 1)
    return lines


def _notebook_cell_lines(path, cell_id):
    text = _read_text(path)
    if not text or not isinstance(cell_id, str):
        return 0
    try:
        nb = json.loads(text)
        for cell in nb.get("cells", []) if isinstance(nb, dict) else []:
            if isinstance(cell, dict) and cell.get("id") == cell_id:
                src = cell.get("source")
                src = "".join(src) if isinstance(src, list) else src
                return common.count_lines(src if isinstance(src, str) else "")
    except (ValueError, TypeError, AttributeError):
        return 0
    return 0


def changed_lines(tool, inp):
    if tool == "Edit":
        needs_file = inp.get("replace_all") is True
        return _edit_lines(inp, _read_text(inp.get("file_path")) if needs_file else "")
    if tool == "MultiEdit":
        edits = inp.get("edits")
        if not isinstance(edits, list) or not edits:
            return 1
        file_text = _read_text(inp.get("file_path")) if any(
            isinstance(e, dict) and e.get("replace_all") is True for e in edits) else ""
        return sum(_edit_lines(e, file_text) if isinstance(e, dict) else 1 for e in edits)
    if tool == "Write":
        existing = _read_text(inp.get("file_path"))
        existing_lines = LARGE if existing is None else common.count_lines(existing)
        return max(common.count_lines(inp.get("content")), existing_lines, 1)
    if tool == "NotebookEdit":
        mode = inp.get("edit_mode") if inp.get("edit_mode") in ("insert", "delete") else "replace"
        new = common.count_lines(inp.get("new_source"))
        if mode == "insert":  # adds a cell after cell_id; that cell stays as it is
            return max(new, 1)
        old = _notebook_cell_lines(inp.get("notebook_path"), inp.get("cell_id"))
        return max(old, 1) if mode == "delete" else max(new, old, 1)
    return None


def _transcript_records(transcript_path):
    if not isinstance(transcript_path, str) or not transcript_path:
        return []
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, size - 4 * 1024 * 1024))
            raw = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the first line may be cut by the seek; other junk is ignored
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _blocks(rec):
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    return msg.get("role"), content


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text(b.get("text") if b.get("type") == "text" else b.get("content"))
                        for b in content if isinstance(b, dict))
    return ""


def throttle_evidence(transcript_path):
    """True if an architect/implementer call of the current prompt failed with 429/529.

    Evidence is tied to the tool-use id of that call, so text elsewhere (for example the router's own
    directive, which mentions "429") never counts. Two shapes are recognised:
      - background agents (Claude Code 2.1+): a <task-notification> user message with <status>failed</status>,
        <tool-use-id>ID</tool-use-id> and the API error;
      - synchronous agents: an error tool_result block (is_error) for that tool_use id.
    Calls made before the current prompt never count, and neither do successful results.
    """
    records = _transcript_records(transcript_path)
    pinned = []          # tool_use ids of architect/implementer calls, in order
    last_user_prompt = -1
    for i, rec in enumerate(records):
        role, content = _blocks(rec)
        if role == "assistant" and isinstance(content, list):
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task")
                        and isinstance(b.get("input"), dict) and b["input"].get("subagent_type") in common.PINNED_AGENTS
                        and isinstance(b.get("id"), str)):
                    pinned.append((i, b["id"]))
        elif (rec.get("type") == "user" and role == "user" and isinstance(content, str)
              and "<task-notification>" not in content):
            last_user_prompt = i
    recent = [tid for i, tid in pinned if i > last_user_prompt]  # this prompt's calls only, never older ones
    if not recent:
        return False
    for rec in records[last_user_prompt + 1:]:
        role, content = _blocks(rec)
        if role != "user":
            continue
        if isinstance(content, str) and "<task-notification>" in content:
            m = NOTIF_STATUS_RE.search(content)
            if (m and m.group(1).lower() in common.FAILED_STATUSES
                    and any("<tool-use-id>%s</tool-use-id>" % tid in content for tid in recent)
                    and THROTTLE_RE.search(common.notification_error_text(content))):
                return True
        elif isinstance(content, list):
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in recent
                        and b.get("is_error") is True and THROTTLE_RE.search(_text(b.get("content")))):
                    return True
    return False


def _sub(inp):
    return inp.get("subagent_type") if isinstance(inp.get("subagent_type"), str) else ""


def _has_throttle_evidence(data, state):
    if state.get("throttled_calls"):
        return True
    paths = [data.get("agent_transcript_path"), data.get("transcript_path")]
    return any(throttle_evidence(p) for p in paths if isinstance(p, str) and p)


def check_agent_call(inp, data, state, from_subagent=False):
    """Why this Agent call is not accepted, or None. The delegate rules, per tier and caller:

    | delegate                  | main: FAST | main: STANDARD | main: DEEP | from a subagent |
    | architect                 | yes        | yes            | yes        | yes             |
    | implementer               | yes        | yes            | no         | yes             |
    | deep-haiku                | yes        | after a 429    | after a 429| after a 429     |
    | general-purpose / fork    | yes        | no             | no         | yes             |
    | any other (Explore, ...)  | yes        | yes            | yes        | yes             |
    `model` on architect/implementer is never accepted.
    """
    sub = _sub(inp)
    if sub in common.PINNED_AGENTS and inp.get("model"):
        return ("Do not pass `model` when calling `%s`; its definition pins the right model. "
                "Call it again without `model`." % sub)
    tier = state.get("tier") if state.get("tier") in common.TIERS else "standard"
    target = "architect" if tier == "deep" else "implementer"
    if sub == common.FALLBACK_AGENT and (from_subagent or tier != "fast") and not _has_throttle_evidence(data, state):
        return ("`deep-haiku` is only for after `architect` or `implementer` failed with a rate-limit (429) or "
                "overloaded (529) error in this prompt, and no such failure is visible. Call `%s` first. If the user "
                "wants the small model anyway, they can start the prompt with ::fast." % target)
    if from_subagent:
        return None
    if tier == "deep" and sub == "implementer":
        return ("The router verdict for this prompt is DEEP: delegate to `architect`, not `implementer` "
                "(architect can split off subtasks itself).")
    if tier in ("standard", "deep") and sub in common.GENERIC_AGENTS:
        return ("The router verdict for this prompt is %s: `%s` runs on your own small model. Delegate to `%s` "
                "instead (research agents such as `Explore` are fine for reading)." % (
                    tier.upper(), sub or "a fork", target))
    return None


def main():
    if not common.router_active():
        return 0
    data = common.read_hook_input()
    if data is None:
        return 0
    tool = data.get("tool_name") if isinstance(data.get("tool_name"), str) else ""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    # agent_id identifies a subagent; agent_type alone (a session started with --agent NAME) is the main session.
    is_subagent = isinstance(data.get("agent_id"), str) and bool(data.get("agent_id"))
    agent = data.get("agent_type") if is_subagent and isinstance(data.get("agent_type"), str) else ""
    try:
        cfg = common.load_config()
    except common.RouterError:
        return 0  # the judge hook already told Claude that routing is off
    if tool not in ("Agent", "Task"):
        changed = changed_lines(tool, inp)
        if changed is None or agent in common.EDIT_AGENTS:
            return 0
    session = common.safe_id(data.get("session_id"))
    result = {}

    def decide(state):
        if tool in ("Agent", "Task"):
            problem = check_agent_call(inp, data, state, from_subagent=is_subagent)
            if problem:
                result["deny"] = problem
                return None
            tid = data.get("tool_use_id")
            if _sub(inp) in common.PINNED_AGENTS and isinstance(tid, str) and tid:
                # Remember the call, so a later 429/529 failure of exactly this call unlocks deep-haiku.
                return dict(state, pinned_calls=(state.get("pinned_calls", []) + [tid])[-common.MAX_TRACKED_CALLS:])
            return None
        if is_subagent:
            limit = cfg["guard"]["other_subagent_edit_lines"]
            used = state.get("other_lines_used", 0)
            if used + changed > limit:
                result["deny"] = (
                    "%s of %d lines would bring edits by subagents running on the small model to %d lines this "
                    "turn (limit %d). Stop and let the main conversation delegate this to `implementer` or "
                    "`architect`." % (tool, min(changed, LARGE), used + changed, limit))
                return None
            return dict(state, other_lines_used=used + changed)
        tier = state.get("tier") if state.get("tier") in common.TIERS else "standard"
        budget = cfg["guard"]["main_edit_budget"][tier]
        used = state.get("main_lines_used", 0)
        if used + changed > budget:
            target = "architect" if tier == "deep" else "implementer"
            size = "an unknown number of" if changed >= LARGE else "%d" % changed
            result["deny"] = (
                "%s of %s lines would bring main-thread edits to more than the %s budget of %d lines for this "
                "prompt (already used: %d). Delegate with the Agent tool to `%s` (use `architect` for design, "
                "schema, security, concurrency or anything that already failed twice). Give it a self-contained "
                "brief: goal, files, constraints, how to verify." % (tool, size, tier.upper(), budget, used, target))
            return None
        return dict(state, main_lines_used=used + changed)

    common.update_state(session, decide)
    if "deny" in result:
        return deny(result["deny"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never break the session
        sys.stderr.write("[router] guard hook error: %s\n" % exc)
        sys.exit(0)
