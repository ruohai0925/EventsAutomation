"""Local review page: tick events, then dry-run / register / sync from the browser.

Binds to 127.0.0.1 only. Choices are written straight back to <ws>/picks.yaml, so the
CLI and the page always agree.
"""
import json
import subprocess
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

HTML = Path(__file__).with_name("web") / "index.html"
HEADER = "# Set register: true/false (or use `python -m eventsauto serve`), then run `register`\n"


class Runner:
    """At most one background CLI job (sync / dry run / register) at a time."""

    def __init__(self, ws):
        self.ws, self.proc, self.mode, self.log = ws, None, None, deque(maxlen=400)
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode):
        args = {"sync": ["sync"], "dry": ["register", "--dry-run"], "real": ["register"],
                "rank": ["rank"], "calendar": ["calendar"]}[mode]
        with self.lock:
            if self.running():
                return False
            self.log.clear()
            self.mode = mode
            from datetime import datetime
            names = {"sync": "sync", "dry": "dry run", "real": "register", "rank": "rank", "calendar": "calendar"}
            self.log.append(f"▶ {names.get(mode, mode)} — started {datetime.now():%H:%M:%S}")
            self.proc = subprocess.Popen([sys.executable, "-u", "-m", "eventsauto", *args, "--ws", str(self.ws.root)],
                                         cwd=Path(__file__).parent.parent, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self):
        for line in self.proc.stdout:
            self.log.append(line.rstrip())
        self.proc.wait()
        self.log.append(f"— finished ({'ok' if self.proc.returncode == 0 else 'exit ' + str(self.proc.returncode)}) —")


def serve(ws, port=8765):
    picks_path = ws.root / "picks.yaml"
    runner = Runner(ws)
    lock = threading.Lock()

    def load_picks():
        return yaml.safe_load(picks_path.read_text(encoding="utf-8")) or []

    def state():
        profile = ws.profile
        return {
            "picks": load_picks(),
            "mine": ws.load("mine.json", {}),
            "mine_tiers": ws.load("mine_tiers.json", {}),
            "registrations": ws.load("registrations.json", {}),
            "per_day": profile.get("per_day", 4),
            "timezone": profile.get("timezone", "America/Los_Angeles"),
            "run": {"running": runner.running(), "mode": runner.mode, "log": list(runner.log)[-60:]},
        }

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, HTML.read_bytes(), "text/html")
            if self.path == "/api/state":
                return self._send(200, state())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/pick":
                with lock:
                    picks = load_picks()
                    for p in picks:
                        if p["id"] in set(body["ids"]):
                            p["register"] = bool(body["register"])
                            p["user_set"] = True
                    picks_path.write_text(HEADER + yaml.safe_dump(picks, allow_unicode=True, sort_keys=False, width=200),
                                          encoding="utf-8")
                return self._send(200, {"ok": True})
            if self.path == "/api/run":
                ok = runner.start(body["mode"])
                return self._send(200 if ok else 409, {"ok": ok})
            self._send(404, {"error": "not found"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Open http://localhost:{port}  (Ctrl+C to stop)", flush=True)
    srv.serve_forever()
