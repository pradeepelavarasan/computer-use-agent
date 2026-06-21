"""
Shopping Agent Server — Session 9
Port: 8001  (S8 runs on 8000, both can be up simultaneously)

Key differences from Session 8 server.py:
  - PORT 8001 instead of 8000
  - Session ID regex: s9-* instead of s8-*
  - Graph path: logs/<sid>/graph.json  (S9 SessionStore layout)
  - Terminal data node: product_recommendation (not formatter)
    The formatter's final_answer is a text summary; the structured
    {products, analysis, task} JSON lives in product_recommendation output.
  - No shopping_system_prompt.txt injection — query decomposition is
    handled by the Planner's shopping section in planner.md.
"""

from __future__ import annotations
import json
import re
import sys
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

PORT = 8001
ROOT = Path(__file__).resolve().parent          # shopping_agent/
PROJECT_ROOT = ROOT.parent                       # EAG Session9 Browser Agent/

# Track the most recently started session ID so /api/graph-status can find it
_active_session_id: str | None = None


class ShoppingAgentHandler(SimpleHTTPRequestHandler):

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
        elif self.path.startswith("/api/image-proxy"):
            self.proxy_image()
        elif self.path == "/graph" or self.path.startswith("/graph?"):
            self.serve_file(ROOT / "graph_viewer.html", "text/html")
        elif self.path == "/api/graph-status" or self.path.startswith("/api/graph-status?"):
            self.handle_graph_status()
        else:
            self.send_error(404, "Not Found")

    def proxy_image(self):
        """Fetch a remote Amazon image URL and relay it server-side to bypass CORS/hotlink."""
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        url = params.get("url", [""])[0]
        if not url or not url.startswith("http"):
            self.send_error(400, "Missing or invalid url parameter")
            return
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ShoppingAgent/1.0)",
                    "Referer": "https://www.amazon.in/",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print(f"[server] image-proxy error for {url}: {e}")
            self.send_error(502, f"Could not fetch image: {e}")

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
        """Return the latest session's graph.json as JSON for live polling.

        Resolution order for session ID:
          1. Explicit ?session_id= query param (from graph viewer URL)
          2. The globally tracked _active_session_id (set when a search completes)
          3. Auto-discover: find the most recently modified s9-* directory under logs/
             This lets the graph viewer show live data even before the search response
             returns (flow.py creates the session dir within the first few seconds).
        """
        global _active_session_id
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        sid = params.get("session_id", [None])[0] or _active_session_id

        # Auto-discover the latest session from disk if nothing is tracked yet
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
            self.send_success_response({"session_id": None, "nodes": [], "edges": [], "query": "", "status": "idle"})
            return

        graph_path = PROJECT_ROOT / "logs" / sid / "graph.json"
        query_path = PROJECT_ROOT / "logs" / sid / "query.txt"
        query = query_path.read_text(encoding="utf-8").strip() if query_path.exists() else ""

        if not graph_path.exists():
            self.send_success_response({"session_id": sid, "nodes": [], "edges": [], "query": query, "status": "starting"})
            return

        try:
            graph_data = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.send_error_response(f"Failed to read graph: {e}")
            return

        # Check if pipeline is still running (any node still pending/running)
        all_nodes = graph_data.get("nodes", [])
        running = any(n.get("status") in ("pending", "running") for n in all_nodes)
        status = "running" if running else "complete"

        self.send_success_response({
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
        else:
            self.send_error(404, "Not Found")

    def handle_search(self):
        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)
        req = json.loads(post_data.decode("utf-8"))
        query = req.get("query", "").strip()

        if not query:
            self.send_error_response("Empty query received.")
            return

        try:
            # ── Locate the Python binary (prefer venv for speed) ───────────────
            python_bin = PROJECT_ROOT / ".venv" / "bin" / "python"
            if not python_bin.exists():
                python_bin = PROJECT_ROOT / "code" / ".venv" / "bin" / "python"
            if not python_bin.exists():
                python_bin = Path(sys.executable)

            # ── Invoke the S9 orchestrator with streaming stdout ──────────────
            # We use Popen + line-by-line reading so we can extract the session ID
            # from the very first log line (e.g. "session s9-2026-06-12_23-21-00")
            # and publish it to _active_session_id immediately — long before the
            # pipeline finishes.  The graph viewer polls /api/graph-status every 2s
            # and will pick up the live graph.json within a couple of seconds.
            print(f"[server] Invoking S9 DAG executor for query: {query}")
            proc = subprocess.Popen(
                [str(python_bin), "code/flow.py", query],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            stdout_lines = []
            session_id = None

            # Stream stdout line-by-line; set _active_session_id as soon as we see it
            global _active_session_id
            for line in proc.stdout:
                stdout_lines.append(line)
                print(line, end="")
                if session_id is None:
                    m = re.search(r"session (s9-[\w\d_-]+)", line)
                    if m:
                        session_id = m.group(1)
                        _active_session_id = session_id
                        print(f"[server] Session ID detected early: {session_id}")

            proc.wait()
            stdout_text = "".join(stdout_lines)

            stderr_text = proc.stderr.read()
            if stderr_text:
                print(stderr_text, file=sys.stderr)

            if not session_id:
                # Fallback: scan entire stdout (should not normally be needed)
                m = re.search(r"session (s9-[\w\d_-]+)", stdout_text)
                if m:
                    session_id = m.group(1)
                    _active_session_id = session_id

            if not session_id:
                self.send_error_response(
                    "Could not identify the orchestrator session ID from output."
                )
                return

            print(f"[server] Identified session ID: {session_id}")

            # ── Read the session graph ─────────────────────────────────────────
            # S9 SessionStore writes to: <project-root>/logs/<sid>/graph.json
            graph_path = PROJECT_ROOT / "logs" / session_id / "graph.json"
            if not graph_path.exists():
                self.send_error_response(
                    f"Session graph file not found at {graph_path}"
                )
                return

            with open(graph_path, "r", encoding="utf-8") as f:
                graph_data = json.load(f)

            # ── Locate the terminal data node ─────────────────────────────────
            # Primary: product_recommendation — contains {products, analysis, task}
            # Fallback: formatter — contains {final_answer} which may be JSON string
            recommendation_node = None
            formatter_node = None

            for node in graph_data.get("nodes", []):
                if node.get("status") != "complete":
                    continue
                skill = node.get("skill", "")
                if skill == "product_recommendation" and recommendation_node is None:
                    recommendation_node = node
                elif skill == "formatter" and formatter_node is None:
                    formatter_node = node

            # Case 1: product_recommendation output has the structured JSON
            if recommendation_node:
                rec_output = recommendation_node.get("result", {}).get("output", {})
                if "products" in rec_output and "analysis" in rec_output:
                    rec_output["session_id"] = session_id
                    self.send_success_response(rec_output)
                    return

            # Case 2: formatter output — may be direct dict or JSON string
            if formatter_node:
                fmt_output = formatter_node.get("result", {}).get("output", {})
                final_answer = fmt_output.get("final_answer", "")

                # Case 2a: formatter wrote products+analysis directly
                if not final_answer and "products" in fmt_output:
                    fmt_output["session_id"] = session_id
                    self.send_success_response(fmt_output)
                    return

                if isinstance(final_answer, dict) and "products" in final_answer:
                    final_answer["session_id"] = session_id
                    self.send_success_response(final_answer)
                    return

                if isinstance(final_answer, str) and final_answer.strip():
                    parsed = self._parse_json_string(final_answer)
                    if parsed and "products" in parsed:
                        parsed["session_id"] = session_id
                        self.send_success_response(parsed)
                        return
                    # Plain text response — wrap for frontend
                    if final_answer:
                        self.send_error_response(
                            f"Agent returned a text answer (not product JSON): {final_answer[:200]}"
                        )
                        return

            self.send_error_response(
                "The DAG did not produce a product_recommendation or formatter output."
            )

        except Exception as e:
            self.send_error_response(f"Server error: {e}")

    # ── Helpers ─────────────────────────────────────────────────────────────────

    def _parse_json_string(self, text: str):
        """Strip markdown fences and parse a JSON string. Returns None on failure."""
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`").strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                cleaned = cleaned[start:end + 1]
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return None

    def send_success_response(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_error_response(self, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Suppress the default per-request noise; we print our own logs
        pass


def run_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ShoppingAgentHandler)
    print(f"[server] Shopping Agent (Session 9) running at http://localhost:{PORT} (threaded)")
    print(f"[server] Project root: {PROJECT_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down.")
        server.server_close()


if __name__ == "__main__":
    run_server()
