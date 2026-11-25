import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import drop


class StaticServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "drop.py"
        self.script.write_text(Path(drop.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        files = {
            "index.html": "<html>ok</html>",
            "library-loader.js": "window.CABINET_DATA=[];",
            "data-v3.js": "window.LIBRARY=[];",
            "data-extra.js": "window.EXTRA=[];",
            "assets/ok.png": "asset",
            "assets/secret.txt": "secret",
            "thumbs/ok.jpg": "thumb",
            "folio/sutera.jpg": "folio",
            "shots/godly.mp4": "shots",
            "local/library.js": "window.LOCAL_LIBRARY=[];",
            "local/assets/ok.png": "local asset",
            "local/thumbs/ok.svg": "<svg/>",
            "local/state.json": '{"trashed":["private"]}',
            "local/private/secret.txt": "private",
            ".git/config": "git secret",
            "tests/test_secret.py": "test secret",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                CABINET_LOCAL_DIR=str(self.root / "local"))
        started = subprocess.run(
            [sys.executable, str(self.script), "start", "--port", str(self.port)],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
        )
        if started.returncode != 0:
            self.fail(started.stderr)
        self.addCleanup(self.stop)

    def stop(self) -> None:
        if self.root.exists():
            subprocess.run([sys.executable, str(self.script), "stop"], cwd=self.root,
                           env=self.environment, capture_output=True, text=True)

    def get(self, path: str, host: str | None = None):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Host": host or f"127.0.0.1:{self.port}"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers.get_content_type()
        except HTTPError as error:
            try:
                return error.code, error.read(), error.headers.get_content_type()
            finally:
                error.close()

    def test_static_files_are_limited_to_page_assets(self) -> None:
        for path in [
            "/.git/config", "/.cabinet.pid", "/drop.py", "/tests/test_secret.py",
            "/local/state.json", "/local/private/secret.txt", "/assets/secret.txt",
        ]:
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 404)
        for path in [
            "/", "/index.html", "/library-loader.js", "/data-v3.js", "/data-extra.js",
            "/assets/ok.png", "/thumbs/ok.jpg", "/folio/sutera.jpg", "/shots/godly.mp4",
            "/local/library.js", "/local/assets/ok.png", "/local/thumbs/ok.svg",
        ]:
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 200)

    def test_get_checks_host_and_localhost_gets_a_plain_message(self) -> None:
        status, body, _ = self.get("/", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        self.assertIn("请用 127.0.0.1 打开".encode(), body)
        status, _, _ = self.get("/local/library.js", host=f"localhost:{self.port}")
        self.assertEqual(status, 403)
        status, _, _ = self.get("/", host="evil.example")
        self.assertEqual(status, 403)

    def test_health_and_pid_file_share_a_random_token(self) -> None:
        status, body, _ = self.get("/api/health")
        self.assertEqual(status, 200)
        token = json.loads(body)["token"]
        self.assertGreaterEqual(len(token), 16)
        pid_data = json.loads((self.root / ".cabinet.pid").read_text(encoding="utf-8"))
        self.assertGreater(pid_data["pid"], 0)
        self.assertEqual(pid_data["token"], token)


if __name__ == "__main__":
    unittest.main()
