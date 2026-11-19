"""Start Claude Code on Microsoft Foundry with the router switched on.

    claude-router [any normal claude arguments]
    claude-router --router-dry-run          # print what would run, secrets masked
    claude-router --router-judge "foundry:opus:high, rules"      # this session only: choose the judge
    claude-router --router-preset azure-only                     # this session only: a judge preset
    claude-router --router-tier deep=claude-fable-5-2@fable-prod # this session only: a tier's model/deployment
Permanent changes: python install.py config (show | judge | tier | preset | set | wizard).

Sets the Foundry variables only for this process tree, so your settings files stay
free of provider variables and the judge can still use your subscription login.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
import common  # noqa: E402
import ui  # noqa: E402

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _load_settings_arg(value):
    """A --settings value is a JSON string or a path to a JSON file, as in Claude Code."""
    text = value.strip()
    try:
        if not text.startswith("{"):
            text = Path(value).expanduser().read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise common.RouterError("cannot read --settings %s: %s" % (value, exc))
    if not isinstance(data, dict):
        raise common.RouterError("--settings %s must be a JSON object" % value)
    return data


def split_settings(args):
    """Remove every --settings / --settings=X from args. Returns (other args, [settings objects])."""
    rest, found, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--settings":
            if i + 1 >= len(args):
                raise common.RouterError("--settings needs a value")
            found.append(_load_settings_arg(args[i + 1]))
            i += 2
            continue
        if a.startswith("--settings="):
            found.append(_load_settings_arg(a[len("--settings="):]))
        else:
            rest.append(a)
        i += 1
    return rest, found


def merged_settings(cfg, user_settings):
    """Claude Code reads only one --settings argument. The user's settings are merged in order, then the router's
    modelOverrides are applied on top, so a user --settings can never drop the deployment mapping."""
    out = {}
    for s in user_settings:
        out = common._deep_merge(out, json.loads(json.dumps(s)))
    ours = common.model_overrides(cfg)
    if ours:
        mo = out.get("modelOverrides") if isinstance(out.get("modelOverrides"), dict) else {}
        for mid, dep in ours.items():
            if mid in mo and mo[mid] != dep:
                sys.stderr.write("[router] your --settings maps %s to %s; the router config maps it to %s, which is "
                                 "used\n" % (mid, mo[mid], dep))
            mo[mid] = dep
        out["modelOverrides"] = mo
    return out


def build_launch(cfg, argv, environ):
    env = common.foundry_env(cfg, environ)  # also drops inherited retry overrides (CLAUDE_CODE_RETRY_WATCHDOG, ...)
    env["CLAUDE_CODE_MAX_RETRIES"] = str(cfg["launcher"]["max_retries"])
    env["CLAUDE_ROUTER"] = "1"
    cmd = common.resolve_claude_cmd(cfg) + ["--model", "haiku"]
    fast_effort = cfg["tiers"]["fast"].get("effort")
    if fast_effort and not any(a == "--effort" or a.startswith("--effort=") for a in argv):
        cmd += ["--effort", fast_effort]
    fb = cfg["launcher"].get("fallback_models") or []
    if fb:
        cmd += ["--fallback-model", ",".join(fb)]
    rest, user_settings = split_settings(list(cfg["launcher"].get("extra_args") or []) + list(argv))
    if user_settings:
        data = merged_settings(cfg, user_settings)
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        cmd += ["--settings", str(common.write_generated("launch-settings-%s.json" % digest, data))]
    else:
        settings = common.foundry_settings_file(cfg)
        if settings:
            cmd += ["--settings", str(settings)]
    return cmd + rest, env


def hooks_active(environ, cwd):
    """True if a settings file Claude Code will read for this folder has the router's hooks."""
    files = [common.user_claude_dir(environ) / n for n in ("settings.json", "settings.local.json")]
    d = Path(cwd).resolve()
    for folder in [d] + list(d.parents):
        files += [folder / ".claude" / "settings.local.json", folder / ".claude" / "settings.json"]
    code = (common.router_home() / "router").as_posix()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8").replace("\\\\", "/").replace("\\", "/")
        except (OSError, ValueError):
            continue
        if code in text and "judge_hook.py" in text:
            return True
    return False


def describe_changes(env, environ):
    """Only what the router sets, changes or removes; secrets fully masked."""
    out = []
    for k in sorted(k for k in env if environ.get(k) != env[k]):
        v = env[k]
        if any(h in k for h in SECRET_HINTS) and v:
            v = "***"
        out.append("  set     %s=%s" % (k, v))
    for k in sorted(k for k in environ if k not in env):
        out.append("  removed %s" % k)
    return "\n".join(out) or "  (nothing)"


ROUTER_FLAGS = ("--router-judge", "--router-preset", "--router-tier")


def session_overrides(argv):
    """Take --router-judge / --router-preset / --router-tier out of argv. Returns (rest, overlay dict)."""
    rest, overlay, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        name, eq, value = a.partition("=")
        if name in ROUTER_FLAGS:
            if not eq:
                if i + 1 >= len(argv):
                    raise common.RouterError("%s needs a value" % name)
                value = argv[i + 1]
                i += 1
            if name in ("--router-judge", "--router-preset"):
                if name == "--router-preset" and value not in common.JUDGE_PRESETS:
                    raise common.RouterError("unknown preset %r; choose one of: %s"
                                             % (value, ", ".join(common.JUDGE_PRESETS)))
                overlay.setdefault("judge", {})["chain"] = common.parse_judge_spec(value)
            else:
                tier, changes = common.parse_tier_spec(value)
                overlay.setdefault("tiers", {}).setdefault(tier, {}).update(changes)
        else:
            rest.append(a)
        i += 1
    return rest, overlay


def main(argv):
    original_env = dict(os.environ)  # for --router-dry-run: what the router changes compared with your shell
    dry = "--router-dry-run" in argv
    argv = [a for a in argv if a != "--router-dry-run"]
    try:
        argv, overlay = session_overrides(argv)
        if overlay:
            # One overlay for the launcher and, through the environment, for every hook of this session.
            try:
                current = json.loads(os.environ.get(common.OVERRIDES_ENV) or "{}")
            except ValueError as exc:
                raise common.RouterError("%s is not valid JSON: %s" % (common.OVERRIDES_ENV, exc))
            os.environ[common.OVERRIDES_ENV] = json.dumps(common._deep_merge(current, overlay), sort_keys=True)
        cfg = common.load_config()
        cmd, env = build_launch(cfg, argv, os.environ)
    except common.RouterError as exc:
        sys.stderr.write("[router] cannot start: %s\n" % exc)
        return 2
    warnings = []
    if not hooks_active(os.environ, os.getcwd()):
        warnings.append("no router hooks in %s or this project's .claude folder, so nothing will be routed. If you use "
                        "CLAUDE_CONFIG_DIR, run `python install.py install` with the same value (SETUP.md, "
                        "troubleshooting)." % common.user_claude_dir())
    endpoint = env.get("ANTHROPIC_FOUNDRY_RESOURCE") or env.get("ANTHROPIC_FOUNDRY_BASE_URL")
    try:
        lines = ui.banner(cfg, common.VERSION, endpoint, ui.Painter(sys.stderr), mode=cfg["ui"]["banner"],
                          warnings=warnings)
        sys.stderr.write("\n".join(lines) + ("\n" if lines else ""))
    except Exception as exc:  # graphics must never stop Claude Code from starting
        sys.stderr.write("[router] Foundry %s (banner error: %s)\n" % (endpoint, exc))
        for w in warnings:
            sys.stderr.write("[router] warning: %s\n" % w)
    if dry:
        sys.stdout.write("command:\n  %s\nenvironment changes made by the router:\n%s\n" % (
            " ".join(cmd), describe_changes(env, original_env)))
        return 0
    try:
        return run_claude(cmd, env)
    except OSError as exc:
        sys.stderr.write("[router] could not start claude: %s\n" % exc)
        return 2


def run_claude(cmd, env, popen=subprocess.Popen, execvpe=None, windows=None):
    """Hand the terminal to Claude Code without ever killing it on Ctrl+C.

    POSIX: replace this Python process (exec), so signals go straight to Claude Code.
    Windows: no exec; start Claude Code and keep waiting through KeyboardInterrupt, because the
    console delivers Ctrl+C to Claude Code itself and it decides what to do.
    """
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        (execvpe or os.execvpe)(cmd[0], cmd, env)
        return 0  # not reached with the real os.execvpe
    proc = popen(cmd, env=env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
