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
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())["results"]


def text_of(prop):
    return "".join(x["plain_text"] for x in prop.get("rich_text", prop.get("title", [])))


def files_of(prop):
    return [{"name": f.get("name", "file"),
             "url": (f.get("file") or f.get("external") or {}).get("url")}
            for f in prop.get("files", []) if (f.get("file") or f.get("external") or {}).get("url")]


_lock = threading.Lock()


def rows():
    with _lock:            # one fetch at a time; others reuse the cache
        return _rows()


def _rows():
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


def query_all(db, body=None):
    out, cur = [], None
    while True:
        b = dict(body or {}); b["page_size"] = 100
        if cur:
            b["start_cursor"] = cur
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{db}/query", data=json.dumps(b).encode(),
            headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                     "Notion-Version": "2022-06-28", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            q = json.loads(r.read())
        out += q.get("results", [])
        if not q.get("has_more"):
            break
        cur = q["next_cursor"]
    return out


def num(prop):
    return prop.get("number")


def sel(prop):
    return (prop.get("select") or {}).get("name") or ""


def dt(prop):
    return ((prop.get("date") or {}).get("start") or "")[:19]


_cdata = {"t": 0, "data": None}


def creators_data():
    """Links, clicks, creators, posts for the Creators studio (cached like the inbox rows)."""
    with _lock:
        return _creators_data()


def _creators_data():
    if time.time() - _cdata["t"] > CACHE_SECONDS or _cdata["data"] is None:
        d = {"links": [], "clicks": [], "creators": [], "posts": []}
        env = os.environ
        if env.get("NOTION_LINKS_DB"):
            for r in query_all(env["NOTION_LINKS_DB"]):
                p = r["properties"]
                d["links"].append({"code": text_of(p["Code"]), "owner": text_of(p.get("Owner", {})),
                                   "target": p.get("Target", {}).get("url") or "", "destination": sel(p.get("Destination", {})),
                                   "active": bool(p.get("Active", {}).get("checkbox")), "notes": text_of(p.get("Notes", {}))})
        if env.get("NOTION_CLICKS_DB"):
            for r in query_all(env["NOTION_CLICKS_DB"]):
                p = r["properties"]
                d["clicks"].append({"code": text_of(p.get("Code", {})), "dest": text_of(p.get("Destination", {})),
                                    "device": sel(p.get("Device", {})), "when": dt(p.get("When", {}))})
        if env.get("NOTION_CREATORS_DB"):
            for r in query_all(env["NOTION_CREATORS_DB"]):
                p = r["properties"]
                d["creators"].append({"name": text_of(p["Name"]), "tiktok": text_of(p.get("TikTok", {})).lstrip("@"),
                                      "instagram": text_of(p.get("Instagram", {})).lstrip("@"), "code": text_of(p.get("Link code", {})),
                                      "track": sel(p.get("Track", {})), "active": bool(p.get("Active", {}).get("checkbox"))})
        if env.get("NOTION_POSTS_DB"):
            for r in query_all(env["NOTION_POSTS_DB"]):
                p = r["properties"]
                try:
                    hist = json.loads(text_of(p.get("History", {})) or "[]")
                except json.JSONDecodeError:
                    hist = []
                d["posts"].append({"title": text_of(p["Post"]), "creator": text_of(p.get("Creator", {})), "platform": sel(p.get("Platform", {})),
                                   "url": p.get("URL", {}).get("url") or "", "posted": dt(p.get("Posted", {}))[:10],
                                   "views": num(p.get("Views", {})), "likes": num(p.get("Likes", {})), "comments": num(p.get("Comments", {})),
                                   "shares": num(p.get("Shares", {})), "checked": dt(p.get("Last checked", {}))[:10], "history": hist})
        _cdata.update(t=time.time(), data=d)
    return _cdata["data"]


def log_click(code, dest, ua, referrer):
    """One Notion row per click, written off the request thread so the redirect is instant."""
    db = os.environ.get("NOTION_CLICKS_DB")
    if not db:
        return
    ua = ua or ""
    # only real browsers count: curl, uptime checks and link previews (iMessage, Slack, Facebook) are noise
    low = ua.lower()
    if "mozilla" not in low or any(b in low for b in ("bot", "curl", "facebookexternalhit", "facebot", "preview", "slack", "whatsapp", "telegram", "discord", "python", "wget", "render")):
        return
    device = ("iPhone" if "iPhone" in ua or "iPad" in ua else "Android" if "Android" in ua
              else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Other")
    when = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    body = {"parent": {"database_id": db}, "properties": {
        "Click": {"title": [{"text": {"content": f"{code} {when}"}}]},
        "Code": {"rich_text": [{"text": {"content": code}}]},
        "Destination": {"rich_text": [{"text": {"content": dest[:200]}}]},
        "Device": {"select": {"name": device}},
        "Referrer": {"rich_text": [{"text": {"content": (referrer or "")[:300]}}]},
        "When": {"date": {"start": when}}}}
    try:
        req = urllib.request.Request("https://api.notion.com/v1/pages", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
                                              "Notion-Version": "2022-06-28", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=25).read()
    except Exception as e:
        print("click log failed:", e)


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
/* creators studio */
.modes{display:flex;border:1px solid var(--hair);border-radius:8px;overflow:hidden}
.modes button{border:0;background:var(--paper);color:var(--ink3);font:inherit;font-size:12px;font-weight:500;padding:6px 10px;cursor:pointer}
.modes button.on{background:var(--ink);color:var(--paper)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile-stat{background:var(--paper);border:1px solid var(--hair);border-radius:10px;padding:12px 14px}
.tile-stat .n{font-family:"Instrument Serif",Georgia,serif;font-size:28px;line-height:1.1}
.tile-stat .l{font-size:12px;color:var(--ink3)}
.tile-stat .d{font-size:12px;color:var(--ok)}
.card2{background:var(--paper);border:1px solid var(--hair);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.card2 h3{margin:0 0 4px;font-size:15px}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-weight:500;color:var(--ink3);font-size:11px;padding:6px 8px;border-bottom:1px solid var(--hair)}
.tbl td{padding:7px 8px;border-bottom:1px solid var(--hair);white-space:nowrap}
.tbl td.t{white-space:normal;max-width:320px}
.tbl a{color:var(--accent);text-decoration:none}
.tw{overflow-x:auto;background:var(--paper);border:1px solid var(--hair);border-radius:10px}
.bars{display:flex;align-items:flex-end;gap:3px;height:60px;margin:8px 0 2px}
.bars div{flex:1;background:var(--accent);border-radius:2px 2px 0 0;min-height:2px;opacity:.85}
.bars div.zero{background:var(--hair)}
.mini{font-size:11px;color:var(--ink3);display:flex;justify-content:space-between}
.share{font-family:"JetBrains Mono",ui-monospace,monospace;font-size:12px;background:var(--surface);border:1px solid var(--hair);border-radius:6px;padding:4px 8px;display:inline-block}
.up{color:var(--ok)} .dn{color:var(--crit)}
</style></head><body>
<div class="top"><div class="modes"><button id="m-studio" class="on" onclick="setMode('studio')">Studio</button><button id="m-creators" onclick="setMode('creators')">Creators</button></div><h1 id="title">Clips</h1><input class="search" id="q" placeholder="Search by feeling or scene (struggle, smug, thanos...)"></div>
<main id="main"></main>
<nav id="nav-studio">
<button id="nb-clips" class="on" onclick="go('clips')"><span>Clips</span></button>
<button id="nb-reels" onclick="go('reels')"><span>Reels</span></button>
<button id="nb-drops" onclick="go('drops')"><span>Drops</span></button>
</nav>
<nav id="nav-creators" hidden>
<button id="nb-dash" class="on" onclick="go('dash')"><span>Dashboard</span></button>
<button id="nb-posts" onclick="go('posts')"><span>Posts</span></button>
<button id="nb-links" onclick="go('links')"><span>Links</span></button>
</nav>
<script>
const DATA = __DATA__;
const ROWS = DATA.rows;
let mode='studio', view='clips', q='';
const fmt=n=>n==null?'':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e4?(n/1e3).toFixed(1)+'K':String(n);
const SITE=location.hostname.endsWith('offerloop.ai')?'https://go.offerloop.ai':location.origin;
const goUrl=code=>SITE.startsWith('https://go.')?`${SITE}/${code}`:`${SITE}/go/${code}`;
function delta(p,field){ // change over the last day from the post's history snapshots
  const idx={views:1,likes:2,comments:3,shares:4}[field]; const h=p.history||[];
  if(h.length<2) return null; return (h[h.length-1][idx]||0)-(h[h.length-2][idx]||0);
}
function sum(list,f){return list.reduce((a,p)=>a+(p[f]||0),0)}
function dayKey(iso){return (iso||'').slice(0,10)}
function lastDays(n){const out=[];const d=new Date();for(let i=n-1;i>=0;i--){const x=new Date(d);x.setDate(d.getDate()-i);out.push(x.toISOString().slice(0,10));}return out}
function bars(counts,days){const mx=Math.max(1,...days.map(d=>counts[d]||0));return `<div class="bars">${days.map(d=>`<div class="${counts[d]?'':'zero'}" style="height:${Math.round((counts[d]||0)/mx*100)}%" title="${d}: ${counts[d]||0}"></div>`).join('')}</div><div class="mini"><span>${days[0]}</span><span>${days[days.length-1]}</span></div>`}
function clicksByDay(code){const c={};DATA.clicks.filter(x=>!code||x.code===code).forEach(x=>{const k=dayKey(x.when);c[k]=(c[k]||0)+1});return c}
function creatorsView(){
  const posts=DATA.posts.filter(p=>match({title:p.title,description:p.creator,sources:p.platform,tags:''}));
  const creators=DATA.creators.filter(c=>c.active);
  const days=lastDays(14); const clicks=clicksByDay(null); const c7=days.slice(-7).reduce((a,d)=>a+(clicks[d]||0),0);
  const dv=posts.reduce((a,p)=>a+(delta(p,'views')||0),0);
  if(view==='dash'){
    return `<div class="tiles">
      <div class="tile-stat"><div class="n">${fmt(sum(posts,'views'))}</div><div class="l">views</div>${dv?`<div class="d">+${fmt(dv)} last day</div>`:''}</div>
      <div class="tile-stat"><div class="n">${fmt(sum(posts,'likes'))}</div><div class="l">likes</div></div>
      <div class="tile-stat"><div class="n">${fmt(sum(posts,'shares'))}</div><div class="l">shares</div></div>
      <div class="tile-stat"><div class="n">${fmt(sum(posts,'comments'))}</div><div class="l">comments</div></div>
      <div class="tile-stat"><div class="n">${DATA.clicks.length}</div><div class="l">link clicks</div>${c7?`<div class="d">${c7} last 7 days</div>`:''}</div>
      <div class="tile-stat"><div class="n">${posts.length}</div><div class="l">posts tracked</div></div>
    </div>
    <div class="card2"><h3>Link clicks, last 14 days</h3>${bars(clicks,days)}</div>
    ${creators.map(c=>{const ps=posts.filter(p=>p.creator===c.name); const top=ps.slice().sort((a,b)=>(b.views||0)-(a.views||0))[0];
      return `<div class="card2"><h3>${esc(c.name)} <span class="tmeta" style="display:inline">${c.tiktok?'@'+esc(c.tiktok)+' on TikTok':''} ${c.instagram?'· @'+esc(c.instagram)+' on Instagram':''} · ${esc(c.track||'all')} posts</span></h3>
        <div class="tmeta">${ps.length} posts · ${fmt(sum(ps,'views'))} views · ${fmt(sum(ps,'likes'))} likes · ${fmt(sum(ps,'shares'))} shares${c.code?` · link clicks: ${DATA.clicks.filter(x=>x.code.startsWith(c.code)).length}`:''}</div>
        ${top?`<div class="tmeta">top post: <a href="${top.url}" target="_blank">${esc(top.title.slice(0,70))}</a> · ${fmt(top.views)} views</div>`:'<div class="tmeta">no posts yet'+(c.tiktok?'':' (add the TikTok handle in Notion)')+'</div>'}
      </div>`}).join('')||'<div class="empty">No creators yet. Add one in the Creators database in Notion.</div>'}`;
  }
  if(view==='posts'){
    const list=posts.slice().sort((a,b)=>(b.posted||'').localeCompare(a.posted||''));
    return `<div class="count">${list.length} posts</div><div class="tw"><table class="tbl"><tr><th>Creator</th><th>Post</th><th>Posted</th><th>Views</th><th>Δ day</th><th>Likes</th><th>Comments</th><th>Shares</th><th>Checked</th></tr>
      ${list.map(p=>{const d=delta(p,'views');return `<tr><td>${esc(p.creator)}</td><td class="t"><a href="${p.url}" target="_blank">${esc(p.title.slice(0,80))}</a> <span class="pill t-none">${esc(p.platform)}</span></td><td>${p.posted}</td><td>${fmt(p.views)}</td><td class="${d>0?'up':d<0?'dn':''}">${d==null?'':(d>0?'+':'')+fmt(d)}</td><td>${fmt(p.likes)}</td><td>${fmt(p.comments)}</td><td>${fmt(p.shares)}</td><td>${p.checked}</td></tr>`}).join('')||'<tr><td colspan=9 class="empty">Nothing tracked yet.</td></tr>'}</table></div>`;
  }
  return DATA.links.map(l=>{const cs=DATA.clicks.filter(x=>x.code===l.code); const c={}; cs.forEach(x=>{c[x.device||'Other']=(c[x.device||'Other']||0)+1});
    const byday=clicksByDay(l.code); const d7=lastDays(7).reduce((a,d)=>a+(byday[d]||0),0);
    return `<div class="card2"><h3>${esc(l.code)} <span class="tmeta" style="display:inline">${esc(l.owner)} · ${esc(l.destination)}${l.active?'':' · inactive'}</span></h3>
      <div style="margin:6px 0"><span class="share">${goUrl(esc(l.code))}</span> <button class="chip" onclick="navigator.clipboard.writeText('${goUrl(esc(l.code))}')">copy</button></div>
      <div class="tmeta">${cs.length} clicks · ${d7} in the last 7 days · ${Object.entries(c).map(([k,v])=>k+' '+v).join(', ')||'no clicks yet'}</div>
      <div class="tmeta">sends to <a href="${l.target}" target="_blank">${esc(l.target.slice(0,80))}</a></div>
      ${l.notes?`<div class="tmeta">${esc(l.notes)}</div>`:''}
      ${bars(byday,lastDays(14))}</div>`}).join('')||'<div class="empty">No links yet. Add a row to the Studio Links database in Notion.</div>';
}
function setMode(m){mode=m;view=m==='studio'?'clips':'dash';['studio','creators'].forEach(x=>document.getElementById('m-'+x).className=(x===m?'on':''));document.getElementById('nav-studio').hidden=m!=='studio';document.getElementById('nav-creators').hidden=m!=='creators';render();window.scrollTo(0,0)}
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
  document.getElementById('title').textContent={clips:'Clips',reels:'Reels',drops:'Drops',dash:'Dashboard',posts:'Posts',links:'Links'}[view];
  ['clips','reels','drops','dash','posts','links'].forEach(v=>{const b=document.getElementById('nb-'+v); if(b) b.className=(v===view?'on':'')});
  document.getElementById('q').style.display=(view==='drops'||view==='dash'||view==='links')?'none':'';
  document.getElementById('q').placeholder=mode==='creators'?'Search posts':'Search by feeling or scene (struggle, smug, thanos...)';
  const m=document.getElementById('main');
  if(mode==='creators'){ m.innerHTML=creatorsView(); return; }
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
if(location.hash==='#creators') setMode('creators');
const q0=new URLSearchParams(location.search).get('q'); if(q0){ setQ(q0); }
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
        parts0 = self.path.split("?")[0].strip("/").split("/")
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host.startswith("go.") and parts0 and parts0[0] and parts0[0] != "go":
            parts0 = ["go"] + parts0          # go.offerloop.ai/josiah == studio.offerloop.ai/go/josiah
        if host.startswith("go.") and (not parts0 or not parts0[0]):
            self.send_response(302); self.send_header("Location", "https://offerloop.ai/"); self.end_headers(); return
        if parts0[0] == "go" and len(parts0) >= 2:      # ambassador links: public, no password
            code = parts0[1].lower()
            dest = parts0[2].lower() if len(parts0) > 2 else ""
            links = [l for l in creators_data()["links"] if l["active"] and l["code"].lower() == (f"{code}-{dest}" if dest else code)]
            if not links:
                links = [l for l in creators_data()["links"] if l["active"] and l["code"].lower() == code]
            if not links or not links[0]["target"]:
                self.send_response(404); self.send_header("Content-Type", "text/plain"); self.end_headers()
                self.wfile.write(b"unknown link"); return
            link = links[0]
            threading.Thread(target=log_click, args=(link["code"], link["destination"] or link["target"],
                             self.headers.get("User-Agent"), self.headers.get("Referer")), daemon=True).start()
            self.send_response(302); self.send_header("Location", link["target"]); self.send_header("Cache-Control", "no-store")
            self.end_headers(); return
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
            data = {"rows": rows(), **creators_data()}
            page = PAGE.replace("__DATA__", json.dumps(data))
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
    # threaded: one slow Notion call must never block the health check or other visitors
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
