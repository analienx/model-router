"""Start Claude Code on Microsoft Foundry with the router switched on.

    claude-router [any normal claude arguments]
    claude-router --router-dry-run          # print what would run, secrets masked

Sets the Foundry variables only for this process tree, so your settings files stay
free of provider variables and the judge can still use your subscription login.
"""
import os
import subprocess
import sys

sys.dont_write_bytecode = True
import common  # noqa: E402

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def build_launch(cfg, argv, environ):
    env = common.foundry_env(cfg, environ)
    env["CLAUDE_CODE_MAX_RETRIES"] = str(cfg["launcher"]["max_retries"])
    env["CLAUDE_ROUTER"] = "1"
    env.pop("CLAUDE_ROUTER_JUDGE", None)
    cmd = common.resolve_claude_cmd(cfg) + ["--model", "haiku"]
    fb = cfg["launcher"].get("fallback_models") or []
    if fb:
        cmd += ["--fallback-model", ",".join(fb)]
    settings = common.foundry_settings_file(cfg)
    if settings:
        cmd += ["--settings", str(settings)]
    cmd += list(cfg["launcher"].get("extra_args") or []) + list(argv)
    return cmd, env


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


def main(argv):
    dry = "--router-dry-run" in argv
    argv = [a for a in argv if a != "--router-dry-run"]
    try:
        cfg = common.load_config()
        cmd, env = build_launch(cfg, argv, os.environ)
    except common.RouterError as exc:
        sys.stderr.write("[router] cannot start: %s\n" % exc)
        return 2
    models = common.alias_models(cfg)
    judge = cfg["judge"]["chain"][0]
    banner = "[router] Foundry %s | haiku=%s sonnet=%s opus=%s | judge: %s" % (
        env.get("ANTHROPIC_FOUNDRY_RESOURCE") or env.get("ANTHROPIC_FOUNDRY_BASE_URL"),
        models["haiku"], models["sonnet"], models["opus"],
        "rules" if judge["backend"] == "rules" else "%s %s (%s)" % (judge["backend"], judge["model"], judge.get("effort", "default")))
    sys.stderr.write(banner + "\n")
    if dry:
        sys.stdout.write("command:\n  %s\nenvironment changes made by the router:\n%s\n" % (
            " ".join(cmd), describe_changes(env, os.environ)))
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
