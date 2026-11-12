"""Loopback-only, read-only HTTP board and local outbox collector."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

from helm_store import Ledger, LedgerError, atomic_json, now

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "dashboard"


def make_server(ledger: Ledger, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body: bytes, mime: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._reply(b"Loopback same-origin access only", "text/plain", 403)
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path in {"/", "/dashboard.js", "/dashboard.css"}:
                    filename, mime = {"/": ("index.html", "text/html; charset=utf-8"),
                        "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                        "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8")}[parsed.path]
                    self._reply((ASSETS / filename).read_bytes(), mime)
                elif parsed.path == "/api/snapshot":
                    self._reply(json.dumps(ledger.snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
                elif parsed.path == "/evidence":
                    query = parse_qs(parsed.query, strict_parsing=True)
                    if set(query) != {"project", "workflow", "task", "kind"} or any(len(v) != 1 for v in query.values()):
                        raise LedgerError("evidence requires one project, workflow, task and kind")
                    path, checksum = ledger.artifact(*(query[k][0] for k in ("project", "workflow", "task", "kind")))
                    # Always plain text: reports cannot execute scripts in the board's origin.
                    body = (f"SHA-256: {checksum}\nFile: {path.name}\n\n" + path.read_text(encoding="utf-8-sig")).encode()
                    self._reply(body, "text/plain; charset=utf-8")
                elif parsed.path == "/events":
                    query = parse_qs(parsed.query)
                    after = max(0, int(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    while not stop.is_set():
                        latest = ledger.sequence()
                        if after > latest:
                            self.wfile.write(f"id: {latest}\nevent: reset\ndata: {{}}\n\n".encode())
                            after = latest
                        for event in ledger.changes(after):
                            after = int(event["seq"])
                            self.wfile.write((f"id: {after}\nevent: change\ndata: " + json.dumps({"sequence": after, "created_at": event["created_at"], "project": event["project"]}) + "\n\n").encode())
                        self.wfile.write(("event: heartbeat\ndata: " + json.dumps({"time": now(), "collector_error": health["error"]}) + "\n\n").encode())
                        self.wfile.flush()
                        stop.wait(1)
                else:
                    self._reply(b"Not found", "text/plain", 404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except (LedgerError, ValueError, OSError, sqlite3.Error) as exc:
                self._reply(json.dumps({"error": str(exc)}).encode(), "application/json", 400)

        def do_POST(self) -> None:
            self._reply(b"The board is read-only", "text/plain", 405)

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    stop = threading.Event()
    health = {"error": ""}

    def collect() -> None:
        while not stop.is_set():
            try:
                ledger.scan()
                health["error"] = ""
            except (LedgerError, ValueError, OSError, sqlite3.Error) as exc:
                health["error"] = str(exc)
            stop.wait(1)

    class BoardServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self) -> None:
            stop.set()
            super().server_close()
            collector.join(timeout=3)

    server = BoardServer(("127.0.0.1", port), Handler)
    collector = threading.Thread(target=collect, name="session-helm-collector", daemon=True)
    collector.start()
    return server


def serve(ledger: Ledger, port: int, ready_file: Path | None) -> None:
    ledger.snapshot()
    server = make_server(ledger, port)
    info = {"url": f"http://127.0.0.1:{server.server_port}/", "pid": os.getpid(), "database": str(ledger.path), "started_at": now()}
    if ready_file:
        atomic_json(ready_file, info)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
