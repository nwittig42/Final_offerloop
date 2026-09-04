#!/usr/bin/env python3
"""
Offerloop Studio viewer, clip-first.

Run:  .venv/bin/python studio_app.py     then open http://localhost:8899
Phone on the same Wi-Fi: http://<this Mac's IP>:8899

Three tabs, phone-first:
  Clips  a vertical feed of every ready clip (the deliverable), search on top,
         Save and Source one tap away
  Reels  the entries: thumbnail, description, full breakdown
  Drops  the queue: what is cooking, waiting, or failed
Read-only; data refreshes from Notion every 45 seconds.
"""
import json
import os
import subprocess
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8899"))
PASSWORD = os.environ.get("STUDIO_PASSWORD", "")   # set on the hosted copy; empty = open (local Mac only)
CACHE_SECONDS = 45
_cache = {"t": 0, "rows": []}


def load_env():
    env = HERE / ".env"
    if not env.exists():      # hosted copy: variables come from the platform, no file
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def notion_query():
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{os.environ['NOTION_INBOX_DB']}/query",
        data=json.dumps({"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                         "page_size": 100}).encode(),
        headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                 "Notion-Version": "2022-06-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["results"]


def text_of(prop):
    return "".join(x["plain_text"] for x in prop.get("rich_text", prop.get("title", [])))


def files_of(prop):
    return [{"name": f.get("name", "file"),
             "url": (f.get("file") or f.get("external") or {}).get("url")}
            for f in prop.get("files", []) if (f.get("file") or f.get("external") or {}).get("url")]


def rows():
    if time.time() - _cache["t"] > CACHE_SECONDS:
        out = []
        for r in notion_query():
            p = r["properties"]
            rid = r["id"].replace("-", "")
            clips = files_of(p.get("Clip files", {}))
            # posters uploaded by the watcher: "poster_<clip name>.jpg" pairs with its clip
            hosted_posters = {}
            for f in files_of(p.get("Clip posters", {})):
                n = f["name"]
                if n.startswith("poster_") and n.endswith(".jpg"):
                    hosted_posters[n[len("poster_"):-4]] = f["url"]
            for c in clips:
                local = HERE / "inbox_runs" / rid / c["name"]
                if not local.exists() and c["name"] == "reel.mp4":
                    local = HERE / "inbox_runs" / rid / "work" / "reel.mp4"
                if local.exists():
                    c["local"] = f"/media/{rid}/{c['name']}"
                    c["poster"] = f"/poster/{rid}/{c['name']}.jpg"
                elif c["name"] in hosted_posters:
                    c["poster"] = hosted_posters[c["name"]]
            out.append({
                "id": rid,
                "title": text_of(p["Title"]) or "(untitled)",
                "status": (p["Status"]["select"] or {}).get("name", ""),
                "url": p.get("Reel URL", {}).get("url"),
                "sent_by": text_of(p.get("Sent by", {})),
                "description": text_of(p.get("Description", {})),
                "sources": text_of(p.get("Source clips", {})),
                "tags": text_of(p.get("Tags", {})),
                "clips": clips,
                "thumb": (files_of(p.get("Thumbnail", {})) or [{}])[0].get("url"),
                "created": r["created_time"][:10],
                "notion_url": r["url"],
            })
        _cache.update(t=time.time(), rows=out)
    return _cache["rows"]


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offerloop Studio</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Inter:wght@400;500;600&display=swap">
<style>
:root{--paper:#fff;--surface:#F5F6F8;--ink:#111318;--ink2:#4B5160;--ink3:#8A90A0;--hair:#E5E5E0;
--accent:#4A60A8;--accent-soft:#E9EDF8;--ok:#2E7D4F;--ok-soft:#E3F3E9;--warn:#A6681A;--warn-soft:#FBEEDC;
--crit:#B03A3A;--crit-soft:#F9E3E3}
@media (prefers-color-scheme: dark){:root{--paper:#16181D;--surface:#101215;--ink:#F2F3F6;--ink2:#B6BAC6;
--ink3:#7D8291;--hair:#2A2E37;--accent:#8FA3E6;--accent-soft:#232A40;--ok:#6CC397;--ok-soft:#1C2E24;
--warn:#E0A85A;--warn-soft:#33281A;--crit:#E07A7A;--crit-soft:#361F1F}}
*{box-sizing:border-box}
html{scroll-padding-top:70px}
body{margin:0;background:var(--surface);color:var(--ink);font-family:Inter,system-ui,sans-serif;font-size:15px;line-height:1.5;padding-bottom:76px}
.top{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--hair);padding:10px 14px;display:flex;align-items:center;gap:10px}
.top h1{font-family:"Instrument Serif",Georgia,serif;font-weight:400;font-size:22px;margin:0;white-space:nowrap}
.search{flex:1;min-width:0;border:1px solid var(--hair);border-radius:8px;padding:8px 12px;background:var(--paper);color:var(--ink);font:inherit;font-size:14px}
.search::placeholder{color:var(--ink3)}
main{max-width:1180px;margin:0 auto;padding:12px 12px 30px}
.pill{font-size:11px;font-weight:500;padding:2px 8px;border-radius:8px;display:inline-block;white-space:nowrap}
.t-self{background:var(--ok-soft);color:var(--ok)}
.t-library,.t-meme{background:var(--accent-soft);color:var(--accent)}
.t-youtube,.t-social,.t-retry,.t-vision{background:var(--warn-soft);color:var(--warn)}
.s-Clipsready{background:var(--ok-soft);color:var(--ok)}
.s-Linksonly{background:var(--warn-soft);color:var(--warn)}
.s-Inspiration{background:var(--accent-soft);color:var(--accent)}
.s-Failed{background:var(--crit-soft);color:var(--crit)}
.s-Inbox,.s-Processing{background:var(--surface);color:var(--ink3);border:1px solid var(--hair)}

/* the tile: reel beside its clip, everywhere */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.tile{background:var(--paper);border:1px solid var(--hair);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.pair{display:grid;grid-template-columns:1fr 1fr;height:160px;background:#0d0f14;gap:2px}
.half{position:relative;overflow:hidden;background:#0d0f14}
.half img,.half video{width:100%;height:100%;object-fit:cover;display:block}
.half.src{cursor:pointer}
.half.src img{opacity:.85}
.lbl{position:absolute;bottom:6px;left:6px;font-size:10px;font-weight:500;padding:1px 6px;border-radius:6px;background:rgba(0,0,0,.6);color:#fff;pointer-events:none}
.more{position:absolute;top:6px;right:6px;font-size:11px;font-weight:600;padding:1px 7px;border-radius:6px;background:rgba(0,0,0,.6);color:#fff}
.nothing{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#7D8291;font-size:12px;text-align:center;padding:8px}
.tb{padding:8px 10px 10px;display:flex;flex-direction:column;gap:6px;flex:1}
.tdesc{font-size:13px;line-height:1.35;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:11px;padding:1px 7px;border-radius:6px;background:var(--accent-soft);color:var(--accent);cursor:pointer;border:0;font-family:inherit}
.chip:hover{filter:brightness(.92)}
.tmeta{font-size:11px;color:var(--ink3);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.actions{display:flex;gap:6px;margin-top:auto}
.btn{flex:1;text-align:center;border:1px solid var(--hair);border-radius:8px;padding:7px 0;font-size:13px;font-weight:500;text-decoration:none;color:var(--ink);background:var(--paper)}
.btn.primary{background:var(--ink);color:var(--paper);border-color:var(--ink)}
details summary{font-size:12px;color:var(--accent);cursor:pointer}
.src-text{font-size:12px;color:var(--ink2);white-space:pre-wrap;word-break:break-word;margin-top:6px}
.src-text a{color:var(--accent)}

/* drops */
.drop{background:var(--paper);border:1px solid var(--hair);border-radius:10px;padding:12px 14px;margin-bottom:10px;display:flex;gap:10px;align-items:center}
.drop .t{flex:1;min-width:0;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drop .d{font-size:12px;color:var(--ink3)}

nav{position:fixed;bottom:0;left:0;right:0;background:var(--paper);border-top:1px solid var(--hair);display:flex;z-index:6;padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;border:0;background:none;color:var(--ink3);font:inherit;font-size:13px;font-weight:500;padding:13px 0;cursor:pointer}
nav button.on{color:var(--ink)}
nav button.on span{border-bottom:2px solid var(--ink);padding-bottom:3px}
.empty{text-align:center;color:var(--ink3);padding:60px 0;grid-column:1/-1}
.count{font-size:12px;color:var(--ink3);margin:2px 2px 10px}
.feels{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
</style></head><body>
<div class="top"><h1 id="title">Clips</h1><input class="search" id="q" placeholder="Search by feeling or scene (struggle, smug, thanos...)"></div>
<main id="main"></main>
<nav>
<button id="nb-clips" class="on" onclick="go('clips')"><span>Clips</span></button>
<button id="nb-reels" onclick="go('reels')"><span>Reels</span></button>
<button id="nb-drops" onclick="go('drops')"><span>Drops</span></button>
</nav>
<script>
const ROWS = __ROWS__;
let view='clips', q='';
function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML}
function tierOf(row, clipName){
  const m=(row.sources||'').match(/via (\\w+)\\]/);
  if((clipName||'').includes('_self')) return 'self';
  return m?m[1]:null;
}
function tierPill(t){
  const label={self:'from the reel',library:'from your library',meme:'from Giphy/Tenor',youtube:'from YouTube',social:'from TikTok/IG',retry:'retry search',vision:'vision-verified'}[t]||t;
  return t?`<span class="pill t-${t}">${label}</span>`:'';
}
function statusPill(s){return `<span class="pill s-${(s||'').replace(/ /g,'')}">${esc(s)}</span>`}
// Tags text: "feels: a, b | shows: ... | used for: ... | tags: x, y"
function parseTags(t){
  const o={feels:[],shows:'',used:'',tags:[]};
  (t||'').split('|').forEach(part=>{
    const i=part.indexOf(':'); if(i<0) return;
    const k=part.slice(0,i).trim().toLowerCase(), v=part.slice(i+1).trim();
    if(k==='feels') o.feels=v.split(',').map(x=>x.trim()).filter(Boolean);
    else if(k==='shows') o.shows=v;
    else if(k==='used for') o.used=v;
    else if(k==='tags') o.tags=v.split(',').map(x=>x.trim()).filter(Boolean);
  });
  return o;
}
function haystack(r){return [r.title,r.description,r.sources,r.tags].join(' ').toLowerCase()}
function match(r){ if(!q) return true; return q.split(/\\s+/).every(w=>haystack(r).includes(w)); }
function chips(tg,n){return tg.feels.slice(0,n).map(f=>`<button class="chip" onclick="setQ('${esc(f)}')">${esc(f)}</button>`).join('')}
function clipHalf(c,row){
  if(c&&c.url) return `<div class="half"><video controls muted playsinline preload="${c.local||c.poster?'metadata':'none'}" ${c.poster?`poster="${c.poster}"`:''} src="${c.local||c.url}"></video><span class="lbl">clip</span></div>`;
  return `<div class="half"><div class="nothing">${row.status==='Links only'?'no clean cut, links in details':'no clip'}</div></div>`;
}
function srcHalf(row,extra){
  const open=row.url?`onclick="window.open('${row.url}','_blank')"`:'';
  return `<div class="half src" ${open}>${row.thumb?`<img src="${row.thumb}" alt="" loading="lazy">`:''}<span class="lbl">reel</span>${extra||''}</div>`;
}
// one tile for a clip (Clips view) or a whole row (Reels view)
function tile(row, clip, opts){
  opts=opts||{};
  const tg=parseTags(row.tags);
  const t=clip?tierOf(row,clip.name):null;
  const more=opts.count>1?`<span class="more">+${opts.count-1}</span>`:'';
  return `<article class="tile">
    <div class="pair">${srcHalf(row,opts.status?statusPill(row.status).replace('class="pill','class="pill more" style="left:auto;top:auto;bottom:6px;right:6px;position:absolute;'):'')}${clipHalf(clip,row).replace('<span class="lbl">clip</span>','<span class="lbl">clip</span>'+more)}</div>
    <div class="tb">
      <p class="tdesc" title="${esc(tg.used||'')}">${esc(tg.shows||row.description||row.title)}</p>
      ${tg.feels.length?`<div class="chips">${chips(tg,4)}</div>`:''}
      ${tg.used?`<div class="tmeta">used for: ${esc(tg.used)}</div>`:''}
      <div class="tmeta">${tierPill(t)}<span>${esc((row.sent_by||'?').split(' ')[0])} · ${row.created}</span></div>
      ${opts.details?`<details><summary>Details</summary><div class="src-text">${esc(row.sources)}</div><div class="src-text"><a href="${row.notion_url}" target="_blank">Open in Notion</a></div></details>`:''}
      <div class="actions">
        ${clip&&clip.url?`<a class="btn primary" href="${clip.url}" target="_blank" rel="noopener">Save clip</a>`:''}
        ${row.url?`<a class="btn" href="${row.url}" target="_blank" rel="noopener">Source reel</a>`:''}
      </div>
    </div></article>`;
}
function feelCloud(rows){
  const c={}; rows.forEach(r=>parseTags(r.tags).feels.forEach(f=>c[f]=(c[f]||0)+1));
  const top=Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,12);
  return top.length?`<div class="feels">${top.map(([f,n])=>`<button class="chip" onclick="setQ('${esc(f)}')">${esc(f)} ${n}</button>`).join('')}</div>`:'';
}
function render(){
  document.getElementById('title').textContent={clips:'Clips',reels:'Reels',drops:'Drops'}[view];
  ['clips','reels','drops'].forEach(v=>document.getElementById('nb-'+v).className=(v===view?'on':''));
  document.getElementById('q').style.display=view==='drops'?'none':'';
  const m=document.getElementById('main');
  if(view==='clips'){
    const list=[]; ROWS.filter(match).forEach(r=>r.clips.forEach(c=>{ if(c.url) list.push({c,r}); }));
    m.innerHTML=(q?'':feelCloud(ROWS))+`<div class="count">${list.length} clip${list.length!==1?'s':''}${q?' matching "'+esc(q)+'"':' ready'}</div><div class="grid">`+
      (list.map(x=>tile(x.r,x.c)).join('')||'<div class="empty">No clips match. Try a feeling (struggle, smug, relief) or a scene word.</div>')+`</div>`;
  } else if(view==='reels'){
    const list=ROWS.filter(match);
    m.innerHTML=`<div class="count">${list.length} reel${list.length!==1?'s':''}</div><div class="grid">`+
      (list.map(r=>tile(r,r.clips[0]||null,{count:r.clips.length,status:true,details:true})).join('')||'<div class="empty">Nothing yet.</div>')+`</div>`;
  } else {
    const busy=ROWS.filter(r=>['Inbox','Processing'].includes(r.status));
    const bad=ROWS.filter(r=>r.status==='Failed');
    const done=ROWS.filter(r=>!['Inbox','Processing','Failed'].includes(r.status)).slice(0,10);
    const row=r=>`<div class="drop">${statusPill(r.status)}<div class="t">${esc(r.title)}<div class="d">from ${esc((r.sent_by||'?').split(' ')[0])} · ${r.created}</div></div></div>`;
    m.innerHTML=(busy.length?`<div class="count">Cooking now</div>`+busy.map(row).join(''):'')+
      (bad.length?`<div class="count">Needs attention</div>`+bad.map(row).join(''):'')+
      `<div class="count">Recently finished</div>`+(done.map(row).join('')||'<div class="empty">Nothing yet.</div>');
  }
}
function go(v){view=v;render();window.scrollTo(0,0)}
function setQ(w){q=w.toLowerCase();document.getElementById('q').value=w;if(view==='drops')view='clips';render();window.scrollTo(0,0)}
document.getElementById('q').addEventListener('input',e=>{q=e.target.value.trim().toLowerCase();render()});
render();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_file(self, path, ctype):
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = int(b) if b else end
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def media_path(self, rid, name):
        if not (rid.isalnum() and "/" not in name and ".." not in name):
            return None
        path = HERE / "inbox_runs" / rid / name
        if not path.exists() and name == "reel.mp4":
            path = HERE / "inbox_runs" / rid / "work" / "reel.mp4"
        return path if path.exists() else None

    def authorized(self):
        if not PASSWORD:
            return True
        import base64
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user_pw = base64.b64decode(h[6:]).decode()
                return user_pw.split(":", 1)[-1] == PASSWORD
            except Exception:
                return False
        return False

    def do_GET(self):
        if self.path.split("?")[0] == "/health":     # Render's health check, no password
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Offerloop Studio"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Offerloop Studio: password required")
            return
        try:
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) == 3 and parts[0] in ("media", "poster"):
                name = parts[2][:-4] if parts[0] == "poster" and parts[2].endswith(".jpg") else parts[2]
                src = self.media_path(parts[1], name)
                if not src:
                    self.send_response(404); self.end_headers(); return
                if parts[0] == "media":
                    return self.send_file(src, "video/mp4")
                poster = src.parent / "posters" / (name + ".jpg")
                if not poster.exists():
                    poster.parent.mkdir(exist_ok=True)
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(src),
                                    "-frames:v", "1", "-vf", "scale=480:-2", str(poster)], capture_output=True)
                if not poster.exists():
                    self.send_response(404); self.end_headers(); return
                return self.send_file(poster, "image/jpeg")
            data = rows()
            page = PAGE.replace("__ROWS__", json.dumps(data))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"error: {e}".encode())


if __name__ == "__main__":
    load_env()
    print(f"Offerloop Studio viewer: http://localhost:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
