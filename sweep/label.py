"""A labelling page for scoring: one picture at a time, J for junk, K for keep. The person does not see what the
model or the guard said, so the labels can score both. Half the sample comes from the suggestions, half from what
the guard kept."""
import json
import random
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import media
from .db import now

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Label pictures</title><style>
body{margin:0;background:#15120e;color:#f3ede4;font:17px system-ui,sans-serif;display:flex;flex-direction:column;align-items:center}
img{max-width:96vw;max-height:72vh;margin:16px 0;border-radius:10px;background:#333}
.row{display:flex;gap:14px}button{font:inherit;font-size:20px;padding:14px 26px;border:0;border-radius:12px;cursor:pointer}
.j{background:#b8432f;color:#fff}.k{background:#2f7d5b;color:#fff}.s{background:#3a332a;color:#f3ede4}p{color:#a99d8d}
</style></head><body><p id="n"></p><img id="im" alt=""><div class="row">
<button class="j" onclick="lab('junk')">Junk (J)</button><button class="s" onclick="skip()">Skip (S)</button>
<button class="k" onclick="lab('keep')">Keep (K)</button></div>
<p>Junk = she would happily lose it (forwards, copies, mistakes). Keep = anything she might want.</p>
<script>
const IDS=__IDS__; let i=0;
async function load(){ while(i<IDS.length && IDS[i][1]) i++; document.getElementById('n').textContent=`${i+1} / ${IDS.length}`;
  if(i>=IDS.length){document.body.innerHTML='<h2>Done. Thank you.</h2>';return;} document.getElementById('im').src='/full/'+IDS[i][0]+'.jpg';}
async function lab(l){ await fetch('/label',{method:'POST',body:JSON.stringify({id:IDS[i][0],label:l})}); IDS[i][1]=l; i++; load(); }
function skip(){ i++; load(); }
document.onkeydown=e=>{const k=e.key.toLowerCase(); if(k==='j')lab('junk'); if(k==='k')lab('keep'); if(k==='s')skip();};
load();
</script></body></html>"""


def sample_alone(con, n, seed=7):
    """The pictures Gemma alone would delete (any forward kind or 'accidental'), in random order: labelling them says
    how many of them a person wants to keep, which is what the rules are there to protect."""
    rng = random.Random(seed)
    ids = [r["file_id"] for r in con.execute(
        "SELECT v.file_id FROM verdicts v JOIN files f ON f.id=v.file_id WHERE v.kind IN "
        "('greeting','quote','meme','news','ad','accidental') AND f.folder != 'camera' ORDER BY v.file_id")]
    ids = rng.sample(ids, min(n, len(ids)))
    done = {r["file_id"]: r["label"] for r in con.execute("SELECT file_id, label FROM labels")}
    return [[i, done.get(i)] for i in ids]


def sample(con, n, seed=7):
    rng = random.Random(seed)
    sug = [r["file_id"] for r in con.execute("SELECT file_id FROM suggestions ORDER BY file_id")]
    kept = [r["id"] for r in con.execute(
        "SELECT f.id FROM files f WHERE f.sha1 IS NOT NULL AND f.folder != 'camera' AND f.id NOT IN "
        "(SELECT file_id FROM suggestions) ORDER BY f.id")]
    a = rng.sample(sug, min(len(sug), n // 2))
    b = rng.sample(kept, min(len(kept), n - len(a)))
    ids = a + b
    rng.shuffle(ids)
    done = {r["file_id"]: r["label"] for r in con.execute("SELECT file_id, label FROM labels")}
    return [[i, done.get(i)] for i in ids]


def serve(con_factory, workspace, n=200, port=8766, seed=7, log=print, alone=False):
    thumbs = Path(workspace) / "thumbs" / "l"
    con = con_factory()
    ids = sample_alone(con, n, seed) if alone else sample(con, n, seed)
    con.close()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.send_response(421); self.end_headers(); return
            path = urllib.parse.urlparse(self.path).path
            c = con_factory()
            try:
                if path == "/":
                    body = PAGE.replace("__IDS__", json.dumps(ids)).encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers(); self.wfile.write(body); return
                if path.startswith("/full/"):
                    fid = int(path[6:-4])
                    row = c.execute("SELECT local_path FROM files WHERE id=?", (fid,)).fetchone()
                    jpg = media.thumbnail(row["local_path"], thumbs, fid, side=1200) if row else None
                    self.send_response(200 if jpg else 404); self.send_header("Content-Type", "image/jpeg")
                    self.end_headers(); self.wfile.write(jpg or b""); return
                self.send_response(404); self.end_headers()
            finally:
                c.close()

        def do_POST(self):
            if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.send_response(421); self.end_headers(); return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            c = con_factory()
            try:
                with c:
                    c.execute("INSERT INTO labels(file_id, label, at) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE "
                              "SET label=excluded.label, at=excluded.at", (int(body["id"]), body["label"], now()))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            finally:
                c.close()

    log(f"Label page: http://127.0.0.1:{port}  ({len(ids)} pictures)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
