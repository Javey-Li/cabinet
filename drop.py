#!/usr/bin/env python3
"""Drop a URL, a note or an image into Cabinet's own library format."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import stat
import struct
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    from PIL import Image
except ImportError:
    Image = None


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data-v3.js"
ASSETS = ROOT / "assets"
THUMBS = ROOT / "thumbs"
DROP_LOCK = threading.Lock()
MAX_BODY = 16 * 1024 * 1024
KIND_COLORS = {
    "url": ["#f4efec", "#b0402f", "#8a8478", "#25211f"],
    "note": ["#f7f5f0", "#6f8a72", "#8a8478", "#25211f"],
    "image": ["#e3dfd6", "#b0402f", "#8a8478", "#25211f"],
}


class DropError(Exception):
    pass


def read_library() -> list[dict]:
    text = DATA.read_text(encoding="utf-8")
    marker = text.find("window.LIBRARY")
    if marker < 0:
        raise DropError("data-v3.js has no window.LIBRARY marker")
    start = text.find("[", marker)
    end = text.rfind("]")
    if start < 0 or end < start:
        raise DropError("data-v3.js has no library array")
    return json.loads(text[start:end + 1])


def write_library(entries: list[dict]) -> None:
    text = DATA.read_text(encoding="utf-8")
    marker = text.find("window.LIBRARY")
    if marker < 0:
        raise DropError("data-v3.js has no window.LIBRARY marker")
    start = text.find("[", marker)
    end = text.rfind("]")
    if start < 0 or end < start:
        raise DropError("data-v3.js has no library array")
    body = json.dumps(entries, ensure_ascii=False, indent=2)
    atomic_write(DATA, (text[:start] + body + text[end + 1:]).encode("utf-8"))


def next_id(entries: list[dict]) -> str:
    used = {str(e.get("id", "")) for e in entries}
    number = max([int(i) for i in used if i.isdigit()] or [0]) + 1
    while f"{number:04d}" in used:
        number += 1
    return f"{number:04d}"


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise DropError("URL is empty")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DropError("URL must start with http:// or https://")
    return value


def url_key(value: str) -> str:
    parsed = urlsplit(value)
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/",
                       urlencode(query), ""))


def title_for(kind: str, value: str) -> str:
    if kind == "url":
        value = value.replace("https://", "").replace("http://", "").strip("/")
    else:
        value = " ".join(value.split())
    value = value.replace("&", "＆").replace("<", "＜").replace(">", "＞")
    return value[:48] or "一条随手记"


def extension_for(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if payload.startswith(b"GIF8"):
        return ".gif"
    raise DropError("image must be PNG, JPEG, WebP or GIF")


def image_dimensions(payload: bytes) -> tuple[int, int]:
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
        return struct.unpack(">II", payload[16:24])
    if payload.startswith((b"GIF87a", b"GIF89a")) and len(payload) >= 10:
        return struct.unpack("<HH", payload[6:10])
    if payload.startswith(b"\xff\xd8\xff"):
        offset = 2
        while offset + 4 <= len(payload):
            if payload[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(payload) and payload[offset] == 0xFF:
                offset += 1
            if offset >= len(payload):
                break
            marker = payload[offset]
            offset += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(payload):
                break
            length = struct.unpack(">H", payload[offset:offset + 2])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height, width = struct.unpack(">HH", payload[offset + 3:offset + 7])
                return width, height
            offset += max(length, 2)
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP" and len(payload) >= 30:
        chunk = payload[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(payload[24:27], "little")
            height = 1 + int.from_bytes(payload[27:30], "little")
            return width, height
        if chunk == b"VP8L" and payload[20] == 0x2F:
            bits = int.from_bytes(payload[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and payload[23:26] == b"\x9d\x01\x2a":
            width = struct.unpack("<H", payload[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", payload[28:30])[0] & 0x3FFF
            return width, height
    raise DropError("image header is invalid or truncated")


def measure_image(payload: bytes) -> tuple[float, list[str]]:
    width, height = image_dimensions(payload)
    colors = KIND_COLORS["image"]
    if Image is not None:
        import io
        try:
            with Image.open(io.BytesIO(payload)) as image:
                quantized = image.convert("RGB").quantize(colors=6)
                palette = quantized.getpalette() or []
                counts = quantized.getcolors() or []
                colors = ["#%02x%02x%02x" % tuple(palette[color * 3:color * 3 + 3])
                          for count, color in sorted(counts, reverse=True)[:6]] or colors
        except Exception:
            pass
    return round(width / height, 3), colors


def text_lines(value: str, limit: int = 22) -> list[str]:
    return [value[index:index + limit] for index in range(0, min(len(value), limit * 3), limit)]


def placeholder(entry_id: str, label: str, title: str, source: str, created_at: str,
                colors: list[str], width: int, height: int) -> str:
    title_lines = text_lines(title)
    source_lines = text_lines(source, 30)
    title_svg = "".join(
        f'<text x="48" y="{156 + index * 38}" font-family="sans-serif" font-size="28" fill="#25211f">'
        f'{html.escape(line)}</text>' for index, line in enumerate(title_lines))
    source_svg = "".join(
        f'<text x="48" y="{292 + index * 30}" font-family="sans-serif" font-size="19" fill="#8a8478">'
        f'{html.escape(line)}</text>' for index, line in enumerate(source_lines))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 720 480">'
            f'<rect width="720" height="480" fill="{colors[0]}"/>'
            f'<rect x="24" y="24" width="672" height="432" fill="none" stroke="{colors[1]}" stroke-width="3"/>'
            f'<text x="48" y="84" font-family="sans-serif" font-size="22" fill="{colors[1]}">{label} · №{entry_id}</text>'
            f'{title_svg}{source_svg}'
            f'<text x="48" y="410" font-family="sans-serif" font-size="18" fill="{colors[1]}">'
            f'收于 {html.escape(created_at)} · 待 /ingest 拆解</text></svg>')


def svg_thumb(entry_id: str, label: str, title: str, source: str, created_at: str,
              colors: list[str]) -> Path:
    THUMBS.mkdir(exist_ok=True)
    path = THUMBS / f"{entry_id}.svg"
    path.write_text(placeholder(entry_id, label, title, source, created_at, colors, 720, 480),
                    encoding="utf-8")
    return path


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".drop-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def lock_file(path: Path):
    stream = path.open("a+b")
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_EX)
    elif msvcrt is not None:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    return stream


def unlock_file(stream) -> None:
    if fcntl is not None:
        fcntl.flock(stream, fcntl.LOCK_UN)
    elif msvcrt is not None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    stream.close()


def make_facet(entry_id: str, kind: str, value: str) -> dict:
    if kind == "url":
        return {
            "id": f"{entry_id}-source",
            "zh": "原始链接",
            "en": "source link",
            "at": [50, 50],
            "feels": ["科技"],
            "mech": "保留丢进来的原始链接和时间，之后由 /ingest 拆成可迁移的手法。",
            "say": "Open source.value from this entry before using it as a reference.",
            "q": value,
            "tune": "未拆手法前只当线索，不照搬内容。",
        }
    if kind == "image":
        return {
            "id": f"{entry_id}-draft",
            "zh": "待拆图",
            "en": "image to decode",
            "at": [50, 50],
            "feels": ["玩味"],
            "mech": "图片已经存进 Cabinet，等待 /ingest 按 INGEST.md 拆成手法。",
            "say": "Decode this plate with /ingest before using it as a design instruction.",
            "q": value,
            "tune": "未拆手法前只当参考图，不直接迁移整张画面。",
        }
    return {
        "id": f"{entry_id}-note",
        "zh": "一句话线索",
        "en": "one-line note",
        "at": [50, 50],
        "feels": ["安静"],
        "mech": "把一句话先收进灵感池，保住当时的想法；之后由 /ingest 决定怎么处理。",
        "say": "Treat source.value from this entry as a hint, not a finished instruction.",
        "q": value,
        "tune": "没拆成手法前不要当设计规则用。",
    }


def drop(kind: str, value: str, *, name: str = "", payload: bytes | None = None) -> dict:
    if kind == "url":
        value = normalize_url(value)
        fingerprint_source = url_key(value)
    elif kind in {"note", "image"}:
        value = value.strip()
        if not value:
            raise DropError(f"{kind} is empty")
        fingerprint_source = " ".join(value.split()) if kind == "note" else ""
    else:
        raise DropError("kind must be url, note or image")

    if kind == "image" and payload is None:
        raise DropError("image payload is missing")

    if kind == "image":
        fingerprint = hashlib.sha256(b"image\0" + payload).hexdigest()
    else:
        fingerprint = hashlib.sha256(f"{kind}\0{fingerprint_source}".encode("utf-8")).hexdigest()
    with DROP_LOCK:
        lock_path = ROOT / ".drop.lock"
        lock = lock_file(lock_path)
        try:
            return drop_locked(kind, value, name, payload, fingerprint)
        finally:
            unlock_file(lock)


def drop_locked(kind: str, value: str, name: str, payload: bytes | None, fingerprint: str) -> dict:
    entries = read_library()
    for entry in entries:
        if entry.get("source", {}).get("fingerprint") == fingerprint:
            return {"status": "duplicate", "id": entry["id"], "entry": entry}

    entry_id = next_id(entries)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    ASSETS.mkdir(exist_ok=True)

    if kind == "image":
        assert payload is not None
        suffix = extension_for(payload)
        asset_path = ASSETS / f"{entry_id}{suffix}"
        ar, colors = measure_image(payload)
        atomic_write(asset_path, payload)
        thumb_path = f"assets/{asset_path.name}"
    else:
        colors = KIND_COLORS[kind]
        ar = 1.5
        label = "LINK" if kind == "url" else "NOTE"
        title = title_for(kind, value)
        source_text = value.replace("https://", "").replace("http://", "")[:90]
        asset_path = ASSETS / f"{entry_id}.svg"
        atomic_write(asset_path, placeholder(entry_id, label, title, source_text, now, colors,
                                             720, 480).encode("utf-8"))
        svg_thumb(entry_id, label, title, source_text, now, colors)
        thumb_path = f"thumbs/{entry_id}.svg"

    source = {
        "kind": kind,
        "value": value,
        "name": name or None,
        "fingerprint": fingerprint,
        "createdAt": now,
    }
    entry = {
        "id": entry_id,
        "title": title_for(kind, value),
        "family": "Image" if kind == "image" else "Graphic",
        "asset": f"assets/{asset_path.name}",
        "thumb": thumb_path,
        "ar": ar,
        "colors": colors,
        "source": source,
        "createdAt": now,
        "facets": [make_facet(entry_id, kind, value)],
    }
    write_library(entries + [entry])
    return {"status": "created", "id": entry_id, "entry": entry}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/drop":
            self.send_json(404, {"error": "not found"})
            return
        token = os.environ.get("CABINET_DROP_TOKEN")
        if token and self.headers.get("Authorization") != f"Bearer {token}":
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            host = urlsplit("//" + self.headers.get("Host", "")).hostname
        except ValueError:
            host = None
        if host not in {"127.0.0.1", "localhost", "::1"}:
            self.send_json(403, {"error": "local host required"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                self.send_json(413, {"error": "request body must be between 1 byte and 16 MiB"})
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "JSON body must be an object"})
            return
        try:
            kind = payload.get("kind", "")
            if kind == "image":
                try:
                    image_bytes = base64.b64decode(payload.get("data", ""), validate=True)
                except (ValueError, TypeError) as error:
                    raise DropError(f"invalid base64: {error}")
                result = drop(kind, payload.get("value") or payload.get("name", "image"),
                              name=payload.get("name", ""), payload=image_bytes)
            elif kind in {"url", "note"}:
                result = drop(kind, payload.get("value", ""))
            else:
                raise DropError("kind must be url, note or image")
            self.send_json(200 if result["status"] == "created" else 409, result)
        except DropError as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", nargs="?", choices=["url", "note", "image", "serve"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--name", default="")
    parser.add_argument("--file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    if args.kind == "serve":
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"Cabinet drop listening on http://{args.host}:{args.port}/drop", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    if args.kind not in {"url", "note", "image"}:
        parser.error("kind must be url, note, image or serve")

    if args.kind == "image":
        if not args.file and args.value:
            args.file = args.value
            args.value = None
        if not args.file:
            parser.error("image drops require a path or --file")
        payload = Path(args.file).read_bytes()
        value = args.value or Path(args.file).name
        name = args.name or Path(args.file).name
        result = drop("image", value, name=name, payload=payload)
    else:
        if not args.value:
            parser.error(f"{args.kind} drops require a value")
        result = drop(args.kind, args.value)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
