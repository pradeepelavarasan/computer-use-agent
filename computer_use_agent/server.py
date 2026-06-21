"""
Computer Use Agent Server — Session 10
Port: 8002  (shopping_agent is 8001, both can run simultaneously)

Accepts a natural-language desktop task, feeds it to the Session 10 DAG
orchestrator (code/flow.py), and returns the formatter node's final answer
as plain text.  The graph viewer tab polls /api/graph-status for live DAG
updates while the task is running.
"""

from __future__ import annotations
import json
import os
import re
import signal
import sys
import subprocess
import threading
import urllib.parse
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8002
ROOT = Path(__file__).resolve().parent          # computer_use_agent/
PROJECT_ROOT = ROOT.parent                       # EAG Session10 Computer Agent/

# Track the most recently started session ID so /api/graph-status can find it
_active_session_id: str | None = None

# Reference to the currently running flow.py subprocess — killed on Stop / Ctrl+C
_active_proc: subprocess.Popen | None = None

# Set by handle_stop so handle_search knows the user explicitly stopped the task
_stop_event = threading.Event()


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """Kill proc AND all its children (handles 'uv run' wrapper → python child).

    Strategy: send SIGKILL to the entire process group started with
    start_new_session=True.  Falls back to proc.kill() if pgid lookup fails.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.kill()
    except Exception:
        pass


class ComputerAgentHandler(SimpleHTTPRequestHandler):

    # ── GET handlers ────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.serve_file(ROOT / "index.html", "text/html")
        elif self.path.startswith("/static/"):
            rel_path = self.path[8:]
            file_path = ROOT / "static" / rel_path
            content_type = "application/octet-stream"
            if file_path.suffix == ".css":
                content_type = "text/css"
            elif file_path.suffix == ".js":
                content_type = "application/javascript"
            elif file_path.suffix in (".png", ".jpg", ".jpeg"):
                content_type = f"image/{file_path.suffix[1:]}"
            self.serve_file(file_path, content_type)
        elif self.path == "/graph" or self.path.startswith("/graph?"):
            self.serve_file(ROOT / "graph_viewer.html", "text/html")
        elif self.path == "/api/graph-status" or self.path.startswith("/api/graph-status?"):
            self.handle_graph_status()
        else:
            self.send_error(404, "Not Found")

    def serve_file(self, file_path, content_type):
        if not file_path.exists() or file_path.is_dir():
            self.send_error(404, "File Not Found")
            return
        try:
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Internal Server Error: {e}")

    def handle_graph_status(self):
        """Return the latest session's graph.json for live DAG polling.

        Resolution order for session ID:
          1. Explicit ?session_id= query param
          2. The globally tracked _active_session_id
          3. Auto-discover: most recently modified s9-* directory under logs/
        """
        global _active_session_id
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        sid = params.get("session_id", [None])[0] or _active_session_id

        if not sid:
            logs_dir = PROJECT_ROOT / "logs"
            if logs_dir.exists():
                candidates = sorted(
                    [d for d in logs_dir.iterdir() if d.is_dir() and d.name.startswith("s9-")],
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    sid = candidates[0].name

        if not sid:
            self.send_json({"session_id": None, "nodes": [], "edges": [], "query": "", "status": "idle"})
            return

        graph_path = PROJECT_ROOT / "logs" / sid / "graph.json"
        query_path = PROJECT_ROOT / "logs" / sid / "query.txt"
        query = query_path.read_text(encoding="utf-8").strip() if query_path.exists() else ""

        if not graph_path.exists():
            self.send_json({"session_id": sid, "nodes": [], "edges": [], "query": query, "status": "starting"})
            return

        try:
            graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_error_response(f"Failed to read graph: {e}")
            return

        all_nodes = graph_data.get("nodes", [])
        running = any(n.get("status") in ("pending", "running") for n in all_nodes)
        status = "running" if running else "complete"

        self.send_json({
            "session_id": sid,
            "nodes": all_nodes,
            "edges": graph_data.get("edges", []),
            "query": query,
            "status": status,
        })

    # ── POST handlers ───────────────────────────────────────────────────────────

    def do_POST(self):
        if self.path == "/api/search":
            self.handle_search()
        elif self.path == "/api/stop":
            self.handle_stop()
        else:
            self.send_error(404, "Not Found")

    def handle_stop(self):
        """Kill the active flow.py subprocess (and all its children) without stopping the server."""
        global _active_proc
        proc = _active_proc
        if proc and proc.poll() is None:
            _stop_event.set()        # signal handle_search thread to return cleanly
            _kill_proc_tree(proc)
            _active_proc = None
            print("[server] Task killed by user (Stop button)")
            self.send_json({"stopped": True, "message": "Task stopped."})
        else:
            _active_proc = None
            self.send_json({"stopped": False, "message": "No task was running."})

    def handle_search(self):
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)
        req = json.loads(post_data.decode("utf-8"))
        query = req.get("query", "").strip()

        if not query:
            self.send_error_response("Empty query received.")
            return

        try:
            # ── Invoke the DAG orchestrator with streaming stdout ──────────────
            # start_new_session=True puts the subprocess in its own process group
            # so _kill_proc_tree() can send SIGKILL to uv AND its python child.
            print(f"[server] Running task: {query}")
            global _active_session_id, _active_proc
            _stop_event.clear()

            proc = subprocess.Popen(
                ["uv", "run", "code/flow.py", query],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                start_new_session=True,   # own process group → killpg kills all children
            )

            stdout_lines = []
            session_id = None
            _active_proc = proc

            for line in proc.stdout:
                stdout_lines.append(line)
                print(line, end="")
                if session_id is None:
                    m = re.search(r"session (s9-[\w\d_-]+)", line)
                    if m:
                        session_id = m.group(1)
                        _active_session_id = session_id
                        print(f"[server] Session detected: {session_id}")

            proc.wait()
            stdout_text = "".join(stdout_lines)

            stderr_text = proc.stderr.read()
            if stderr_text:
                print(stderr_text, file=sys.stderr)

            # ── User hit Stop — return early with a clean message ─────────────
            if _stop_event.is_set():
                _active_proc = None
                self.send_json({
                    "success": False,
                    "stopped": True,
                    "result": "Task was stopped by the user.",
                    "session_id": session_id or "",
                })
                return

            # Fallback: scan full stdout for session id
            if not session_id:
                m = re.search(r"session (s9-[\w\d_-]+)", stdout_text)
                if m:
                    session_id = m.group(1)
                    _active_session_id = session_id

            if not session_id:
                self.send_error_response("Could not identify session ID from orchestrator output.")
                return

            # ── Extract the final answer from graph.json ──────────────────────
            graph_path = PROJECT_ROOT / "logs" / session_id / "graph.json"
            if not graph_path.exists():
                self.send_error_response(f"Session graph not found: {graph_path}")
                return

            graph_data = json.loads(graph_path.read_text(encoding="utf-8"))

            _active_proc = None
            result_text = self._extract_result(graph_data, stdout_text)
            self.send_json({"success": True, "result": result_text, "session_id": session_id})

        except Exception as e:
            _active_proc = None
            self.send_error_response(f"Server error: {e}")

    def _extract_result(self, graph_data: dict, stdout_fallback: str) -> str:
        """Pull the final answer out of the completed DAG.

        Preference order:
          1. formatter node → output.final_answer
          2. Any complete node's result.output.content (computer skill output)
          3. The FINAL: line printed to stdout by flow.py
        """
        formatter_node = None
        computer_node = None

        for node in graph_data.get("nodes", []):
            if node.get("status") != "complete":
                continue
            skill = node.get("skill", "")
            if skill == "formatter":
                # Always update — keeps the LAST complete formatter.
                # When recovery retries produce multiple formatters the last
                # one reflects the final outcome, not the intermediate failure.
                formatter_node = node
            elif skill == "computer" and computer_node is None:
                computer_node = node

        # 1. Formatter node final_answer
        if formatter_node:
            output = formatter_node.get("result", {}).get("output", {})
            answer = output.get("final_answer", "")
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            if isinstance(answer, dict):
                return json.dumps(answer, indent=2)

        # 2. Computer node content
        if computer_node:
            output = computer_node.get("result", {}).get("output", {})
            content = output.get("content", "")
            if content:
                return str(content).strip()

        # 3. FINAL: line from stdout
        m = re.search(r"FINAL:\s*(.+)", stdout_fallback, re.DOTALL)
        if m:
            return m.group(1).strip()

        return "Task completed. Check the DAG viewer for details."

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_response(self, message: str):
        self.send_json({"success": False, "error": message}, status=500)

    def log_message(self, fmt, *args):
        pass   # suppress per-request noise; we print our own logs


def run_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ComputerAgentHandler)

    def _shutdown(signum, frame):
        """Ctrl+C / SIGTERM handler: kill active subprocess tree then exit immediately."""
        print("\n[server] Shutting down (Ctrl+C)…")
        if _active_proc and _active_proc.poll() is None:
            print("[server] Killing active flow.py process group…")
            _kill_proc_tree(_active_proc)
        server.server_close()
        os._exit(0)   # force-exit all threads; subprocess is already dead

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f"[server] Computer Use Agent (Session 10) at http://localhost:{PORT}")
    print(f"[server] Project root: {PROJECT_ROOT}")
    print(f"[server] Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
