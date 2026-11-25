import html
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHROME = next((path for path in [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome"),
    shutil.which("chromium"),
] if path and Path(path).exists()), None)


def malicious_entry() -> dict:
    return {
        "id": "0042",
        "title": '"><img src=x onerror=window.__x=1>',
        "family": "Graphic",
        "asset": "javascript:window.__asset=1",
        "thumb": "https://evil.example/track.png",
        "ar": 1.5,
        "colors": ["#f7f5f0"],
        "facets": [{
            "id": "0042-note",
            "zh": '<img src=x onerror=window.__zh=1>',
            "en": '<img src=x onerror=window.__en=1>',
            "at": [50, 50],
            "feels": ["安静"],
            "mech": '<img src=x onerror=window.__mech=1>',
            "say": '<img src=x onerror=window.__say=1>',
            "q": "safe query",
            "tune": '<img src=x onerror=window.__tune=1>',
        }],
    }


def pending_entry() -> dict:
    return {
        "id": "0099",
        "title": "pending note",
        "family": "Graphic",
        "asset": "local/assets/0099.svg",
        "thumb": "local/thumbs/0099.svg",
        "ar": 1.5,
        "colors": ["#f7f5f0"],
        "createdAt": "2026-11-20T10:00:00+09:00",
        "source": {
            "kind": "note",
            "value": "screen pending note",
            "name": "",
            "fingerprint": "screen-pending-note",
            "createdAt": "2026-11-20T10:00:00+09:00",
        },
        "notes": [{"body": "note-needle", "createdAt": "2026-11-20T10:01:00+09:00"}],
        "facets": [],
    }


def run_page(script: str, entry: dict | None = None, remote_state: dict | None = None,
             width: int = 800) -> dict:
    if CHROME is None:
        raise unittest.SkipTest("Chrome is not installed")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name in ["index.html", "data-v3.js", "library-loader.js"]:
            shutil.copy2(ROOT / name, root / name)
        if entry is not None:
            (root / "local").mkdir()
            (root / "local" / "library.js").write_text(
                "window.LOCAL_LIBRARY = " + json.dumps([entry]) + ";\n", encoding="utf-8")
        page = (root / "index.html").read_text(encoding="utf-8")
        harness = (
            "<script>(async()=>{try{const frame=()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));"
            "const waitFor=async check=>{for(let index=0;index<60;index++){if(check())return;"
            "await new Promise(resolve=>setTimeout(resolve,20));}throw new Error('condition not met');};"
            + script
            + "}catch(error){const pre=document.createElement('pre');pre.id='test-result';"
              "pre.textContent=JSON.stringify({error:String(error)});document.body.appendChild(pre);}})();</script>"
        )
        (root / "index.html").write_text(page.replace("</body>", harness + "</body>"), encoding="utf-8")
        profile = root / "profile"
        server = None
        thread = None
        page_url = (root / "index.html").as_uri()
        if remote_state is not None:
            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_GET(self):
                    if self.path == "/api/state":
                        body = json.dumps(remote_state).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    super().do_GET()

                def do_POST(self):
                    if self.path != "/drop":
                        self.send_error(404)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length))
                    entry_id = payload.get("id")
                    action = payload.get("action")
                    for bucket in ["trashed", "later"]:
                        remote_state[bucket] = [item for item in remote_state.get(bucket, [])
                                                if item != entry_id]
                    if action in {"trash", "later"}:
                        remote_state["trashed" if action == "trash" else "later"].append(entry_id)
                    body = json.dumps({"status": action}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         lambda *args, **kwargs: Handler(*args, directory=str(root), **kwargs))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            page_url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
        result = subprocess.run([
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-translate",
            "--no-proxy-server",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={width},900",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=5000",
            "--dump-dom",
            page_url,
        ], capture_output=True, text=True, timeout=30)
        if server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if result.returncode != 0:
            raise AssertionError(result.stderr[-2000:])
        match = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, flags=re.S)
        if not match:
            raise AssertionError("test result missing from Chrome DOM\n" + result.stdout[-2000:])
        payload = json.loads(html.unescape(match.group(1)))
        if remote_state is not None:
            payload["remoteState"] = remote_state
        return payload


class PageSafetyTests(unittest.TestCase):
    def test_malicious_local_fields_do_not_execute_or_load_external_assets(self) -> None:
        result = run_page(
            "await window.CABINET_READY;await frame();"
            "window.openDrawer(window.CABINET_DATA.length-1);await frame();"
            "document.querySelector('.act.pick').click();document.querySelector('#pick-btn').click();await frame();"
            "document.querySelector('#pick-btn').click();document.querySelector('#plate-trash').click();"
            "document.querySelector('#trash-btn').click();await frame();"
            "const sources=[...document.querySelectorAll('#plate img,#pick-chips img,.trash-card img')]"
            ".map(image=>image.getAttribute('src')).filter(Boolean);"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({x:window.__x===1,zh:window.__zh===1,en:window.__en===1,"
            "mech:window.__mech===1,say:window.__say===1,tune:window.__tune===1,"
            "asset:window.__asset===1,sources});document.body.appendChild(pre);",
            malicious_entry(),
        )
        self.assertFalse(result.get("error"), result)
        for key in ["x", "zh", "en", "mech", "say", "tune", "asset"]:
            self.assertFalse(result[key], result)
        self.assertEqual(result["sources"], [], result)

    def test_theme_switch_changes_canvas_background_and_restores_it(self) -> None:
        result = run_page(
            "await window.CABINET_READY;await frame();"
            "const canvas=document.querySelector('#cv');const context=canvas.getContext('2d');"
            "const pixel=()=>Array.from(context.getImageData(3,3,1,1).data);"
            "const before=pixel();document.querySelector('#theme-toggle').click();await frame();"
            "const dark=pixel();document.querySelector('#theme-toggle').click();await frame();"
            "const restored=pixel();const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({before,dark,restored,changed:before.join()!==dark.join(),"
            "restored:before.join()===restored.join()});document.body.appendChild(pre);"
        )
        self.assertFalse(result.get("error"), result)
        self.assertTrue(result["changed"], result)
        self.assertTrue(result["restored"], result)
        self.assertEqual(result["before"][:3], [247, 245, 240], result)
        self.assertEqual(result["dark"][:3], [23, 21, 19], result)

    def test_dark_canvas_word_labels_use_the_solid_word_color(self) -> None:
        result = run_page(
            "await window.CABINET_READY;await frame();"
            "const context=document.querySelector('#cv').getContext('2d');"
            "const original=context.fillText.bind(context);const samples=[];"
            "context.fillText=function(text,x,y){if(this.font.includes('18px'))"
            "samples.push({color:this.fillStyle,alpha:this.globalAlpha});"
            "return original(text,x,y);};"
            "document.querySelector('#theme-toggle').click();"
            "for(let index=0;index<5;index++)await frame();"
            "const word=getComputedStyle(document.documentElement).getPropertyValue('--word').trim();"
            "const sub=getComputedStyle(document.documentElement).getPropertyValue('--sub').trim();"
            "const probe=document.createElement('canvas').getContext('2d');probe.fillStyle=word;"
            "const expected=probe.fillStyle;const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({word,sub,expected,samples,"
            "solid:samples.some(sample=>sample.color===expected&&sample.alpha===1)});"
            "document.body.appendChild(pre);"
        )
        self.assertFalse(result.get("error"), result)
        self.assertNotEqual(result["word"], result["sub"], result)
        self.assertTrue(result["solid"], result)

    def test_narrow_pending_entry_trashes_and_restores(self) -> None:
        remote_state = {"trashed": [], "later": []}
        result = run_page(
            "await window.CABINET_READY;await frame();await window.CABINET_RENDER_PENDING();"
            "window.__summon('note-needle');await frame();"
            "const searchCount=document.querySelector('#qn').textContent;"
            "document.querySelector('#qx').click();"
            "const tab=document.querySelector('#pending-tab');"
            "const visible=getComputedStyle(tab).display!=='none'&&tab.getBoundingClientRect().width>0;"
            "tab.click();await frame();await window.CABINET_RENDER_PENDING();"
            "const pending=document.body.classList.contains('mode-pending');"
            "const trash=[...document.querySelectorAll('#pending-list button')]"
            ".find(button=>button.textContent==='不要了');trash.click();"
            "await waitFor(()=>document.querySelector('#trashed-list .pending-row'));"
            "const trashed=document.querySelector('#trashed-list h3').textContent;"
            "document.querySelector('#trashed-list button').click();"
            "await waitFor(()=>document.querySelector('#pending-list .pending-row'));"
            "const restored=document.querySelector('#pending-list h3').textContent;"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({width:innerWidth,visible,pending,searchCount,trashed,restored,"
            "tabText:tab.textContent});document.body.appendChild(pre);",
            pending_entry(), remote_state, width=390,
        )
        self.assertFalse(result.get("error"), result)
        self.assertGreater(result["width"], 0, result)
        self.assertLessEqual(result["width"], 820, result)
        self.assertTrue(result["visible"], result)
        self.assertTrue(result["pending"], result)
        self.assertEqual(result["searchCount"], "1 张对味", result)
        self.assertEqual(result["trashed"], "screen pending note", result)
        self.assertEqual(result["restored"], "screen pending note", result)
        self.assertEqual(result["tabText"], "画布", result)
        self.assertEqual(result["remoteState"], {"trashed": [], "later": []}, result)

    def test_pending_trash_appears_in_the_main_recycle_bin(self) -> None:
        remote_state = {"trashed": [], "later": []}
        result = run_page(
            "await window.CABINET_READY;await frame();await window.CABINET_RENDER_PENDING();"
            "document.querySelector('.lobe[data-mode=\"pending\"]').click();"
            "await frame();await window.CABINET_RENDER_PENDING();"
            "const trash=[...document.querySelectorAll('#pending-list button')]"
            ".find(button=>button.textContent==='不要了');trash.click();"
            "await waitFor(()=>document.querySelector('#trashed-list .pending-row'));"
            "document.querySelector('.lobe[data-mode=\"canvas\"]').click();await frame();"
            "await waitFor(()=>document.querySelector('#trash-btn').classList.contains('show'));"
            "document.querySelector('#trash-btn').click();await frame();"
            "const count=document.querySelector('#trash-n').textContent;"
            "const caption=document.querySelector('.trash-card .tc-cap').textContent;"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({count,caption});document.body.appendChild(pre);",
            pending_entry(), remote_state, width=1000,
        )
        self.assertFalse(result.get("error"), result)
        self.assertEqual(result["count"], "1", result)
        self.assertIn("pending note", result["caption"], result)
        self.assertEqual(result["remoteState"]["trashed"], ["0099"], result)


if __name__ == "__main__":
    unittest.main()
