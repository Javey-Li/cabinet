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
            "for(let index=0;index<8;index++)await frame();"
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

    def test_canvas_word_labels_are_drawn_after_cards(self) -> None:
        result = run_page(
            "await window.CABINET_READY;for(let index=0;index<5;index++)await frame();"
            "const context=document.querySelector('#cv').getContext('2d');"
            "const fillRect=context.fillRect.bind(context);const fillText=context.fillText.bind(context);"
            "const events=[];let frameStart=0;"
            "context.fillRect=function(x,y,width,height){"
            "if(x===0&&y===0&&width===innerWidth&&height===innerHeight){events.push({type:'frame'});frameStart=events.length-1;}"
            "else events.push({type:'card'});return fillRect(x,y,width,height);};"
            "context.fillText=function(text,x,y){if(this.font.includes('18px'))events.push({type:'word',text:String(text)});"
            "return fillText(text,x,y);};"
            "for(let index=0;index<3;index++)await frame();"
            "const lastFrame=events.slice(frameStart);const lastCard=lastFrame.map((event,index)=>event.type==='card'?index:-1).filter(index=>index>=0).pop();"
            "const firstWord=lastFrame.findIndex(event=>event.type==='word');"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({lastCard,firstWord,events:lastFrame});document.body.appendChild(pre);"
        )
        self.assertFalse(result.get("error"), result)
        self.assertGreaterEqual(result["lastCard"], 0, result)
        self.assertGreater(result["firstWord"], result["lastCard"], result)

    def test_three_tabs_keep_equal_geometry_across_theme_language_and_width(self) -> None:
        result = run_page(
            "await window.CABINET_READY;await frame();const samples=[];"
            "for(const lang of ['zh','en']){window.setLang(lang);"
            "for(const theme of ['light','dark']){if(document.documentElement.dataset.theme!==theme)document.querySelector('#theme-toggle').click();"
            "for(const mode of ['canvas','discover','pending']){document.querySelector(`.lobe[data-mode='${mode}']`).click();"
            "const lobes=[...document.querySelectorAll('.lobe')].map(element=>{const rect=element.getBoundingClientRect();"
            "const label=element.querySelector('.seg-lbl').getBoundingClientRect();const style=getComputedStyle(element);"
            "return {mode:element.dataset.mode,width:rect.width,center:rect.left+rect.width/2,labelCenter:label.left+label.width/2,"
            "visible:style.display!=='none'&&rect.width>0,selected:element.classList.contains('on'),background:style.backgroundColor};});"
            "samples.push({lang,theme,mode,lobes});}}}"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({samples});document.body.appendChild(pre);",
            width=390,
        )
        self.assertFalse(result.get("error"), result)
        for sample in result["samples"]:
            with self.subTest(lang=sample["lang"], theme=sample["theme"], mode=sample["mode"]):
                self.assertTrue(all(lobe["visible"] for lobe in sample["lobes"]), sample)
                widths = [lobe["width"] for lobe in sample["lobes"]]
                self.assertLess(max(widths) - min(widths), 1, sample)
                for lobe in sample["lobes"]:
                    self.assertLess(abs(lobe["center"] - lobe["labelCenter"]), 1, sample)
                    # The selected tint is drawn once, in the SVG under the labels; the buttons stay clear.
                    self.assertIn(lobe["background"], ["transparent", "rgba(0, 0, 0, 0)"], sample)

    def test_tab_backgrounds_sit_under_their_labels(self) -> None:
        def script(lang: str, theme: str) -> str:
            return (
                "await window.CABINET_READY;await frame();const samples=[];"
                "const box=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,center:r.left+r.width/2};};"
                "const shape=id=>{const el=document.getElementById(id);if(!el)return null;const b=el.getBBox();"
                "if(!b.width)return null;const m=el.getScreenCTM();"
                "return {left:m.a*b.x+m.e,right:m.a*(b.x+b.width)+m.e,center:m.a*(b.x+b.width/2)+m.e};};"
                f"window.setLang('{lang}');"
                f"if(document.documentElement.dataset.theme!=='{theme}')document.querySelector('#theme-toggle').click();"
                "const sample=(mode,hover)=>{const tab=document.querySelector(`.lobe[data-mode='${mode}']`);"
                "const lobes=[...document.querySelectorAll('.lobe')].map(box);"
                "samples.push({mode,hover,label:box(tab.querySelector('.seg-lbl')),first:lobes[0],last:lobes[2],"
                "all:shape('seg-path'),on:shape('seg-on'),width:innerWidth});};"
                "for(const mode of ['canvas','discover','pending']){document.querySelector(`.lobe[data-mode='${mode}']`).click();"
                "await frame();sample(mode,false);}"
                # Hovering stretches the capsule outward; the tint and the label have to move with it.
                "const tab=document.querySelector('.lobe[data-mode=\"canvas\"]');tab.click();"
                "tab.dispatchEvent(new MouseEvent('mouseenter',{clientX:box(tab).center,clientY:20}));"
                "for(let index=0;index<4;index++)await frame();sample('canvas',true);"
                "const pre=document.createElement('pre');pre.id='test-result';"
                "pre.textContent=JSON.stringify({samples});document.body.appendChild(pre);"
            )
        for width in [1440, 390]:
            for lang in ["zh", "en"]:
                for theme in ["light", "dark"]:
                    result = run_page(script(lang, theme), width=width)
                    self.assertFalse(result.get("error"), result)
                    for sample in result["samples"]:
                        with self.subTest(width=width, lang=lang, theme=theme, mode=sample["mode"], hover=sample["hover"]):
                            self.assertIsNotNone(sample["on"], sample)
                            self.assertLess(abs(sample["on"]["center"] - sample["label"]["center"]), 1, sample)
                            self.assertGreaterEqual(sample["all"]["left"], -0.5, sample)
                            self.assertLessEqual(sample["all"]["right"], sample["width"] + 0.5, sample)
                            if not sample["hover"]:
                                self.assertLess(abs(sample["all"]["left"] - sample["first"]["left"]), 1, sample)
                                self.assertLess(abs(sample["all"]["right"] - sample["last"]["right"]), 1, sample)

    def test_canvas_feel_words_do_not_cover_cards(self) -> None:
        script = (
            "await window.CABINET_READY;const canvas=document.querySelector('canvas');const context=canvas.getContext('2d');"
            "let cards=[],words=[];const fillRect=context.fillRect,fillText=context.fillText,roundRect=context.roundRect;"
            "context.fillRect=function(x,y,width,height){if(x===0&&y===0&&width===innerWidth&&height===innerHeight){cards=[];words=[];}"
            "else cards.push({left:x,top:y,right:x+width,bottom:y+height});return fillRect.call(this,x,y,width,height);};"
            "context.fillText=function(text,x,y){if(this.font.includes('18px')){const w=this.measureText(String(text)).width;"
            "words.push({text:String(text),left:x-w/2,right:x+w/2,top:y-16,bottom:y+18});}return fillText.call(this,text,x,y);};"
            "for(let index=0;index<12;index++)await frame();"
            "const hit=(a,b)=>a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom;"
            "const overlaps=words.flatMap(word=>cards.filter(card=>hit(word,card)).map(card=>({word:word.text,card})));"
            "const pre=document.createElement('pre');pre.id='test-result';"
            "pre.textContent=JSON.stringify({words:words.length,cards:cards.length,overlaps});document.body.appendChild(pre);"
        )
        for width in [1440, 390]:
            result = run_page(script, width=width)
            with self.subTest(width=width):
                self.assertFalse(result.get("error"), result)
                self.assertGreater(result["words"], 0, result)
                self.assertGreater(result["cards"], 0, result)
                self.assertEqual(result["overlaps"], [], result)

    def test_narrow_pending_entry_trashes_and_restores(self) -> None:
        remote_state = {"trashed": [], "later": []}
        result = run_page(
            "await window.CABINET_READY;await frame();await window.CABINET_RENDER_PENDING();"
            "window.__summon('note-needle');await frame();"
            "const searchCount=document.querySelector('#qn').textContent;"
            "document.querySelector('#qx').click();"
            "const tab=document.querySelector('.lobe[data-mode=\"pending\"]');"
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
            "tabText:tab.textContent.trim()});document.body.appendChild(pre);",
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
        self.assertEqual(result["tabText"], "待拆", result)
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
