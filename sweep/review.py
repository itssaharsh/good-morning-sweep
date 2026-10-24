"""The review page: every suggestion with its reason, grouped, big enough for a parent to check on the laptop.

Served on 127.0.0.1 only. Tap a picture to keep it. Nothing is deleted until "Delete from phone" is pressed, and the
laptop keeps its copy of every file either way. `--static` writes a self-contained demo copy with no server."""
import base64
import hmac
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import guard, media
from .db import now, set_meta, get_meta


def items(con, buckets=None):
    rows = con.execute("""
        SELECT s.file_id AS id, s.bucket, s.reason, s.rule, s.preselected, f.bytes, f.folder, f.phone_path,
               d.action AS decided, x.ok AS deleted
        FROM suggestions s JOIN files f ON f.id = s.file_id
        LEFT JOIN decisions d ON d.file_id = s.file_id
        LEFT JOIN deletions x ON x.file_id = s.file_id
        ORDER BY f.bytes DESC""").fetchall()
    out = []
    for r in rows:
        if buckets and r["bucket"] not in buckets:
            continue
        sel = (r["decided"] == "delete") if r["decided"] else bool(r["preselected"])
        out.append({"id": r["id"], "bucket": r["bucket"], "reason": r["reason"], "bytes": r["bytes"],
                    "video": media.is_video(r["phone_path"]), "sel": sel, "gone": bool(r["deleted"]),
                    "name": r["phone_path"].rsplit("/", 1)[-1]})
    return out


def summary(con):
    f = con.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM files").fetchone()
    seen = con.execute("SELECT COUNT(*) n FROM verdicts WHERE kind IS NOT NULL").fetchone()["n"]
    return {"files": f["n"], "bytes": f["b"], "looked_at": seen, "device": get_meta(con, "device", "the phone"),
            "free_before": get_meta(con, "free_before"), "free_after": get_meta(con, "free_after")}


def save(con, delete_ids, keep_ids=None):
    """Store the person's decisions. delete_ids: the ticked pictures they saw. keep_ids: the ones they saw and
    unticked. Pictures they never scrolled to get no decision, so they stay on the phone and keep their tick for
    next time. Without keep_ids (the command line), every other suggestion is recorded as keep."""
    ids = [r["file_id"] for r in con.execute("SELECT file_id FROM suggestions")]
    dels = set(int(i) for i in delete_ids)
    keeps = None if keep_ids is None else set(int(i) for i in keep_ids)
    with con:
        for fid in ids:
            if fid in dels:
                act = "delete"
            elif keeps is None or fid in keeps:
                act = "keep"
            else:
                con.execute("DELETE FROM decisions WHERE file_id=? AND action='delete'", (fid,))
                continue
            con.execute("INSERT INTO decisions(file_id, action, at) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE "
                        "SET action=excluded.action, at=excluded.at", (fid, act, now()))
    set_meta(con, "review_saved_at", now())
    return len(dels & set(ids))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Good Morning Sweep</title>
<style>
:root{--bg:#f7f3ec;--card:#fffdf9;--ink:#1f1b16;--soft:#6b6257;--line:#e6ddd0;--sun:#e8862a;--sun-ink:#8a4a0c;
--keep:#2f7d5b;--del:#b8432f;--shadow:0 1px 2px rgba(60,40,10,.06),0 4px 14px rgba(60,40,10,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#17140f;--card:#211d17;--ink:#f3ede4;
--soft:#a99d8d;--line:#3a332a;--sun:#f0a050;--sun-ink:#f6c48f;--keep:#5fc497;--del:#ef7d66;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);
font:17px/1.45 "Segoe UI","Nirmala UI","Noto Sans Devanagari","Noto Sans",system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 92%,transparent);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.bar{max-width:1180px;margin:0 auto;padding:14px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.brand{display:flex;gap:10px;align-items:center;font-weight:700;font-size:19px}
.sun{width:28px;height:28px;border-radius:50%;background:radial-gradient(circle at 50% 60%,#ffd27a,var(--sun));
box-shadow:0 0 0 4px color-mix(in srgb,var(--sun) 20%,transparent)}
.total{margin-left:auto;text-align:right}.total b{font-size:26px;letter-spacing:-.01em}.total small{display:block;color:var(--soft)}
button{font:inherit;border:0;border-radius:12px;cursor:pointer}
.go{background:var(--del);color:#fff;padding:12px 18px;font-weight:650}.go:disabled{opacity:.45;cursor:not-allowed}
main{max-width:1180px;margin:0 auto;padding:8px 16px 80px}
.intro{color:var(--soft);margin:16px 0 4px;max-width:70ch}
.demo{margin:16px 0 0;padding:12px 14px;border-radius:12px;background:color-mix(in srgb,var(--sun) 14%,var(--card));
color:var(--ink);max-width:80ch;font-size:15px}.demo a{color:var(--sun-ink)}
section{margin-top:28px}
.sh{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:8px}
.sh h2{margin:0;font-size:21px}.sh .meta{color:var(--soft)}
.sh .all{margin-left:auto;background:transparent;color:var(--sun-ink);padding:6px 8px;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-top:14px}
.tile{position:relative;background:var(--card);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;
border:2px solid transparent;transition:border-color .15s,opacity .15s}
.tile.broken .ph{opacity:.3}
.tile.sel{border-color:color-mix(in srgb,var(--del) 70%,transparent)}
.tile:not(.sel){opacity:.72}.tile.gone{opacity:.35;pointer-events:none}
.tile .ph{display:block;width:100%;aspect-ratio:1;object-fit:cover;background:var(--line);cursor:pointer}
.tile.shot .ph{object-position:top}
.badge{position:absolute;top:8px;left:8px;padding:3px 9px;border-radius:999px;font-size:13px;font-weight:650;color:#fff}
.sel .badge{background:var(--del)}.tile:not(.sel) .badge{background:var(--keep)}
.vid{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);color:#fff;border-radius:8px;padding:2px 7px;font-size:12px}
.zoom{position:absolute;right:8px;top:calc(100% - 40px);width:32px;height:32px;border-radius:50%;
background:rgba(0,0,0,.55);color:#fff;font-size:16px;line-height:32px;text-align:center;padding:0}
.tile .imgwrap{position:relative}
.unseen{color:var(--soft);font-size:14px;margin-top:2px}
.why{padding:9px 11px 11px;font-size:14px;line-height:1.35}.why .sz{color:var(--soft);font-size:12.5px;margin-top:4px}
.empty{color:var(--soft);padding:40px 0;text-align:center}
dialog{border:0;border-radius:16px;padding:0;max-width:min(92vw,900px);background:var(--card);color:var(--ink)}
dialog img{display:block;max-width:100%;max-height:78vh}dialog p{margin:0;padding:12px 16px}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--ink);color:var(--bg);
padding:12px 18px;border-radius:12px;display:none;max-width:90vw}
@media (max-width:640px){.grid{grid-template-columns:repeat(2,1fr);gap:10px}.total{margin-left:0;text-align:left}}
</style></head><body>
<header><div class="bar"><div class="brand"><span class="sun"></span>Good Morning Sweep</div>
<div class="total"><b id="tot">0 GB</b><small id="totn">to free</small><small class="unseen" id="unseen"></small></div>
<button class="go" id="go">Delete from phone</button></div></header>
<main><p class="demo" id="demo" hidden></p><p class="intro" id="intro"></p><div id="groups"></div></main>
<dialog id="dlg"><img id="big" alt=""><p id="bigwhy"></p></dialog><div class="toast" id="toast"></div>
<script>
const DATA = __DATA__;
const STATIC = __STATIC__;
const GROUPS = DATA.groups, S = DATA.summary;
let items = DATA.items;
const gb = b => b >= 1e9 ? (b/1e9).toFixed(1)+' GB' : b >= 1e6 ? Math.round(b/1e6)+' MB' : Math.max(1, Math.round(b/1e3))+' KB';
const src = it => STATIC ? DATA.thumbs[it.id] : '/t/'+it.id+'.jpg';
if (STATIC && DATA.note){ const d=document.getElementById('demo'); d.hidden=false; d.innerHTML=DATA.note; }
const tiles = new Map();
const seenObs = ('IntersectionObserver' in window) ? new IntersectionObserver(es => {
  for (const e of es) if (e.isIntersecting) { const it = tiles.get(e.target); if (it && !it.seen) { it.seen = true; totals(); } }
}, {threshold: 0.6}) : null;
function paint(t, it){
  t.classList.toggle('sel', it.sel && !it.gone); t.classList.toggle('gone', it.gone);
  t.querySelector('.badge').textContent = it.gone ? 'Deleted' : (it.sel ? 'Delete' : 'Keep');
}
function totals(){
  const sel = items.filter(i=>i.sel && !i.gone && (i.seen || STATIC));
  const later = items.filter(i=>i.sel && !i.gone && !i.seen && !STATIC).length;
  document.getElementById('tot').textContent = gb(sel.reduce((a,i)=>a+i.bytes,0));
  document.getElementById('totn').textContent = `to free · ${sel.length.toLocaleString()} pictures`;
  document.getElementById('unseen').textContent = later ? `${later.toLocaleString()} more ticked below: scroll to them, or they stay on the phone` : '';
  document.getElementById('go').disabled = !sel.length;
}
function render(){
  const g = document.getElementById('groups'); g.innerHTML=''; tiles.clear(); if (seenObs) seenObs.disconnect();
  document.getElementById('intro').textContent =
    `Gemma looked at ${S.looked_at.toLocaleString()} pictures from ${S.device} on this laptop. `+
    `Red pictures will be deleted from the phone, but only the ones you have scrolled past. Tap a picture to keep it; `+
    `tap the magnifier to see it bigger. Everything stays saved on the laptop.`;
  for (const [key,title] of GROUPS){
    const list = items.filter(i=>i.bucket===key); if(!list.length) continue;
    const sec = document.createElement('section');
    const bytes = list.reduce((a,i)=>a+i.bytes,0);
    sec.innerHTML = `<div class="sh"><h2>${title}</h2><span class="meta">${list.length.toLocaleString()} · ${gb(bytes)}</span>
      <button class="all">${list.every(i=>i.sel)?'Keep all':'Delete all'}</button></div><div class="grid"></div>`;
    const grid = sec.querySelector('.grid');
    for (const it of list){
      const t = document.createElement('div');
      t.className = 'tile'+(key==='screenshots'?' shot':''); t.dataset.id = it.id;
      t.innerHTML = `<div class="imgwrap"><img class="ph" loading="lazy" alt="" src="${src(it)}"><button class="zoom" title="See it bigger" aria-label="See it bigger"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" style="vertical-align:-3px"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg></button></div>
        <span class="badge"></span>${it.video?'<span class="vid">video</span>':''}<div class="why"></div>`;
      t.querySelector('.why').textContent = it.reason;
      const sz = document.createElement('div'); sz.className='sz'; sz.textContent = gb(it.bytes); t.querySelector('.why').appendChild(sz);
      t.querySelector('.ph').onclick = () => { if (it.gone) return; it.sel = !it.sel; paint(t, it); totals(); };
      t.querySelector('.ph').oncontextmenu = e => {e.preventDefault(); zoom(it);};
      t.querySelector('.zoom').onclick = () => zoom(it);
      paint(t, it); tiles.set(t, it);
      const im = t.querySelector('.ph');
      const watch = () => { if (seenObs) seenObs.observe(t); else { it.seen = true; totals(); } };
      if (im.complete && im.naturalWidth) watch(); else { im.addEventListener('load', watch, {once:true}); }
      im.addEventListener('error', () => { t.classList.add('broken'); });
      grid.appendChild(t);
    }
    const allBtn = sec.querySelector('.all');
    allBtn.onclick = () => { const on=!list.every(i=>i.sel); list.forEach(i=>{ if(!i.gone) i.sel=on; });
      for (const [t,i] of tiles) if (i.bucket===key) paint(t,i); allBtn.textContent = on?'Keep all':'Delete all'; totals(); };
    g.appendChild(sec);
  }
  if(!items.length) g.innerHTML='<p class="empty">Nothing to suggest. This phone is already tidy.</p>';
  totals();
}
function zoom(it){ document.getElementById('big').src = STATIC ? src(it) : '/full/'+it.id+'.jpg';
  document.getElementById('bigwhy').textContent = it.reason; document.getElementById('dlg').showModal(); }
document.getElementById('dlg').onclick = e => e.currentTarget.close();
function toast(m){const t=document.getElementById('toast'); t.textContent=m; t.style.display='block'; setTimeout(()=>t.style.display='none',6000);}
document.getElementById('go').onclick = async () => {
  const sel = items.filter(i=>i.sel && !i.gone && (i.seen || STATIC));
  if (STATIC){ toast(`Demo: on a real phone this deletes ${sel.length} pictures (${gb(sel.reduce((a,i)=>a+i.bytes,0))}).`); return; }
  if (!confirm(`Delete ${sel.length} pictures from the phone? The laptop keeps a copy of each.`)) return;
  const b = document.getElementById('go'); b.disabled = true; b.textContent = 'Deleting…';
  const r = await fetch('/api/apply', {method:'POST', headers:{'X-Sweep-Token': DATA.token},
    body: JSON.stringify({delete: sel.map(i=>i.id), keep: items.filter(i=>!i.sel && !i.gone && i.seen).map(i=>i.id), suggested_at: DATA.suggested_at})});
  const j = await r.json(); b.textContent = 'Delete from phone';
  if (!r.ok){ toast(j.error || 'Something went wrong'); totals(); return; }
  const fresh = new Map(j.items.map(x=>[x.id,x]));
  for (const [t,i] of tiles){ const f = fresh.get(i.id); if (f){ i.gone = f.gone; i.sel = f.sel; paint(t,i); } }
  totals(); toast(`Done. ${j.deleted} deleted, ${gb(j.freed)} freed.` + (j.held ? ` ${j.held} kept for safety.` : ''));
};
render();
</script></body></html>"""


# A public demo page may only show pictures nobody in the family took: forwards, and copies of forwards.
PUBLIC_BUCKETS = ("forwards",)


def page(con, static=False, thumb_dir=None, buckets=None, note=None, token=""):
    its = items(con, buckets)
    data = {"groups": [[b, t] for b, t, _ in guard.BUCKETS], "summary": summary(con), "items": its, "note": note,
            "token": "" if static else token, "suggested_at": get_meta(con, "suggested_at", "")}
    if static:
        data["thumbs"] = {}
        for it in its:
            row = con.execute("SELECT local_path FROM files WHERE id=?", (it["id"],)).fetchone()
            jpg = media.thumbnail(row["local_path"], thumb_dir, it["id"], side=260)
            if jpg:
                data["thumbs"][it["id"]] = "data:image/jpeg;base64," + base64.b64encode(jpg).decode()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob).replace("__STATIC__", "true" if static else "false")


def serve(con_factory, workspace, apply_fn, port=8777, log=print):
    thumbs = Path(workspace) / "thumbs"
    lock = threading.Lock()
    token = secrets.token_urlsafe(18)   # only this page can ask for a deletion

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") or "json" in ctype
                             else "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):   # refuse DNS-rebinding pages: only this machine's own names
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.host_ok():
                return self.send(421, b"wrong host", "text/plain")
            path = urllib.parse.urlparse(self.path).path
            con = con_factory()
            try:
                if path == "/":
                    return self.send(200, page(con, token=token).encode(), "text/html; charset=utf-8")
                for prefix, side, sub in (("/t/", 320, "s"), ("/full/", 1200, "l")):
                    if path.startswith(prefix) and path.endswith(".jpg"):
                        fid = int(path[len(prefix):-4])
                        row = con.execute("SELECT local_path FROM files WHERE id=?", (fid,)).fetchone()
                        jpg = media.thumbnail(row["local_path"], thumbs / sub, fid, side=side) if row else None
                        return self.send(200, jpg, "image/jpeg") if jpg else self.send(404, b"", "text/plain")
                self.send(404, b"not found", "text/plain")
            finally:
                con.close()

        def do_POST(self):
            if not self.host_ok():
                return self.send(421, b"{}", "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            con = con_factory()
            try:
                if self.path == "/api/apply":
                    if not hmac.compare_digest(self.headers.get("X-Sweep-Token", ""), token):
                        return self.send(403, json.dumps({"error": "This page is out of date. Reload it."}).encode(),
                                         "application/json")
                    if str(body.get("suggested_at", "")) != str(get_meta(con, "suggested_at", "")):
                        return self.send(409, json.dumps({"error": "The suggestions changed since this page was "
                                                          "opened. Reload it and look again."}).encode(),
                                         "application/json")
                    with lock:
                        save(con, body.get("delete", []), body.get("keep", []))
                        res = apply_fn(con)
                    return self.send(200, json.dumps(dict(res, items=items(con))).encode(), "application/json")
                self.send(404, b"{}", "application/json")
            except Exception as e:
                self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            finally:
                con.close()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    log(f"Review page: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    srv.serve_forever()
