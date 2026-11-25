import base64
import concurrent.futures
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import drop


SEED = {
    "id": "0041",
    "title": "seed",
    "family": "Screen",
    "asset": "assets/seed.jpg",
    "thumb": "thumbs/seed.jpg",
    "ar": 1.0,
    "colors": ["#ffffff"],
    "facets": [],
}


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    rows = b"".join(b"\x00" + b"\xb0\x40\x2f" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


class DropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data-v3.js"
        self.data.write_text("window.LIBRARY = " + json.dumps([SEED]) + ";\n", encoding="utf-8")
        self.public_bytes = self.data.read_bytes()
        self.paths = mock.patch.multiple(
            drop,
            ROOT=self.root,
            PUBLIC_DATA=self.data,
            LOCAL_DIR=self.root / "local",
            LOCAL_DATA=self.root / "local" / "library.js",
            ASSETS=self.root / "local" / "assets",
            THUMBS=self.root / "local" / "thumbs",
        )
        self.paths.start()
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.paths.stop()
        self.temporary.cleanup()

    def library(self) -> list[dict]:
        return drop.all_entries()

    def start_server(self) -> int:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), drop.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def request(self, port: int, payload: dict, token: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(f"http://127.0.0.1:{port}/drop",
                          data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_url_keeps_source_time_and_deduplicates_tracking_params(self) -> None:
        result = drop.drop("url", "https://example.com/ref?utm_source=x#part")
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["id"], "0042")
        entry = result["entry"]
        self.assertEqual(entry["source"]["kind"], "url")
        self.assertEqual(entry["source"]["value"], "https://example.com/ref?utm_source=x#part")
        datetime.fromisoformat(entry["createdAt"])
        self.assertTrue((self.root / entry["asset"]).exists())
        self.assertTrue((self.root / entry["thumb"]).exists())

        duplicate = drop.drop("url", "https://example.com/ref")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["id"], "0042")
        self.assertEqual(len(self.library()), 2)
        self.assertEqual(self.data.read_bytes(), self.public_bytes)
        self.assertEqual(entry["asset"], "local/assets/0042.svg")
        self.assertEqual(entry["thumb"], "local/thumbs/0042.svg")

    def test_note_is_escaped_and_deduplicated_after_whitespace_cleanup(self) -> None:
        value = "  <img src=x onerror=alert(1)> & 留白  "
        result = drop.drop("note", value)
        entry = result["entry"]
        self.assertNotIn("<img", entry["title"])
        self.assertEqual(entry["source"]["value"], value.strip())
        svg = (self.root / entry["asset"]).read_text(encoding="utf-8")
        self.assertIn("&lt;img", svg)
        self.assertNotIn("<img", svg)
        self.assertEqual(drop.drop("note", " <img src=x onerror=alert(1)>   &   留白 ")["status"],
                         "duplicate")

    def test_image_uses_real_dimensions_and_content_fingerprint(self) -> None:
        payload = png(4, 2)
        result = drop.drop("image", "fake plate", name="misleading.jpg", payload=payload)
        entry = result["entry"]
        self.assertEqual(entry["ar"], 2.0)
        self.assertTrue(entry["asset"].endswith(".png"))
        self.assertEqual((self.root / entry["asset"]).read_bytes(), payload)
        duplicate = drop.drop("image", "another title", name="copy.png", payload=payload)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["id"], "0042")

    def test_invalid_image_does_not_create_an_entry(self) -> None:
        with self.assertRaises(drop.DropError):
            drop.drop("image", "fake", name="fake.png", payload=b"not an image")
        self.assertEqual(len(self.library()), 1)
        self.assertFalse((self.root / "assets" / "0042.png").exists())

    def test_missing_library_marker_is_rejected(self) -> None:
        self.data.write_text("const other = [];\n", encoding="utf-8")
        with self.assertRaises(drop.DropError):
            drop.drop("note", "blocked")

    def test_drop_keeps_git_status_clean_and_public_library_unchanged(self) -> None:
        (self.root / ".gitignore").write_text("local/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "data-v3.js", ".gitignore"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=self.root, check=True)

        result = drop.drop("note", "private note")
        self.assertEqual(result["status"], "created")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "")
        self.assertEqual(self.data.read_bytes(), self.public_bytes)

    def test_deduplicates_against_public_and_local_libraries(self) -> None:
        note = "same note"
        fingerprint = hashlib.sha256(f"note\0{note}".encode("utf-8")).hexdigest()
        public_entry = dict(SEED, id="0042", source={"fingerprint": fingerprint})
        self.data.write_text("window.LIBRARY = " + json.dumps([SEED, public_entry]) + ";\n",
                             encoding="utf-8")
        self.assertEqual(drop.drop("note", note)["status"], "duplicate")

        local_entry = dict(SEED, id="0043", source={"fingerprint": fingerprint})
        drop.LOCAL_DATA.parent.mkdir(parents=True, exist_ok=True)
        drop.LOCAL_DATA.write_text("window.LOCAL_LIBRARY = " + json.dumps([local_entry]) + ";\n",
                                   encoding="utf-8")
        self.assertEqual(drop.drop("note", note)["status"], "duplicate")

    def test_page_loader_without_local_library_uses_public_data(self) -> None:
        loader = Path(drop.__file__).with_name("library-loader.js").read_text(encoding="utf-8")
        script = ("global.window={LIBRARY:" + json.dumps([SEED]) + "};" + loader
                  + ";process.stdout.write(JSON.stringify(window.CABINET_DATA));")
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [SEED])

    def test_page_loader_merges_local_data_and_deduplicates(self) -> None:
        loader = Path(drop.__file__).with_name("library-loader.js").read_text(encoding="utf-8")
        local_entry = dict(SEED, id="0042", title="local")
        duplicate_entry = dict(SEED, id="0041", title="duplicate", source={"fingerprint": "same"})
        duplicate_fingerprint = dict(SEED, id="0044", source={"fingerprint": "same"})
        public_entry = dict(SEED, id="0043", source={"fingerprint": "same"})
        script = ("global.window={LIBRARY:" + json.dumps([SEED, public_entry])
                  + ",LOCAL_LIBRARY:" + json.dumps([local_entry, duplicate_entry, duplicate_fingerprint]) + "};" + loader
                  + ";process.stdout.write(JSON.stringify(window.CABINET_DATA));")
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual([entry["id"] for entry in json.loads(result.stdout)], ["0041", "0043", "0042"])

    def test_concurrent_drops_keep_unique_ids(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda index: drop.drop("note", f"note {index}"), range(8)))
        ids = [result["id"] for result in results]
        self.assertEqual(len(set(ids)), 8)
        self.assertEqual(len(self.library()), 9)

    def test_http_auth_create_duplicate_and_bad_payload(self) -> None:
        port = self.start_server()
        with mock.patch.dict(os.environ, {"CABINET_DROP_TOKEN": "test-token"}):
            status, _ = self.request(port, {"kind": "note", "value": "http fake"})
            self.assertEqual(status, 401)
            status, created = self.request(port, {"kind": "note", "value": "http fake"},
                                           token="test-token")
            self.assertEqual(status, 200)
            self.assertEqual(created["status"], "created")
            status, duplicate = self.request(port, {"kind": "note", "value": "http fake"},
                                             token="test-token")
            self.assertEqual(status, 409)
            self.assertEqual(duplicate["status"], "duplicate")
            status, _ = self.request(port, ["not", "an", "object"], token="test-token")
            self.assertEqual(status, 400)

    def test_http_accepts_base64_image(self) -> None:
        port = self.start_server()
        status, result = self.request(port, {
            "kind": "image",
            "name": "fake.png",
            "data": base64.b64encode(png(3, 1)).decode("ascii"),
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["entry"]["ar"], 3.0)

    def test_http_rejects_cross_site_and_non_json_requests(self) -> None:
        port = self.start_server()
        payload = json.dumps({"kind": "note", "value": "blocked"}).encode("utf-8")
        for headers, expected in [
            ({"Host": "localhost", "Content-Type": "application/json"}, 403),
            ({"Host": "127.0.0.1", "Origin": "https://evil.example",
              "Content-Type": "application/json"}, 403),
            ({"Host": "127.0.0.1", "Sec-Fetch-Site": "cross-site",
              "Content-Type": "application/json"}, 403),
            ({"Host": f"127.0.0.1:{port}", "Content-Type": "text/plain"}, 415),
        ]:
            request = Request(f"http://127.0.0.1:{port}/drop", data=payload, headers=headers)
            try:
                with urlopen(request, timeout=5) as response:
                    status = response.status
            except HTTPError as error:
                try:
                    status = error.code
                finally:
                    error.close()
            self.assertEqual(status, expected)
        self.assertEqual(len(self.library()), 1)

    def test_cli_note_and_image_path(self) -> None:
        script = self.root / "drop.py"
        script.write_text(Path(drop.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        image = self.root / "fake.png"
        image.write_bytes(png(2, 1))
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        note = subprocess.run([sys.executable, str(script), "note", "cli fake"], cwd=self.root,
                              env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(note.stdout)["status"], "created")
        picture = subprocess.run([sys.executable, str(script), "image", str(image)], cwd=self.root,
                                 env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(picture.stdout)["entry"]["ar"], 2.0)

    def test_cli_custom_local_directory_inside_ignored_directory(self) -> None:
        script = self.root / "drop.py"
        script.write_text(Path(drop.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CABINET_LOCAL_DIR="local/private")
        result = subprocess.run([sys.executable, str(script), "note", "custom local"], cwd=self.root,
                                env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "created")
        self.assertTrue((self.root / "local" / "private" / "library.js").exists())
        self.assertTrue((self.root / "local" / "library.js").exists())
        self.assertEqual(self.data.read_bytes(), self.public_bytes)

        default_environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        default_environment.pop("CABINET_LOCAL_DIR", None)
        default_result = subprocess.run([sys.executable, str(script), "note", "back to default"],
                                        cwd=self.root, env=default_environment,
                                        capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(default_result.stdout)["status"], "created")
        self.assertNotIn("CABINET_LOCAL_PATH", (self.root / "local" / "library.js").read_text())


if __name__ == "__main__":
    unittest.main()
