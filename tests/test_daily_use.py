import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import drop


def read_json(response) -> dict:
    return json.loads(response.read().decode("utf-8"))


class DailyUseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data-v3.js").write_text("window.LIBRARY = [];\n", encoding="utf-8")
        (self.root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        self.patch = mock.patch.multiple(
            drop,
            ROOT=self.root,
            PUBLIC_DATA=self.root / "data-v3.js",
            LOCAL_DIR=self.root / "local",
            LOCAL_DATA=self.root / "local" / "library.js",
            STATE_DATA=self.root / "local" / "state.json",
            ASSETS=self.root / "local" / "assets",
            THUMBS=self.root / "local" / "thumbs",
            PID_PATH=self.root / ".cabinet.pid",
        )
        self.patch.start()
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.patch.stop()
        self.temporary.cleanup()

    def start(self) -> int:
        class Handler(drop.Handler):
            pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def post(self, port: int, payload: dict, host: str = "127.0.0.1") -> tuple[int, dict]:
        request = Request(f"http://127.0.0.1:{port}/drop", data=json.dumps(payload).encode(),
                          headers={"Host": host, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, read_json(response)
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode())
            finally:
                error.close()

    def test_likes_drop_contract_uses_localhost_host_and_url_payload(self) -> None:
        port = self.start()
        status, result = self.post(port, {
            "kind": "url",
            "value": "https://x.com/user/status/123",
        }, host="localhost")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "created")

        status, duplicate = self.post(port, {
            "kind": "url",
            "value": "https://x.com/user/status/123",
        }, host="127.0.0.1")
        self.assertEqual(status, 409)
        self.assertEqual(duplicate["status"], "duplicate")

    def test_same_url_appends_note_without_overwriting(self) -> None:
        port = self.start()
        url = "https://example.com/same"
        self.post(port, {"kind": "url", "value": url})
        status, noted = self.post(port, {"kind": "url", "value": url, "note": "first note"})
        self.assertEqual(status, 200)
        self.assertEqual(noted["status"], "noted")
        self.post(port, {"kind": "url", "value": url, "note": "second note"})
        entry = next(item for item in drop.local_entries() if item["id"] == noted["id"])
        self.assertEqual([item["body"] for item in entry["notes"]], ["first note", "second note"])
        self.assertEqual(entry["source"]["value"], url)

    def test_pending_sort_and_state_actions(self) -> None:
        drop.drop("note", "older")
        drop.drop("note", "newer")
        pending = drop.pending_entries()
        self.assertEqual([item["source"]["value"] for item in pending], ["older", "newer"])
        drop.update_state("later", pending[0]["id"])
        drop.update_state("trash", pending[1]["id"])
        self.assertEqual(drop.pending_entries(), [])
        state = drop.read_state()
        self.assertEqual(state["later"], [pending[0]["id"]])
        self.assertEqual(state["trashed"], [pending[1]["id"]])
        drop.restore_entry(pending[0]["id"])
        self.assertEqual([item["id"] for item in drop.pending_entries()], [pending[0]["id"]])

    def test_http_state_actions_and_restore(self) -> None:
        port = self.start()
        entry = drop.drop("note", "http state")["entry"]
        status, _ = self.post(port, {"kind": "state", "action": "trash", "id": entry["id"]})
        self.assertEqual(status, 200)
        with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as response:
            self.assertEqual(read_json(response)["trashed"], [entry["id"]])
        status, restored = self.post(port, {"kind": "state", "action": "restore", "id": entry["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(drop.pending_entries()[0]["id"], entry["id"])

    def test_start_stop_use_one_loopback_endpoint(self) -> None:
        script = self.root / "drop.py"
        script.write_text(Path(drop.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                           CABINET_LOCAL_DIR=str(self.root / "local"))
        def cleanup():
            if self.root.exists():
                subprocess.run([sys.executable, str(script), "stop"], cwd=self.root,
                               env=environment, capture_output=True, text=True)
        self.addCleanup(cleanup)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        start = subprocess.run([sys.executable, str(script), "start", "--port", str(port)],
                               cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertIn("started", start.stdout)
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
        with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            self.assertIn(b"<html>ok</html>", response.read())
        second = subprocess.run([sys.executable, str(script), "start", "--port", str(port)],
                                cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0)
        self.assertIn("already", second.stdout)
        stop = subprocess.run([sys.executable, str(script), "stop"],
                              cwd=self.root, env=environment, capture_output=True, text=True)
        self.assertEqual(stop.returncode, 0)
        self.assertIn("stopped", stop.stdout)

    def test_start_rejects_non_loopback_host(self) -> None:
        with self.assertRaisesRegex(drop.DropError, "127.0.0.1"):
            drop.start("0.0.0.0", 8788)

    def test_start_reports_the_process_using_an_occupied_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        self.addCleanup(probe.close)
        completed = subprocess.CompletedProcess([], 0, stdout=f"{os.getpid()}\n", stderr="")
        with mock.patch.object(drop.shutil, "which", return_value="/usr/sbin/lsof"), \
                mock.patch.object(drop.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(drop.DropError, f"pid {os.getpid()}"):
                drop.start("127.0.0.1", probe.getsockname()[1])

    def test_restore_rejects_entries_outside_the_local_library(self) -> None:
        with self.assertRaisesRegex(drop.DropError, "entry not found"):
            drop.restore_entry("9999")


if __name__ == "__main__":
    unittest.main()
