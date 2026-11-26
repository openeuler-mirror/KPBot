#!/usr/bin/env python3
"""mock_flink.py — a file-backed fake Flink REST server plus state helpers.

The server serves GET /jobs/overview and /jobs/<jid>/plan from a JSON state
file that every handler re-reads per request, so tests (or the mock
sql-client) can flip job states by editing the file. Plans mimic the real
REST shape: a gluten node carries its engine marker in engine-generated
fields (operator name, "GlutenHASH" ship strategy), while "description"
embeds user SQL text — the clean-plan source even reads from a table named
gluten_orders — which gluten detection must never rely on; a fallen-back
job carries no engine marker at all, only the identifier-rich description.

CLI (used by the mock sql-client.sh):
  add-job <state> <job-name> <job-state> <gluten-nodes> [exception]  -> jid
  poison <state>   /jobs/overview answers a bare JSON list (tool-abort tests)
  dead <state>     every request answers 503 (REST gone after the submit)
  refuse-cancel <state>  PATCH /jobs/<jid>/cancel answers 404 (the cancel
      cannot be confirmed → the remaining scenarios are skipped)
"""
import argparse
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def load_state(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path, state):
    """Atomic write (tmp + rename) — the server may read concurrently."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def empty_state():
    return {"overview": [], "details": {}, "plans": {}, "exceptions": {}}


def make_plan_nodes(gluten_nodes):
    nodes = []
    for i in range(max(gluten_nodes, 1)):
        if i < gluten_nodes:
            nodes.append({
                "description": "Source: datagen[1]<br/>+- Gluten cal operator",
                "id": str(i + 1),
                "inputs": ([{"id": "0", "ship_strategy": "GlutenHASH",
                             "exchange": "pipelined_bounded"}] if i else []),
                "operator": "Gluten cal" if i == 0 else "n/a",
                "operator_strategy": "n/a",
                "parallelism": 1,
            })
        else:
            # user SQL text only — matching must not rely on it: a table
            # named gluten_orders must never mark this node as a gluten node
            nodes.append({
                "description": "Source: gluten_orders[1]<br/>+- GroupAggregate"
                               "(key: [gluten_bid])",
                "id": str(i + 1),
                "inputs": [{"id": str(i), "ship_strategy": "HASH",
                            "exchange": "pipelined_bounded"}],
                "operator": "n/a",
                "operator_strategy": "n/a",
                "parallelism": 1,
            })
    return nodes


def add_job(path, name, state, gluten_nodes=0, exception=""):
    st = load_state(path)
    jid = uuid.uuid4().hex
    entry = {"jid": jid, "name": name, "state": state,
             "start-time": 0, "end-time": -1, "duration": 1234}
    st["overview"].append(entry)
    st["details"][jid] = dict(entry, vertices=[])
    st["plans"][jid] = {"jid": jid, "name": name, "type": "STREAMING",
                        "nodes": make_plan_nodes(gluten_nodes)}
    st["exceptions"][jid] = {"root-exception": exception or ""}
    save_state(path, st)
    return jid


def set_state(path, jid, state):
    st = load_state(path)
    for job in st["overview"]:
        if job["jid"] == jid:
            job["state"] = state
    if jid in st["details"]:
        st["details"][jid]["state"] = state
    save_state(path, st)


def set_poison(path):
    st = load_state(path)
    st["poison"] = True
    save_state(path, st)


def set_dead(path):
    st = load_state(path)
    st["dead"] = True
    save_state(path, st)


def refuse_cancel(path):
    st = load_state(path)
    st["cancel_refused"] = True
    save_state(path, st)


def cancel_job(path, jid):
    """PATCH /jobs/<jid>/cancel: flips the job to CANCELED (both views)."""
    st = load_state(path)
    for job in st["overview"]:
        if job["jid"] == jid:
            job["state"] = "CANCELED"
    if jid in st["details"]:
        st["details"][jid]["state"] = "CANCELED"
    save_state(path, st)


class Handler(BaseHTTPRequestHandler):
    state_path = None  # set by serve()

    def do_GET(self):
        try:
            state = load_state(self.state_path)
        except Exception as exc:  # state file unreadable mid-write
            self._send_json({"error": str(exc)}, 500)
            return
        if state.get("dead"):  # REST gone: every request fails
            self._send_json({"errors": ["dead"]}, 503)
            return
        path = urlparse(self.path).path.rstrip("/")
        if path == "/jobs/overview":
            if state.get("poison"):  # valid JSON, broken shape
                self._send_json([])
                return
            self._send_json({"jobs": state["overview"]})
            return
        m = re.match(r"^/jobs/([0-9a-f]+)$", path)
        if m and m.group(1) in state["details"]:
            self._send_json(state["details"][m.group(1)])
            return
        m = re.match(r"^/jobs/([0-9a-f]+)/plan$", path)
        if m and m.group(1) in state["plans"]:
            # the real REST API wraps the plan: {"plan": {...}}
            self._send_json({"plan": state["plans"][m.group(1)]})
            return
        m = re.match(r"^/jobs/([0-9a-f]+)/exceptions$", path)
        if m and m.group(1) in state["exceptions"]:
            self._send_json(state["exceptions"][m.group(1)])
            return
        self._send_json({"errors": ["not found"]}, 404)

    def do_PATCH(self):
        try:
            state = load_state(self.state_path)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)
            return
        if state.get("dead"):
            self._send_json({"errors": ["dead"]}, 503)
            return
        m = re.match(r"^/jobs/([0-9a-f]+)/cancel$", urlparse(self.path).path.rstrip("/"))
        if state.get("cancel_refused"):  # a REST that refuses the cancel
            self._send_json({"errors": ["refused"]}, 404)
            return
        if m and m.group(1) in state["details"]:
            cancel_job(self.state_path, m.group(1))
            self._send_json({"status": "success"})
            return
        self._send_json({"errors": ["not found"]}, 404)

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(state_path):
    """Start the server on 127.0.0.1:<random port>; return the HTTPServer
    (shut it down with server.shutdown() + server.server_close())."""
    Handler.state_path = state_path
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-job", help="register a job, print its jid")
    p.add_argument("state_file")
    p.add_argument("job_name")
    p.add_argument("job_state")
    p.add_argument("gluten_nodes", type=int)
    p.add_argument("exception", nargs="?", default="")
    p = sub.add_parser("poison", help="make /jobs/overview answer a bare JSON list")
    p.add_argument("state_file")
    d = sub.add_parser("dead", help="answer 503 to every request (REST gone)")
    d.add_argument("state_file")
    r = sub.add_parser("refuse-cancel", help="answer 404 to every PATCH /cancel")
    r.add_argument("state_file")
    args = parser.parse_args(argv)
    if args.cmd == "add-job":
        print(add_job(args.state_file, args.job_name, args.job_state,
                      args.gluten_nodes, args.exception))
        return 0
    if args.cmd == "poison":
        set_poison(args.state_file)
        return 0
    if args.cmd == "dead":
        set_dead(args.state_file)
        return 0
    if args.cmd == "refuse-cancel":
        refuse_cancel(args.state_file)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
