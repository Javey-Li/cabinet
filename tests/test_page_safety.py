import html
import json
import re
import shutil
import subprocess
import tempfile
import unittest
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


def run_page(script: str, entry: dict | None = None) -> dict:
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
            + script
            + "}catch(error){const pre=document.createElement('pre');pre.id='test-result';"
              "pre.textContent=JSON.stringify({error:String(error)});document.body.appendChild(pre);}})();</script>"
        )
        (root / "index.html").write_text(page.replace("</body>", harness + "</body>"), encoding="utf-8")
        profile = root / "profile"
        result = subprocess.run([
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-translate",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=5000",
            "--dump-dom",
            (root / "index.html").as_uri(),
        ], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise AssertionError(result.stderr[-2000:])
        match = re.search(r'<pre id="test-result">(.*?)</pre>', result.stdout, flags=re.S)
        if not match:
            raise AssertionError("test result missing from Chrome DOM\n" + result.stdout[-2000:])
        return json.loads(html.unescape(match.group(1)))


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


if __name__ == "__main__":
    unittest.main()
