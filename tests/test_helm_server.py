from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from helm_server import make_server
from helm_store import Ledger


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "ledger.sqlite")
        self.ledger.initialize("supervisor")
        self.server = make_server(self.ledger)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_readonly_loopback_board_and_assets(self) -> None:
        status, body = self.request("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["supervisor"], "supervisor")
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/dashboard.js")[0], 200)
        self.assertEqual(self.request("/api/snapshot", "POST")[0], 405)
        self.assertEqual(self.request("/api/snapshot", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/../../ledger.sqlite")[0], 404)
        self.assertEqual(self.request("/evidence?path=../../ledger.sqlite")[0], 400)
        self.assertEqual(self.request("/evidence?project=x&workflow=w&task=t&kind=report&project=y")[0], 400)

    def test_sse_replays_after_cursor_and_ledger_survives_server_restart(self) -> None:
        before = self.ledger.sequence()
        self.ledger.handoff("successor", "supervisor", 1, "test handoff")
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request("GET", "/events", headers={"Last-Event-ID": str(before)})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline().decode().strip(), f"id: {before + 1}")
            self.assertEqual(response.readline().decode().strip(), "event: change")
        finally:
            conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.server = make_server(Ledger(self.ledger.path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(json.loads(self.request("/api/snapshot")[1])["supervisor"], "successor")


if __name__ == "__main__":
    unittest.main()
