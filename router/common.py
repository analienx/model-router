"""Shared helpers for claude-model-router.

Standard library only. Works on Python 3.8+ on Windows, macOS and Linux.
Nothing in here may crash a Claude Code session: callers catch RouterError.
"""
import contextlib
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

VERSION = "1.3.0"

TIERS = ("fast", "standard", "deep")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}
# Claude Code model alias used for each tier.
TIER_ALIAS = {"fast": "haiku", "standard": "sonnet", "deep": "opus"}
# Subagent that does the work for each tier ("fast" = the Haiku main thread itself).
TIER_AGENT = {"fast": None, "standard": "implementer", "deep": "architect"}
PINNED_AGENTS = ("implementer", "architect")              # `model` override on these is denied
EDIT_AGENTS = ("implementer", "architect", "deep-haiku")  # may make large edits
FALLBACK_AGENT = "deep-haiku"                             # only after a 429/529 from a pinned agent
# Delegates that run on the caller's own (small) model. Not accepted for STANDARD or DEEP work from the main session.
GENERIC_AGENTS = ("", "general-purpose", "fork")
FAILED_STATUSES = ("failed", "error", "errored")         # <status> values of a background agent that failed
THROTTLE_RE = re.compile(r"\b(429|529)\b|rate[ _-]?limit|overloaded", re.IGNORECASE)
# Fields of a <task-notification> that are ids or file paths, never error text: a project folder such as
# ".../issue-429/" or a random id must not look like a rate-limit error.
NOTIFICATION_META_RE = re.compile(r"<(task-id|tool-use-id|output-file)>.*?</\1>", re.DOTALL)
MAX_TRACKED_CALLS = 50


def notification_error_text(text):
    """A <task-notification> without its id and path fields: the part that can describe an error."""
    return NOTIFICATION_META_RE.sub(" ", text) if isinstance(text, str) else ""

DEFAULT_STICKY_PHRASES = [
    "yes", "y", "yep", "yeah", "ok", "okay", "sure", "continue", "go on", "go ahead", "do it",
    "proceed", "please do", "sounds good", "lgtm", "thanks", "thank you", "next", "go",
]
DEFAULT_ESCALATE_PHRASES = [
    "try again", "still failing", "still fails", "still broken", "still not working", "still wrong",
    "didn't work", "did not work", "doesn't work", "does not work", "not working", "same error",
    "same problem", "it failed", "failed again", "again",
]

DEFAULT_CONFIG = {
    "version": 1,
    "foundry": {
        "resource": "",
        "base_url": "",
        "auth": "entra",
        "api_key_env": "FOUNDRY_API_KEY",
        "prompt_caching_1h": False,
    },
    "tiers": {
        "fast": {"model_id": "claude-haiku-5-5", "deployment": "claude-haiku-5-5"},
        "standard": {"model_id": "claude-sonnet-5-5", "deployment": "claude-sonnet-5-5"},
        "deep": {"model_id": "claude-opus-5-5", "deployment": "claude-opus-5-5"},
    },
    "launcher": {
        "max_retries": 3,
        "fallback_models": [],
        "extra_args": [],
    },
    "judge": {
        "chain": [
            {"backend": "subscription", "model": "sonnet", "effort": "max", "timeout_s": 90},
            {"backend": "foundry", "model": "sonnet", "effort": "high", "timeout_s": 45},
            {"backend": "rules"},
        ],
        "min_confidence": 0.6,
        "max_prompt_chars": 6000,
        "include_git_signals": True,
        "sticky_phrases": list(DEFAULT_STICKY_PHRASES),
        "escalate_phrases": list(DEFAULT_ESCALATE_PHRASES),
        "claude_path": "",
    },
    "guard": {
        "main_edit_budget": {"fast": 40, "standard": 10, "deep": 0},
        "other_subagent_edit_lines": 20,
    },
    "logging": {
        "enabled": True,
        "log_prompt_text": False,
        "log_judge_details": False,
    },
}

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_BACKENDS = ("subscription", "foundry", "rules")
VALID_ALIASES = ("haiku", "sonnet", "opus", "fable")
SECTIONS = ("foundry", "tiers", "launcher", "judge", "guard", "logging")


class RouterError(Exception):
    """Configuration or environment problem the user has to fix."""


# --------------------------------------------------------------------------- paths

def router_home():
    env = os.environ.get("CLAUDE_ROUTER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude-router"


def config_path():
    return router_home() / "router.config.json"


def code_dir():
    return Path(__file__).resolve().parent


def judge_system_prompt_path():
    return code_dir() / "judge_system.md"


def user_claude_dir(environ=None):
    """The Claude Code profile folder Claude Code itself reads: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    environ = os.environ if environ is None else environ
    env = environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


# --------------------------------------------------------------------------- config

def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value  # lists (e.g. judge.chain) are replaced, not merged
    return base


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    p = Path(path) if path else config_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RouterError("cannot read %s: %s" % (p, exc))
        if not isinstance(data, dict):
            raise RouterError("%s must contain a JSON object" % p)
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
        _deep_merge(cfg, data)
    validate_config(cfg)
    return cfg


def _require(cond, msg):
    if not cond:
        raise RouterError(msg)


def _str_list(x):
    return isinstance(x, list) and all(isinstance(i, str) for i in x)


def validate_config(cfg):
    """Reject every wrong-shaped value, so hooks never crash on a valid-JSON-but-wrong config."""
    _require(isinstance(cfg, dict), "configuration must be a JSON object")
    for s in SECTIONS:
        _require(isinstance(cfg.get(s), dict), "%s must be an object" % s)

    f = cfg["foundry"]
    _require(isinstance(f.get("resource"), str), "foundry.resource must be a string")
    _require(isinstance(f.get("base_url"), str), "foundry.base_url must be a string")
    res = f["resource"]
    _require(not res or re.fullmatch(r"[A-Za-z0-9-]+", res),
             "foundry.resource must be the resource name only (e.g. my-resource), not a URL or host name")
    _require(not f["base_url"] or f["base_url"].startswith("https://")
             or re.match(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", f["base_url"]),
             "foundry.base_url must start with https:// (plain http is accepted only for localhost test servers)")
    _require(f.get("auth") in ("entra", "api_key_env"), 'foundry.auth must be "entra" or "api_key_env"')
    _require(isinstance(f.get("api_key_env"), str), "foundry.api_key_env must be a string")
    if f["auth"] == "api_key_env":
        _require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", f["api_key_env"] or ""),
                 "foundry.api_key_env must be an environment variable name")
    _require(isinstance(f.get("prompt_caching_1h"), bool), "foundry.prompt_caching_1h must be true or false")

    available = 0
    for t in TIERS:
        spec = cfg["tiers"].get(t)
        _require(isinstance(spec, dict), "tiers.%s must be an object" % t)
        dep, mid = spec.get("deployment"), spec.get("model_id")
        _require(dep is None or isinstance(dep, str), "tiers.%s.deployment must be a string or null" % t)
        if dep:
            _require(isinstance(mid, str) and re.fullmatch(r"\S+", mid),
                     "tiers.%s.model_id is required (no spaces) when a deployment is set" % t)
            _require(re.fullmatch(r"\S+", dep), "tiers.%s.deployment must not contain spaces" % t)
            available += 1
    _require(available > 0, "at least one tier needs a Foundry deployment")
    seen = {}
    for t in TIERS:
        spec = cfg["tiers"][t]
        if spec.get("deployment"):
            other = seen.setdefault(spec["model_id"], (t, spec["deployment"]))
            _require(other[1] == spec["deployment"],
                     "tiers.%s and tiers.%s use the same model_id %s with different deployments (%s, %s); Claude Code "
                     "can map a model id to only one deployment" % (other[0], t, spec["model_id"], other[1],
                                                                     spec["deployment"]))

    lau = cfg["launcher"]
    _require(is_int(lau.get("max_retries")) and 0 <= lau["max_retries"] <= 50,
             "launcher.max_retries must be an integer from 0 to 50")
    _require(_str_list(lau.get("fallback_models")), "launcher.fallback_models must be a list of strings")
    _require(_str_list(lau.get("extra_args")), "launcher.extra_args must be a list of strings")

    j = cfg["judge"]
    chain = j.get("chain")
    _require(isinstance(chain, list) and chain, "judge.chain must be a non-empty list")
    for i, step in enumerate(chain):
        _require(isinstance(step, dict), "judge.chain[%d] must be an object" % i)
        b = step.get("backend")
        _require(b in VALID_BACKENDS, "judge.chain[%d].backend must be one of %s" % (i, ", ".join(VALID_BACKENDS)))
        if b != "rules":
            _require(step.get("model") in VALID_ALIASES,
                     "judge.chain[%d].model must be one of %s" % (i, ", ".join(VALID_ALIASES)))
            _require(step.get("effort") in (None,) + VALID_EFFORTS,
                     "judge.chain[%d].effort must be one of %s" % (i, ", ".join(VALID_EFFORTS)))
            ts = step.get("timeout_s", 60)
            _require(is_number(ts) and 1 <= ts <= 600, "judge.chain[%d].timeout_s must be a number from 1 to 600" % i)
    mc = j.get("min_confidence")
    _require(is_number(mc) and 0 <= mc <= 1, "judge.min_confidence must be a number from 0 to 1")
    _require(is_int(j.get("max_prompt_chars")) and j["max_prompt_chars"] > 0,
             "judge.max_prompt_chars must be a positive integer")
    _require(isinstance(j.get("include_git_signals"), bool), "judge.include_git_signals must be true or false")
    _require(_str_list(j.get("sticky_phrases")), "judge.sticky_phrases must be a list of strings")
    _require(_str_list(j.get("escalate_phrases")), "judge.escalate_phrases must be a list of strings")
    cp = j.get("claude_path")
    _require(isinstance(cp, str) or (isinstance(cp, list) and cp and _str_list(cp)),
             "judge.claude_path must be a string or a non-empty list of strings")

    g = cfg["guard"]
    budget = g.get("main_edit_budget")
    _require(isinstance(budget, dict), "guard.main_edit_budget must be an object")
    for t in TIERS:
        _require(is_int(budget.get(t)) and budget[t] >= 0, "guard.main_edit_budget.%s must be a non-negative integer" % t)
    _require(is_int(g.get("other_subagent_edit_lines")) and g["other_subagent_edit_lines"] >= 0,
             "guard.other_subagent_edit_lines must be a non-negative integer")

    lg = cfg["logging"]
    _require(isinstance(lg.get("enabled"), bool), "logging.enabled must be true or false")
    _require(isinstance(lg.get("log_prompt_text"), bool), "logging.log_prompt_text must be true or false")
    _require(isinstance(lg.get("log_judge_details"), bool), "logging.log_judge_details must be true or false")
    return cfg


def judge_timeout_total(cfg):
    return sum(float(s.get("timeout_s", 60)) for s in cfg["judge"]["chain"] if s.get("backend") != "rules")


# --------------------------------------------------------------------------- tiers

def tier_available(tier, cfg):
    return bool(cfg["tiers"].get(tier, {}).get("deployment"))


def effective_tier(tier, cfg):
    """Nearest tier that has a deployment: prefer stronger, then weaker."""
    if tier not in TIER_RANK:
        tier = "standard"
    if tier_available(tier, cfg):
        return tier
    rank = TIER_RANK[tier]
    for t in TIERS[rank + 1:]:
        if tier_available(t, cfg):
            return t
    for t in reversed(TIERS[:rank]):
        if tier_available(t, cfg):
            return t
    raise RouterError("no tier has a deployment")


def bump_tier(tier):
    rank = TIER_RANK.get(tier, 1)
    return TIERS[min(rank + 1, len(TIERS) - 1)]


# --------------------------------------------------------------------------- environments

# Environment policy for every Claude Code process the router starts (see SETUP.md, "Environment policy").
#   removed for every child: provider, credential, model and session variables (ANTHROPIC_*, CLAUDE_CODE_USE_*,
#     the names below) and retry overrides, so the router's provider, model mapping and retry limit always apply;
#   removed for judge children only: effort overrides, because the judge's effort comes from judge.chain;
#   CLAUDE_CODE_OAUTH_TOKEN (a subscription login in token form, from `claude setup-token`): kept for the
#     subscription judge, which then uses it instead of the stored login; removed for every Foundry child;
#   kept: everything else.
_PROVIDER_EXACT = {
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "AWS_BEARER_TOKEN_BEDROCK", "ENABLE_PROMPT_CACHING_1H",
}
_SESSION_EXACT = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_ROUTER", "CLAUDE_ROUTER_JUDGE"}
RETRY_VARS = {"CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CODE_RETRY_WATCHDOG"}
EFFORT_VARS = {"CLAUDE_CODE_EFFORT_LEVEL", "MAX_THINKING_TOKENS"}


def _clean_env(base):
    """Copy of `base` without provider, credential, model, session or retry variables."""
    return {k: v for k, v in base.items()
            if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_")
                    or k in _PROVIDER_EXACT or k in _SESSION_EXACT or k in RETRY_VARS)}


def judge_env(env):
    """Finish a judge child's environment: no hooks recursion, one retry, no memory files, config effort only."""
    for k in EFFORT_VARS:
        env.pop(k, None)
    env["CLAUDE_ROUTER_JUDGE"] = "1"
    env["CLAUDE_CODE_MAX_RETRIES"] = "1"
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env


def subscription_env(base):
    """Environment for a `claude -p` call that must use the logged-in subscription.

    Every provider/API-key variable is removed, so the subscription login (stored, or CLAUDE_CODE_OAUTH_TOKEN)
    is the only credential left. CLAUDE.md loading is switched off so no memory files leave the machine.
    """
    return judge_env(_clean_env(base))


def model_overrides(cfg):
    out = {}
    for t in TIERS:
        spec = cfg["tiers"][t]
        dep, mid = spec.get("deployment"), spec.get("model_id")
        if dep and mid and dep != mid:
            out[mid] = dep
    return out


def alias_models(cfg):
    """Model id each Claude Code alias resolves to, based on available tiers."""
    return {TIER_ALIAS[t]: cfg["tiers"][effective_tier(t, cfg)]["model_id"] for t in TIERS}


def foundry_env(cfg, base):
    """Environment for Claude Code running on Microsoft Foundry."""
    f = cfg["foundry"]
    if not f.get("resource") and not f.get("base_url"):
        raise RouterError("set foundry.resource (or foundry.base_url) in %s" % config_path())
    env = _clean_env(base)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # a subscription credential; Foundry sessions never need it
    env["CLAUDE_CODE_USE_FOUNDRY"] = "1"
    if f.get("base_url"):
        env["ANTHROPIC_FOUNDRY_BASE_URL"] = f["base_url"].rstrip("/")
    else:
        env["ANTHROPIC_FOUNDRY_RESOURCE"] = f["resource"]
    if f.get("auth") == "api_key_env":
        name = f.get("api_key_env") or "FOUNDRY_API_KEY"
        key = base.get(name, "")
        if not key:
            raise RouterError("foundry.auth is api_key_env but environment variable %s is not set" % name)
        env["ANTHROPIC_FOUNDRY_API_KEY"] = key
    # auth == "entra": no key variables, Claude Code uses the Azure default credential chain (az login).
    models = alias_models(cfg)
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = models["haiku"]
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = models["sonnet"]
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = models["opus"]
    if f.get("prompt_caching_1h"):
        env["ENABLE_PROMPT_CACHING_1H"] = "1"
    return env


def write_generated(name, data):
    """Write router_home/generated/<name> only when the content changed (avoids replace races)."""
    path = router_home() / "generated" / name
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except (OSError, ValueError):
        pass
    atomic_write_text(path, text)
    return path


def foundry_settings_file(cfg):
    """--settings file for the session: modelOverrides when deployment names differ from model ids."""
    overrides = model_overrides(cfg)
    if not overrides:
        return None
    return write_generated("foundry-settings.json", {"modelOverrides": overrides})


def judge_settings_file(cfg, backend):
    """--settings file for judge calls: hooks off (+ modelOverrides on Foundry)."""
    data = {"disableAllHooks": True}
    if backend == "foundry":
        overrides = model_overrides(cfg)
        if overrides:
            data["modelOverrides"] = overrides
    return write_generated("judge-%s-settings.json" % backend, data)


def resolve_claude_cmd(cfg):
    cp = cfg["judge"].get("claude_path") or ""
    if isinstance(cp, list):
        return list(cp)
    if cp:
        return [cp]
    found = shutil.which("claude")
    if not found:
        raise RouterError("`claude` was not found on PATH; set judge.claude_path in %s" % config_path())
    return [found]


# --------------------------------------------------------------------------- state & logs

def safe_id(value):
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]
    return s or "unknown"


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def state_path(session_id):
    return router_home() / "state" / (safe_id(session_id) + ".json")


def _sanitize_state(data):
    if not isinstance(data, dict):
        return {}
    out = {}
    if data.get("tier") in TIERS:
        out["tier"] = data["tier"]
    for key in ("main_lines_used", "other_lines_used", "generation"):
        v = data.get(key)
        out[key] = v if is_int(v) and v >= 0 else 0
    for key in ("reason", "source", "judged_tier", "prompt_id"):
        v = data.get(key)
        if isinstance(v, str):
            out[key] = v
    for key in ("pinned_calls", "throttled_calls"):
        v = data.get(key)
        out[key] = [x for x in v if isinstance(x, str)][-MAX_TRACKED_CALLS:] if isinstance(v, list) else []
    c = data.get("confidence")
    out["confidence"] = float(c) if is_number(c) else None
    if is_number(data.get("updated")):
        out["updated"] = data["updated"]
    out["provisional"] = data.get("provisional") is True
    return out


def read_state(session_id):
    try:
        return _sanitize_state(json.loads(state_path(session_id).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def write_state(session_id, data):
    atomic_write_text(state_path(session_id), json.dumps(data, ensure_ascii=True, indent=1))


@contextlib.contextmanager
def file_lock(path, timeout=5.0, stale_after=30.0):
    """Cross-process lock (an O_EXCL lock file). Yields True if held, False after `timeout`.

    Hooks must never hang a session, so a caller that cannot get the lock in time goes on without it.
    A lock file older than `stale_after` seconds is left over from a killed process and is removed.
    """
    path = Path(path)
    fd = None
    deadline = time.time() + timeout
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > stale_after:
                        os.unlink(str(path))
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    break
                time.sleep(0.005)
    except OSError:
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(str(path))
            except OSError:
                pass


def update_state(session_id, change):
    """Read, change and write a session's state as one step, serialised across hook processes.

    `change(state)` gets the current state and returns the new state, or None to leave it as it is.
    Parallel hooks (several Agent calls in one turn, background subagents) no longer lose each other's updates.
    """
    with file_lock(state_path(session_id).with_suffix(".lock")):
        new = change(read_state(session_id))
        if new is not None:
            try:
                write_state(session_id, new)
            except OSError:
                pass
        return new


def cleanup_old_state(max_age_days=7):
    d = router_home() / "state"
    if not d.is_dir():
        return
    cutoff = time.time() - max_age_days * 86400
    for p in d.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def append_log(cfg, record):
    if not cfg["logging"]["enabled"]:
        return
    try:
        p = router_home() / "logs" / "decisions.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- hook I/O

def router_active():
    return os.environ.get("CLAUDE_ROUTER") == "1" and os.environ.get("CLAUDE_ROUTER_JUDGE") != "1"


def read_hook_input():
    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    debug_dump(raw)
    return data if isinstance(data, dict) else None


def debug_dump(raw):
    """Opt-in troubleshooting: CLAUDE_ROUTER_DEBUG_DIR=<folder> saves every hook input there. Off by default."""
    folder = os.environ.get("CLAUDE_ROUTER_DEBUG_DIR")
    if not folder:
        return
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%.6f-%d.json" % (time.time(), os.getpid())
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(raw)
    except OSError:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True))
    sys.stdout.flush()


def count_lines(text):
    if not isinstance(text, str) or not text:
        return 0
    return len(text.splitlines()) or 1
