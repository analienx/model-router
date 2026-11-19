"""Local stand-in for the Anthropic Messages API (also serves Foundry-style /anthropic/v1/messages).

Used by run_e2e.py to drive the REAL Claude Code CLI through scripted conversations, so the router's
hooks, judge, guard and launcher are exercised end to end without any model, account or network.

Environment:
  MOCK_PORT_FILE   where to write the chosen port
  MOCK_LOG         JSON-lines log of every request (for assertions)
  MOCK_WORKDIR     folder the scripted tool calls write into
  MOCK_VERDICT     tier the scripted judge returns (default: deep)
  MOCK_THROTTLE_MODEL  model name that gets HTTP 429 (simulates a throttled deployment)

Conversation scripts are selected by a marker in the first user message: [[e2e:<scenario>]].
Subagent conversations are recognised by the marker SUBAGENT-TASK in their prompt.
Standard library only.
"""
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LOG", "mock-requests.jsonl")
WORKDIR = os.environ.get("MOCK_WORKDIR", ".")
VERDICT = os.environ.get("MOCK_VERDICT", "deep")
THROTTLE_MODEL = os.environ.get("MOCK_THROTTLE_MODEL", "")
_lock = threading.Lock()


def lines(n, word):
    return "".join("%s line %d\n" % (word, i + 1) for i in range(n))


def path(name):
    return os.path.join(WORKDIR, name)


# Each script is a list of tool calls made one after another; after the last one the "model" ends the turn.
MAIN_SCRIPTS = {
    "deep": [
        ("Write", lambda: {"file_path": path("main_big.txt"), "content": lines(50, "main")}),
        ("Agent", lambda: {"description": "implement change", "subagent_type": "implementer",
                           "prompt": "SUBAGENT-TASK write arch_big.txt"}),
        ("Agent", lambda: {"description": "design change", "subagent_type": "architect",
                           "prompt": "SUBAGENT-TASK write arch_big.txt"}),
    ],
    "fast": [
        ("Write", lambda: {"file_path": path("fast_small.txt"), "content": lines(5, "fast")}),
        ("Write", lambda: {"file_path": path("fast_big.txt"), "content": lines(100, "fast")}),
    ],
    "throttle": [
        ("Agent", lambda: {"description": "design change", "subagent_type": "architect",
                           "prompt": "SUBAGENT-TASK write fallback.txt"}),
        ("Agent", lambda: {"description": "fallback after 429", "subagent_type": "deep-haiku",
                           "prompt": "SUBAGENT-TASK write fallback.txt"}),
    ],
    "plain": [],
}
SUBAGENT_FILE_RE = re.compile(r"SUBAGENT-TASK write ([\w.]+)")


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            out.append(text_of(b.get("content", "")))
    return "\n".join(out)


def system_text(body):
    s = body.get("system", "")
    return s if isinstance(s, str) else text_of(s)


def tool_results(messages):
    names = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b.get("id")] = (b.get("name"), (b.get("input") or {}).get("subagent_type"))
    out = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    name, sub = names.get(b.get("tool_use_id"), (None, None))
                    out.append({"tool": name, "subagent_type": sub, "is_error": bool(b.get("is_error")),
                                "text": text_of(b.get("content", ""))[:1500]})
    return out


def first_user_text(messages):
    for m in messages:
        if m.get("role") == "user":
            return text_of(m.get("content", ""))
    return ""


def last_user_text(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return text_of(m.get("content", ""))
    return ""


def called_subagents(messages):
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                    out.append((b.get("input") or {}).get("subagent_type"))
    return out


def agent_tool(tools):
    return "Task" if "Agent" not in tools and "Task" in tools else "Agent"


def decide(body):
    """Return (kind, actor, blocks, stop_reason) for one request."""
    messages = body.get("messages") or []
    sysx = system_text(body)
    tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
    first = first_user_text(messages)
    results = tool_results(messages)
    if "You are a routing judge" in sysx:
        verdict = {"tier": VERDICT, "confidence": 0.9, "reason": "mock judge verdict"}
        return "judge", "judge", [{"type": "text", "text": json.dumps(verdict)}], "end_turn"
    if not tools:
        return "aux", "aux", [{"type": "text", "text": "ok"}], "end_turn"
    m = SUBAGENT_FILE_RE.search(first)
    if m:
        if not results:
            call = {"file_path": path(m.group(1)), "content": lines(200, "subagent")}
            return "tool_use", "subagent", [_tool("Write", call)], "tool_use"
        return "text", "subagent", [{"type": "text", "text": "subagent done"}], "end_turn"
    sm = re.search(r"\[\[e2e:(\w+)\]\]", first)
    scenario = sm.group(1) if sm else "plain"
    if scenario == "parallel":
        # Three architect calls in ONE response: Claude Code runs them (and their PreToolUse hooks) in parallel.
        if not called_subagents(messages):
            calls = [_tool(agent_tool(tools), {"description": "part %d" % i, "subagent_type": "architect",
                                               "prompt": "SUBAGENT-TASK write par_%d.txt" % i}) for i in range(3)]
            return "tool_use", "main", calls, "tool_use"
        return "text", "main", [{"type": "text", "text": "main done"}], "end_turn"
    if scenario == "throttle":
        # Subagents run in the background: their failure arrives later as a notification message.
        called = called_subagents(messages)
        if "architect" not in called:
            return "tool_use", "main", [_tool(agent_tool(tools), MAIN_SCRIPTS["throttle"][0][1]())], "tool_use"
        if "deep-haiku" not in called:
            # Only the architect's failure notification counts. Its ids and output path are left out, and 429 must
            # stand alone: a random agent id such as "a73c74297540d6f7e" once fired this too early.
            last = last_user_text(messages)
            error_text = re.sub(r"<(task-id|tool-use-id|output-file)>.*?</\1>", " ", last, flags=re.S)
            if "<task-notification>" in last and re.search(r"\b(429|529)\b|rate.?limit|overloaded", error_text, re.I):
                return "tool_use", "main", [_tool(agent_tool(tools), MAIN_SCRIPTS["throttle"][1][1]())], "tool_use"
            return "text", "main", [{"type": "text", "text": "waiting for architect"}], "end_turn"
        return "text", "main", [{"type": "text", "text": "main done"}], "end_turn"
    script = MAIN_SCRIPTS.get(scenario, [])
    step = len(results)
    if step < len(script):
        name, make = script[step]
        if name == "Agent" and "Agent" not in tools and "Task" in tools:
            name = "Task"  # older Claude Code builds call the subagent tool Task
        return "tool_use", "main", [_tool(name, make())], "tool_use"
    return "text", "main", [{"type": "text", "text": "main done"}], "end_turn"


def _tool(name, args):
    return {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": name, "input": args}


def find_effort(obj):
    """The effort value anywhere in a request body (Claude Code sends it inside the request options)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "effort" and isinstance(v, str):
                return v
            found = find_effort(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_effort(v)
            if found:
                return found
    return None


def log(record):
    with _lock:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # close after each response; fine for SSE

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj, headers=None):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/").endswith("/v1/models") or "/v1/models?" in self.path:
            return self._json(200, {"data": [], "has_more": False})
        self._json(200, {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad json"}})
        p = self.path.split("?")[0]
        if p.endswith("/v1/messages/count_tokens"):
            return self._json(200, {"input_tokens": 1000})
        if not p.endswith("/v1/messages"):
            return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": p}})
        model = body.get("model", "")
        messages = body.get("messages") or []
        whole = json.dumps(body)
        ctx = whole.find("[router] ")
        record = {"path": p, "model": model, "stream": bool(body.get("stream")),
                  "tools": [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)],
                  "system_head": system_text(body)[:160], "first_user": first_user_text(messages)[:300],
                  "last_user": last_user_text(messages)[:600],
                  "system_len": len(system_text(body)),
                  "system_has_default_prompt": "official CLI for Claude" in system_text(body),
                  "tool_results": tool_results(messages),
                  "router_context": whole[ctx:ctx + 400] if ctx >= 0 else None,
                  "effort": find_effort(body),
                  "auth_headers": sorted(h for h in self.headers.keys() if h.lower() in ("x-api-key", "api-key", "authorization"))}
        if THROTTLE_MODEL and model == THROTTLE_MODEL:
            record["response"] = "429"
            log(record)
            return self._json(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                               "message": "mock deployment is rate limited (429)"}},
                              {"retry-after": "1"})
        kind, actor, blocks, stop = decide(body)
        record.update({"kind": kind, "actor": actor, "response": blocks, "stop_reason": stop})
        log(record)
        msg_id = "msg_" + uuid.uuid4().hex[:24]
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
            return self._json(200, {"id": msg_id, "type": "message", "role": "assistant", "model": model,
                                    "content": blocks, "stop_reason": stop, "stop_sequence": None, "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def ev(name, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode("utf-8"))
            self.wfile.flush()
        ev("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": dict(usage, output_tokens=1)}})
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": 5}})
        ev("message_stop", {"type": "message_stop"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file = os.environ.get("MOCK_PORT_FILE")
    if port_file:
        with open(port_file + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(str(server.server_address[1]))
        os.replace(port_file + ".tmp", port_file)
    sys.stdout.write("mock Anthropic API on 127.0.0.1:%d\n" % server.server_address[1])
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
