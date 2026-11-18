"""Stand-in for the `claude` CLI used by the tests. Never calls any model.

Behaviour is chosen per backend: FAKE_CLAUDE_MODE_SUBSCRIPTION / FAKE_CLAUDE_MODE_FOUNDRY
(falls back to FAKE_CLAUDE_MODE). The backend is detected from CLAUDE_CODE_USE_FOUNDRY,
which is exactly what the router must strip for subscription calls.
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if "--version" in args:
    print("9.9.9 (Fake Claude Code)")
    sys.exit(0)

stdin = sys.stdin.buffer.read().decode("utf-8", "replace")
backend = "foundry" if os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1" else "subscription"
mode = os.environ.get("FAKE_CLAUDE_MODE_" + backend.upper()) or os.environ.get("FAKE_CLAUDE_MODE", "deep")

record = os.environ.get("FAKE_CLAUDE_RECORD")
if record:
    env_view = {k: ("<set>" if any(h in k for h in ("KEY", "TOKEN")) else v)
                for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE"))}
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"backend": backend, "mode": mode, "args": args, "stdin": stdin,
                             "cwd": os.getcwd(), "env": env_view}) + "\n")


def result(text, is_error=False, cost=0.0123):
    print(json.dumps({"type": "result", "subtype": "success" if not is_error else "error",
                      "is_error": is_error, "result": text, "total_cost_usd": cost}))


VERDICTS = {
    "deep": {"tier": "deep", "confidence": 0.9, "reason": "schema change with migration"},
    "standard": {"tier": "standard", "confidence": 0.8, "reason": "multi-file feature"},
    "fast": {"tier": "fast", "confidence": 0.95, "reason": "simple question"},
    "fast_low": {"tier": "fast", "confidence": 0.3, "reason": "unsure"},
    "synonym": {"tier": "HARD", "confidence": 0.7, "reason": "synonym tier"},
    "bad_tier": {"tier": "galaxy-brain", "confidence": 0.7, "reason": "invalid tier"},
}

if mode in VERDICTS:
    result(json.dumps(VERDICTS[mode]))
elif mode == "prose_json":
    result('Sure! Here is my verdict:\n```json\n{"tier": "standard", "confidence": 0.8, "reason": "multi-file"}\n```')
elif mode == "garbage":
    result("I think this one is pretty hard, honestly.")
elif mode == "json_array":
    print(json.dumps([{"type": "system"}, {"type": "result", "is_error": False,
                                          "result": json.dumps(VERDICTS["deep"])}]))
elif mode == "is_error":
    result("API Error: Request rejected (429)", is_error=True)
elif mode == "crash":
    sys.stderr.write("boom: not logged in\n")
    sys.exit(1)
elif mode == "plain_text_verdict":
    print('{"tier": "fast", "confidence": 0.9, "reason": "plain"}')
elif mode == "sleep":
    time.sleep(10)
    result(json.dumps(VERDICTS["deep"]))
elif mode == "grandchild_hang":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # inherits our stdout/stderr
    pid_file = os.environ.get("FAKE_CLAUDE_PID_FILE")
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))
    time.sleep(30)
elif mode == "fail_if_called":
    sys.stderr.write("judge must not be called in this test\n")
    sys.exit(3)
else:
    sys.stderr.write("unknown fake mode %s\n" % mode)
    sys.exit(4)
