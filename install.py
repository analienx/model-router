#!/usr/bin/env python3
"""claude-model-router installer and tools.

    python install.py install   [--scope user|project] [--project DIR] [--hook-style exec|bash|powershell] [--force-agents]
    python install.py doctor    [--project DIR]
    python install.py test-judge [--prompt TEXT] [--backend subscription|foundry|rules|all]
    python install.py report    [--last N]
    python install.py uninstall [--scope user|project] [--project DIR] [--purge]

Standard library only, Python 3.8+. See SETUP.md.
"""
import argparse
import copy
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "router"))
import common  # noqa: E402
import judge_hook  # noqa: E402

CODE_FILES = ["common.py", "judge_hook.py", "guard_hook.py", "handoff.py", "launch.py", "judge_system.md"]
AGENT_FILES = ["architect.md", "implementer.md", "deep-haiku.md"]
MARKER = "<!-- installed by claude-model-router -->"
HOOK_SCRIPTS = ("judge_hook.py", "guard_hook.py")
GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Agent|Task"
EXCLUDE_START = "# >>> claude-model-router (personal files, not committed) >>>"
EXCLUDE_END = "# <<< claude-model-router <<<"
IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- helpers

def say(msg=""):
    sys.stdout.write(msg + "\n")


def write_exact(path, text):
    """Write text with exactly the line endings it contains, on every OS."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def claude_dir(scope, project):
    return (Path(project).resolve() / ".claude") if scope == "project" else (Path.home() / ".claude")


def settings_file(scope, project):
    base = claude_dir(scope, project)
    # Project scope uses settings.local.json: the hook paths are personal and must not be committed.
    return base / ("settings.local.json" if scope == "project" else "settings.json")


def load_json_file(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.RouterError("%s is not valid JSON (%s). Fix or move it, then run again." % (path, exc))
    if not isinstance(data, dict):
        raise common.RouterError("%s does not contain a JSON object." % path)
    return data


def backup(path, scope):
    """Copy `path` into the router's backup folder (never into the repository). Returns the copy."""
    if not path.exists():
        return None
    d = common.router_home() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%06d" % (int(time.time() * 1e6) % 1000000)
    for n in range(1000):
        dst = d / ("%s-%s-%s-%s%s" % (scope, path.name, tag, stamp, "" if n == 0 else "-%d" % n))
        if not dst.exists():
            shutil.copy2(str(path), str(dst))
            return dst
    raise common.RouterError("could not create a unique backup name in %s" % d)


def write_json_file(path, data):
    common.atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def router_script(name):
    return common.router_home() / "router" / name


def shim_code(script):
    """`python -c` code that runs the hook script, and exits 0 (does nothing) if the script is missing.

    A missing script would otherwise make Python exit with code 2, which Claude Code treats as
    "block": every prompt and every edit would be refused.
    """
    p = Path(script).as_posix()
    if any(c in p for c in "'\"$`\\\r\n"):
        raise common.RouterError("the router folder path %s contains a quote, $, ` or backslash; set "
                                 "CLAUDE_ROUTER_HOME to a simpler folder and run install again" % p)
    return ("import os,sys,runpy;p='%s';os.path.isfile(p) or sys.exit(0);"
            "sys.path.insert(0,os.path.dirname(p));sys.argv=[p];runpy.run_path(p,run_name='__main__')" % p)


def hook_entry(script, timeout, style):
    code = shim_code(script)
    py = sys.executable
    if style == "exec":
        return {"type": "command", "command": os.path.normpath(py), "args": ["-c", code], "timeout": timeout}
    pyp = Path(py).as_posix()
    if any(c in pyp for c in "\"$`"):
        raise common.RouterError("the Python path %s contains a quote, $ or `; use --hook-style exec" % pyp)
    if style == "bash":
        return {"type": "command", "command": '"%s" -c "%s"' % (pyp, code), "timeout": timeout}
    if style == "powershell":
        return {"type": "command", "command": '& "%s" -c "%s"' % (pyp, code), "shell": "powershell",
                "timeout": timeout}
    raise common.RouterError("unknown hook style %s" % style)


def _hook_strings(hook):
    if not isinstance(hook, dict):
        return []
    out = [hook.get("command")]
    if isinstance(hook.get("args"), list):
        out += hook["args"]
    return [s for s in out if isinstance(s, str)]


def router_markers():
    marks = []
    for name in HOOK_SCRIPTS:
        p = router_script(name)
        marks += [p.as_posix(), os.path.normpath(str(p))]
    return marks


def is_router_hook(hook):
    """Only hooks that point at THIS router's scripts (by full path), never same-named user hooks."""
    marks = router_markers()
    return any(m in s for s in _hook_strings(hook) for m in marks)


def remove_router_hooks(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list) and g["hooks"]:
                kept = [h for h in g["hooks"] if not is_router_hook(h)]
                removed += len(g["hooks"]) - len(kept)
                if kept:
                    new_groups.append(dict(g, hooks=kept))
            else:
                new_groups.append(g)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def add_router_hooks(settings, judge_entry, guard_entry):
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise common.RouterError('existing "hooks" in settings is not an object')
    for event in ("UserPromptSubmit", "PreToolUse"):
        if event in hooks and not isinstance(hooks[event], list):
            raise common.RouterError('existing hooks.%s is not a list' % event)
    remove_router_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("UserPromptSubmit", []).append({"hooks": [judge_entry]})
    hooks.setdefault("PreToolUse", []).append({"matcher": GUARD_MATCHER, "hooks": [guard_entry]})


def find_router_hooks(settings):
    found = []
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    for event, groups in hooks.items():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
                if is_router_hook(h):
                    found.append((event, h))
    return found


# --------------------------------------------------------------------------- registry

def registry_path():
    return common.router_home() / "installs.json"


def load_registry():
    """Install registry; a missing or wrong-shaped file reads as empty, never crashes."""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("installs") if isinstance(data, dict) else None
    out = []
    for e in items if isinstance(items, list) else []:
        if isinstance(e, dict) and isinstance(e.get("settings"), str) and isinstance(e.get("agents_dir"), str):
            out.append({"scope": e.get("scope") if e.get("scope") in ("user", "project") else "user",
                        "settings": e["settings"], "agents_dir": e["agents_dir"],
                        "project": e.get("project") if isinstance(e.get("project"), str) else None})
    return out


def save_registry(entries):
    common.atomic_write_text(registry_path(), json.dumps({"installs": entries}, indent=2) + "\n")


def entry_for(scope, project):
    return {"scope": scope, "settings": str(settings_file(scope, project)),
            "agents_dir": str(claude_dir(scope, project) / "agents"),
            "project": str(Path(project).resolve()) if scope == "project" else None}


def settings_still_hooked(path):
    try:
        return bool(find_router_hooks(load_json_file(Path(path))))
    except common.RouterError:
        return True  # unreadable: assume it may still point at the code, keep the code


# --------------------------------------------------------------------------- git exclude (project scope)

def git_exclude_path(project):
    try:
        p = subprocess.run(["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    path = Path(p.stdout.strip())
    return path if path.is_absolute() else Path(project) / path


def _strip_block(text):
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == EXCLUDE_START:
            skipping = True
            continue
        if line.strip() == EXCLUDE_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return out


def set_git_exclude(project, entries):
    path = git_exclude_path(project)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    lines = _strip_block(text)
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ([""] if lines else []) + [EXCLUDE_START] + entries + [EXCLUDE_END]
    write_exact(path, "\n".join(lines) + "\n")
    return path


def remove_git_exclude(project):
    path = git_exclude_path(project)
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if EXCLUDE_START not in text:
        return None
    lines = _strip_block(text)
    while lines and not lines[-1].strip():
        lines.pop()
    write_exact(path, ("\n".join(lines) + "\n") if lines else "")
    return path


# --------------------------------------------------------------------------- launchers

def write_wrappers(home):
    bindir = home / "bin"
    py = os.path.normpath(sys.executable)
    launch = os.path.normpath(str(home / "router" / "launch.py"))
    write_exact(bindir / "claude-router.cmd", '@echo off\r\n"%s" "%s" %%*\r\n' % (py, launch))
    write_exact(bindir / "claude-router.ps1", '& "%s" "%s" @args\r\nexit $LASTEXITCODE\r\n' % (py, launch))
    sh = bindir / "claude-router"
    write_exact(sh, '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (Path(py).as_posix(), Path(launch).as_posix()))
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    return bindir


# --------------------------------------------------------------------------- commands

def cmd_install(args):
    if args.scope == "project" and not args.project:
        raise common.RouterError("--scope project needs --project DIR")
    home = common.router_home()
    code = home / "router"
    try:
        total = common.judge_timeout_total(common.load_config())
    except common.RouterError:
        total = common.judge_timeout_total(common.DEFAULT_CONFIG)
    judge_timeout = max(60, int(math.ceil(total + 30)))

    # Validate everything before changing anything.
    sfile = settings_file(args.scope, args.project)
    settings = load_json_file(sfile)
    judge_entry = hook_entry(code / "judge_hook.py", judge_timeout, args.hook_style)
    guard_entry = hook_entry(code / "guard_hook.py", 15, args.hook_style)
    new_settings = copy.deepcopy(settings)
    add_router_hooks(new_settings, judge_entry, guard_entry)

    code.mkdir(parents=True, exist_ok=True)
    for name in CODE_FILES:
        shutil.copy2(str(ROOT / "router" / name), str(code / name))
    say("[ok] router code copied to %s" % code)

    cfg_path = common.config_path()
    if cfg_path.exists():
        say("[ok] kept your existing config %s" % cfg_path)
    else:
        shutil.copy2(str(ROOT / "router" / "router.config.example.json"), str(cfg_path))
        say("[todo] created %s - edit it before the first run (SETUP.md section 5)" % cfg_path)

    agents_dir = claude_dir(args.scope, args.project) / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    ours = []
    for name in AGENT_FILES:
        src = (ROOT / "agents" / name).read_text(encoding="utf-8")
        dst = agents_dir / name
        if dst.exists():
            current = dst.read_text(encoding="utf-8", errors="replace")
            if current == src:
                ours.append(name)
                say("[ok] agent %s already up to date" % name)
                continue
            if MARKER not in current and not args.force_agents:
                say("[warn] %s exists and was not installed by the router; left unchanged (use --force-agents)" % dst)
                continue
        write_exact(dst, src)
        ours.append(name)
        say("[ok] agent written: %s" % dst)

    b = backup(sfile, args.scope)
    if b:
        say("[ok] backup of your settings: %s" % b)
    write_json_file(sfile, new_settings)
    say("[ok] hooks (%s style, judge timeout %ss) written to %s" % (args.hook_style, judge_timeout, sfile))

    reg = [e for e in load_registry() if e["settings"] != str(sfile)]
    reg.append(entry_for(args.scope, args.project))
    save_registry(reg)

    if args.scope == "project":
        entries = [".claude/settings.local.json"] + [".claude/agents/%s" % n for n in ours]
        ex = set_git_exclude(Path(args.project).resolve(), entries)
        if ex:
            say("[ok] personal files added to %s (local git ignore, never committed)" % ex)
        else:
            say("[warn] %s is not a git repository (or git is missing): do not commit .claude/settings.local.json "
                "or the router agents" % args.project)

    bindir = write_wrappers(home)
    say("[ok] launchers written to %s" % bindir)
    say("")
    say("Next steps:")
    say("  1. Edit %s (Foundry resource, deployments)." % cfg_path)
    say("  2. Add %s to your PATH (or call the launcher by its full path)." % bindir)
    say("  3. python install.py doctor%s" % (" --project \"%s\"" % args.project if args.project else ""))
    say("  4. python install.py test-judge")
    say("  5. Start Claude Code with: claude-router")
    return 0


def _uninstall_entry(e):
    sfile = Path(e["settings"])
    if sfile.exists():
        try:
            settings = load_json_file(sfile)
        except common.RouterError as exc:
            say("[warn] %s; router hooks in it were not removed" % exc)
            settings = None
        if settings is not None:
            removed = remove_router_hooks(settings)
            if removed:
                b = backup(sfile, e["scope"])
                write_json_file(sfile, settings)
                say("[ok] removed %d router hook(s) from %s (backup: %s)" % (removed, sfile, b))
            else:
                say("[ok] no router hooks in %s" % sfile)
    for name in AGENT_FILES:
        p = Path(e["agents_dir"]) / name
        if p.exists() and MARKER in p.read_text(encoding="utf-8", errors="replace"):
            p.unlink()
            say("[ok] removed agent %s" % p)
    if e["scope"] == "project" and e.get("project"):
        ex = remove_git_exclude(Path(e["project"]))
        if ex:
            say("[ok] removed router entries from %s" % ex)


def cmd_uninstall(args):
    if args.scope == "project" and not args.project:
        raise common.RouterError("--scope project needs --project DIR")
    home = common.router_home()
    reg = load_registry()
    this = entry_for(args.scope, args.project)
    targets = reg + [this] if args.purge else [this]
    seen = set()
    for e in targets:
        if e["settings"] in seen:
            continue
        seen.add(e["settings"])
        _uninstall_entry(e)
    reg = [e for e in reg if e["settings"] not in seen]

    # Code stays while any settings file still points at it (registered installs + the user settings file).
    candidates = {e["settings"] for e in reg} | {str(settings_file("user", None))}
    still = sorted(p for p in candidates if p not in seen and Path(p).exists() and settings_still_hooked(p))
    if args.purge:
        shutil.rmtree(str(home), ignore_errors=True)
        say("[ok] removed %s (config, logs, state, backups)" % home)
        if still:
            say("[warn] these settings files still reference the router and now do nothing: %s" % ", ".join(still))
        return 0
    save_registry(reg)
    if still:
        say("[ok] router code kept: still installed for %s" % ", ".join(still))
        return 0
    for sub in ("router", "bin", "generated", "judge-cwd", "state"):
        shutil.rmtree(str(home / sub), ignore_errors=True)
    say("[ok] removed router code; kept %s, logs and backups (use --purge to delete them)" % common.config_path())
    return 0


def _script_of(hook):
    for s in _hook_strings(hook):
        for name in HOOK_SCRIPTS:
            if name in s:
                i = s.find(name)
                start = max(s.rfind("'", 0, i), s.rfind('"', 0, i)) + 1
                return s[start:i + len(name)]
    return None


def cmd_doctor(args):
    problems = {"fail": 0, "warn": 0}

    def report(level, msg):
        if level in problems:
            problems[level] += 1
        say("[%s] %s" % (level, msg))

    if sys.version_info < (3, 8):
        report("fail", "Python %s is too old; need 3.8 or newer" % platform.python_version())
    else:
        report("ok", "Python %s at %s" % (platform.python_version(), sys.executable))

    cfg = None
    if not common.config_path().exists():
        report("fail", "no config at %s - run: python install.py install" % common.config_path())
    else:
        try:
            cfg = common.load_config()
            report("ok", "config is valid: %s" % common.config_path())
        except common.RouterError as exc:
            report("fail", "config problem: %s" % exc)

    if cfg:
        f = cfg["foundry"]
        if f.get("resource") in ("", "YOUR-FOUNDRY-RESOURCE-NAME") and not f.get("base_url"):
            report("fail", "set foundry.resource (or foundry.base_url) in the config")
        else:
            report("ok", "Foundry endpoint: %s" % (f.get("base_url") or f.get("resource")))
        if f.get("auth") == "api_key_env":
            name = f.get("api_key_env")
            if os.environ.get(name):
                report("ok", "Foundry API key found in %s" % name)
            else:
                report("warn", "foundry.auth is api_key_env but %s is not set in this shell" % name)
        else:
            if shutil.which("az"):
                report("ok", "Entra ID auth: Azure CLI found (run `az login` if you have not)")
            else:
                report("warn", "Entra ID auth: Azure CLI (az) not found; Claude Code needs some Azure credential "
                               "(az login, managed identity, environment credentials)")
        for t in common.TIERS:
            spec = cfg["tiers"][t]
            if spec.get("deployment"):
                extra = "" if spec["deployment"] == spec["model_id"] else " (modelOverrides -> %s)" % spec["deployment"]
                report("ok", "tier %-8s -> %s%s" % (t, spec["model_id"], extra))
            else:
                report("warn", "tier %-8s has no deployment; it uses %s instead" % (t, common.effective_tier(t, cfg)))
        first = cfg["judge"]["chain"][0]
        if first.get("backend") != "rules" and first.get("effort") in ("xhigh", "max"):
            report("warn", "judge effort is %s: every new prompt may wait tens of seconds; try high if it feels slow"
                   % first["effort"])
        try:
            cmd = common.resolve_claude_cmd(cfg)
            if IS_WINDOWS and cmd[0].lower().endswith((".cmd", ".bat", ".ps1")):
                report("warn", "claude resolves to %s; the native installer (claude.exe) is recommended" % cmd[0])
            try:
                p = subprocess.run(cmd + ["--version"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30)
                ver = (p.stdout or p.stderr).strip().splitlines()
                report("ok" if p.returncode == 0 else "fail", "claude --version: %s" % (ver[0] if ver else "no output"))
            except (OSError, subprocess.SubprocessError) as exc:
                report("fail", "could not run claude: %s" % exc)
        except common.RouterError as exc:
            report("fail", str(exc))

    files = [Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json"]
    if args.project:
        pr = Path(args.project).resolve() / ".claude"
        files += [pr / "settings.json", pr / "settings.local.json"]
    files += [Path(e["settings"]) for e in load_registry()]
    hooks_found, checked = 0, set()
    for p in files:
        if str(p) in checked or not p.exists():
            continue
        checked.add(str(p))
        try:
            data = load_json_file(p)
        except common.RouterError as exc:
            report("fail", str(exc))
            continue
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        bad = sorted(k for k in env if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_USE_"))
        if bad:
            report("warn", "%s sets %s in its env block. Settings env overrides the shell; move these into the "
                           "router config so the judge's subscription call stays clean." % (p, ", ".join(bad)))
        found = find_router_hooks(data)
        hooks_found += len(found)
        for event, h in found:
            script = _script_of(h)
            if script and not Path(script).exists():
                report("warn", "%s: %s hook points at missing %s (it does nothing); run install again" % (p, event, script))
            t = h.get("timeout")
            if cfg and event == "UserPromptSubmit" and common.is_number(t):
                need = common.judge_timeout_total(cfg) + 10
                if t < need:
                    report("warn", "judge hook timeout %ss in %s is below the judge chain total (%ss); run install again"
                           % (t, p, int(need - 10)))
    if hooks_found >= 2:
        report("ok", "router hooks installed (%d entries in %s)" % (hooks_found, ", ".join(sorted(checked))))
    else:
        report("fail", "router hooks not found in %s" % ", ".join(str(p) for p in files))

    agent_dirs = [Path.home() / ".claude" / "agents"] + [Path(e["agents_dir"]) for e in load_registry()]
    if args.project:
        agent_dirs.append(Path(args.project).resolve() / ".claude" / "agents")
    for name in AGENT_FILES:
        if any((d / name).exists() for d in agent_dirs):
            report("ok", "agent %s present" % name)
        else:
            report("fail", "agent %s missing" % name)

    managed = [Path("C:/Program Files/ClaudeCode/managed-settings.json"),
               Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
               Path("/etc/claude-code/managed-settings.json")]
    for p in managed:
        if p.exists():
            try:
                data = load_json_file(p)
                keys = sorted(k for k in data if any(w in k.lower() for w in ("hook", "provider", "login", "env", "model")))
                report("warn", "organisation-managed settings found at %s; keys to check with IT: %s"
                       % (p, ", ".join(keys) or "none"))
            except common.RouterError:
                report("warn", "organisation-managed settings found at %s (unreadable)" % p)
    if IS_WINDOWS:
        bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash")
        report("ok" if bash else "warn", "Git Bash: %s" % (bash or "not found (fine with the default exec hook style)"))
    say("")
    say("doctor: %d failure(s), %d warning(s)" % (problems["fail"], problems["warn"]))
    return 1 if problems["fail"] else 0


def cmd_test_judge(args):
    cfg = common.load_config()
    prompt = args.prompt or ("The CSV export sometimes produces duplicated rows when two users export at the "
                             "same time. Find out why and fix it.")
    payload = judge_hook.build_payload(prompt, os.getcwd(), {}, cfg)
    say("prompt: %s" % prompt)
    failures = 0
    for step in cfg["judge"]["chain"]:
        b = step["backend"]
        if args.backend != "all" and b != args.backend:
            continue
        label = b if b == "rules" else "%s:%s (effort %s, timeout %ss)" % (b, step["model"], step.get("effort"), step.get("timeout_s", 60))
        t0 = time.time()
        if b == "rules":
            v = judge_hook.classify_with_rules(prompt)
            say("[ok]   %-45s tier=%s reason=%s" % (label, v["tier"], v["reason"]))
            continue
        try:
            v, cost = judge_hook.call_claude(step, payload, cfg)
            say("[ok]   %-45s tier=%s confidence=%s %.1fs cost=%s reason=%s" % (
                label, v["tier"], v["confidence"], time.time() - t0,
                "n/a" if cost is None else "$%.4f" % cost, v["reason"]))
        except (RuntimeError, common.RouterError) as exc:
            failures += 1
            say("[fail] %-45s %.1fs %s" % (label, time.time() - t0, exc))
    return 1 if failures else 0


def cmd_report(args):
    p = common.router_home() / "logs" / "decisions.jsonl"
    if not p.exists():
        say("no decisions logged yet (%s)" % p)
        return 0
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            if isinstance(r, dict):
                rows.append(r)
        except ValueError:
            continue
    if not rows:
        say("log is empty")
        return 0

    def count(key):
        out = {}
        for r in rows:
            k = str(r.get(key))
            out[k] = out.get(k, 0) + 1
        return ", ".join("%s=%d" % kv for kv in sorted(out.items(), key=lambda kv: -kv[1]))

    judged = [r for r in rows if r.get("source") not in ("override", "sticky", "escalation", "slash-command")]
    ms = sorted(int(r["ms"]) for r in judged if common.is_number(r.get("ms")))
    failed = {}
    for r in rows:
        for a in r.get("attempts") if isinstance(r.get("attempts"), list) else []:
            if isinstance(a, dict) and not a.get("ok"):
                k = str(a.get("step"))
                failed[k] = failed.get(k, 0) + 1
    cost = sum(float(r["cost_usd"]) for r in rows if common.is_number(r.get("cost_usd")))
    say("decisions: %d (%s .. %s)" % (len(rows), rows[0].get("ts"), rows[-1].get("ts")))
    say("tiers:     %s" % count("tier"))
    say("sources:   %s" % count("source"))
    if ms:
        say("judge latency: median %.1fs, p90 %.1fs, max %.1fs (%d judged prompts)" % (
            ms[len(ms) // 2] / 1000.0, ms[min(len(ms) - 1, int(len(ms) * 0.9))] / 1000.0, ms[-1] / 1000.0, len(ms)))
    say("judge failures: %s" % (", ".join("%s=%d" % kv for kv in failed.items()) or "none"))
    say("reported judge cost: $%.4f (subscription calls may report a notional cost)" % cost)
    if args.last:
        say("")
        for r in rows[-args.last:]:
            say("%s %-8s %-22s %s" % (r.get("ts"), r.get("tier"), r.get("source"), str(r.get("reason") or "")[:90]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="claude-model-router installer and tools (v%s)" % common.VERSION)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("install")
    p.add_argument("--scope", choices=("user", "project"), default="user")
    p.add_argument("--project")
    p.add_argument("--hook-style", choices=("exec", "bash", "powershell"), default="exec")
    p.add_argument("--force-agents", action="store_true")
    p = sub.add_parser("uninstall")
    p.add_argument("--scope", choices=("user", "project"), default="user")
    p.add_argument("--project")
    p.add_argument("--purge", action="store_true")
    p = sub.add_parser("doctor")
    p.add_argument("--project")
    p = sub.add_parser("test-judge")
    p.add_argument("--prompt")
    p.add_argument("--backend", choices=("all", "subscription", "foundry", "rules"), default="all")
    p = sub.add_parser("report")
    p.add_argument("--last", type=int, default=0)
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    handler = {"install": cmd_install, "uninstall": cmd_uninstall, "doctor": cmd_doctor,
               "test-judge": cmd_test_judge, "report": cmd_report}[args.cmd]
    try:
        return handler(args)
    except common.RouterError as exc:
        say("[fail] %s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
