import json, os, re, subprocess, threading
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_file
import requests

app = Flask(__name__)
MOVIE_DIR = Path(os.getenv("MOVIE_DIR", "/movies"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")); DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm"}
lock = threading.Lock()

def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError): return {"movies": {}, "api_lookups": 0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def has_subtitle_sidecar(p):
    return any(x.exists() for x in p.parent.glob(p.stem + ".*") if x.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt", ".sub"})

def embedded_subtitles(p):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv=p=0", str(p)], text=True, timeout=30)
        return bool(out.strip())
    except Exception: return False

def scan():
    state = load_state(); found = {}
    if MOVIE_DIR.exists():
        for p in sorted(MOVIE_DIR.rglob("*")):
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(MOVIE_DIR).parts) and not p.name.startswith("._") and p.suffix.lower() in VIDEO_EXTS:
                key = str(p)
                old = state["movies"].get(key, {})
                found[key] = {"path": key, "title": p.stem, "has_subtitles": has_subtitle_sidecar(p) or embedded_subtitles(p), **old}
    state["movies"] = found; save_state(state); return state

def movie_or_404(path):
    p = Path(path).resolve(); root = MOVIE_DIR.resolve()
    if root not in p.parents or not p.is_file(): return None
    return p

def parse_srt(text):
    cues=[]
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines=block.splitlines()
        if len(lines)<3 or "-->" not in lines[1]: continue
        a,b=[x.strip().split(",") for x in lines[1].split("-->")]
        def sec(x):
            h,m,s=x.split(":"); return int(h)*3600+int(m)*60+float(s)
        cues.append({"start":sec(a)+float(b[0])/1000 if False else sec(a), "end":sec(b), "text":"\n".join(lines[2:])})
    return cues

def shift_srt(text, offset):
    def fmt(v):
        v=max(0,v); ms=round(v*1000); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    out=[]
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines=block.splitlines()
        if len(lines)>=3 and "-->" in lines[1]:
            a,b=[x.strip() for x in lines[1].split("-->")]
            def sec(x):
                h,m,rest=x.replace(",", ".").split(":"); return int(h)*3600+int(m)*60+float(rest)
            lines[1]=fmt(sec(a)+offset)+" --> "+fmt(sec(b)+offset)
        out.append("\n".join(lines))
    return "\n\n".join(out)+"\n"

def analyze(path):
    # ffmpeg's silencedetect gives a portable, cached voice-activity approximation.
    cmd=["ffmpeg","-hide_banner","-i",str(path),"-af","silencedetect=noise=-35dB:d=0.25","-f","null","-"]
    proc=subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=3600)
    silences=[]
    for a,b in re.findall(r"silence_start: ([\d.]+).*?silence_end: ([\d.]+)", proc.stderr, re.S): silences.append([float(a),float(b)])
    return {"silences":silences, "cached":True}

@app.get("/")
def index(): return render_template("index.html")

@app.get("/api/movies")
def movies():
    state=scan(); return jsonify({"movies":[m for m in state["movies"].values() if not m.get("has_subtitles")], "lookups":state.get("api_lookups",0), "limit":int(os.getenv("API_LOOKUP_LIMIT","100"))})

@app.post("/api/analyze")
def api_analyze():
    p=movie_or_404(request.json.get("path"));
    if not p: return jsonify(error="Invalid movie"),400
    s=load_state(); s["movies"][str(p)]["vad"]=analyze(p); save_state(s); return jsonify(ok=True)

@app.post("/api/search")
def api_search():
    path=request.json.get("path"); p=movie_or_404(path); s=load_state(); m=s["movies"].get(str(p))
    if not p or not m or not m.get("vad",{}).get("cached"): return jsonify(error="Analyze the movie first"),400
    limit=int(os.getenv("API_LOOKUP_LIMIT","100"));
    if s.get("api_lookups",0)>=limit: return jsonify(error=f"Lookup limit ({limit}) reached"),429
    headers={"Api-Key":os.getenv("OPENSUBTITLES_CONSUMER_KEY",os.getenv("OPENSUBTITLES_API_KEY","")),"User-Agent":os.getenv("OPENSUBTITLES_USER_AGENT","subtitle-alignment-local")}
    r=requests.get("https://api.opensubtitles.com/api/v1/subtitles", params={"query":m["title"],"languages":"en","order_by":"download_count","order_direction":"desc","page":1}, headers=headers, timeout=30)
    s["api_lookups"]+=1; save_state(s)
    if not r.ok: return jsonify(error=f"OpenSubtitles returned {r.status_code}: {r.text[:200]}"),502
    data=r.json().get("data",[]); data.sort(key=lambda x:x["attributes"].get("download_count",0) or 0,reverse=True); m["candidates"]=[{"id":x["id"],"name":x["attributes"].get("release",x["attributes"].get("feature_details",{}).get("movie_name","candidate")),"download_id":x["attributes"].get("files",[{}])[0].get("file_id"),"download_count":x["attributes"].get("download_count",0) or 0} for x in data[:10]]; save_state(s); return jsonify(candidates=m["candidates"],lookups=s["api_lookups"])

def download_candidate(p, m, candidate):
    h={"Api-Key":os.getenv("OPENSUBTITLES_CONSUMER_KEY",os.getenv("OPENSUBTITLES_API_KEY","")),"User-Agent":os.getenv("OPENSUBTITLES_USER_AGENT","subtitle-alignment-local"),"Content-Type":"application/json"}
    r=requests.post("https://api.opensubtitles.com/api/v1/download",json={"file_id":candidate.get("download_id")},headers=h,timeout=30)
    if not r.ok: return False, f"Download request failed: {r.status_code}"
    link=r.json().get("link")
    if not link: return False, "OpenSubtitles did not return a download link"
    response=requests.get(link,timeout=60)
    if not response.ok: return False, f"Subtitle download failed: {response.status_code}"
    tmp=DATA_DIR/(p.stem+".srt"); tmp.write_bytes(response.content); m["working_srt"]=str(tmp); m["offset"]=0
    return True, None

@app.post("/api/download")
def api_download():
    path=request.json.get("path"); p=movie_or_404(path); s=load_state(); m=s["movies"].get(str(p)); cand=(m or {}).get("candidates",[])
    if not p or not cand: return jsonify(error="Search for candidates first"),400
    candidate=next((x for x in cand if str(x.get("download_id"))==str(request.json.get("file_id"))),cand[0])
    ok,error=download_candidate(p,m,candidate)
    if not ok: return jsonify(error=error),502
    save_state(s); return jsonify(ok=True)

@app.get("/api/video")
def video():
    p=movie_or_404(request.args.get("path")); return send_file(p) if p else ("",404)

@app.get("/api/subtitles")
def subtitles():
    p=movie_or_404(request.args.get("path")); s=load_state(); m=s["movies"].get(str(p)) if p else None
    if not m or not m.get("working_srt"): return ("",404)
    offset=float(request.args.get("offset",m.get("offset",0))); out=["WEBVTT",""]
    for i,block in enumerate(re.split(r"\n\s*\n",Path(m["working_srt"]).read_text(errors="replace").replace("\r","").strip()),1):
        parts=block.splitlines()
        if len(parts)<3 or "-->" not in parts[1]: continue
        def stamp(x):
            h,mm,rest=x.strip().replace(",",".").split(":"); total=max(0,int(h)*3600+int(mm)*60+float(rest)+offset); return f"{int(total//3600):02}:{int(total%3600//60):02}:{total%60:06.3f}"
        a,b=[x.strip() for x in parts[1].split("-->")]; out += [str(i),stamp(a)+" --> "+stamp(b),*parts[2:],""]
    return app.response_class("\n".join(out),mimetype="text/vtt")

@app.post("/api/decision")
def decision():
    data=request.json; p=movie_or_404(data.get("path")); s=load_state(); m=s["movies"].get(str(p))
    if not p or not m: return jsonify(error="Invalid movie"),400
    if data.get("action")=="approve":
        try:
            final=p.with_suffix(".eng.srt"); final.write_text(shift_srt(Path(m["working_srt"]).read_text(),float(data.get("offset",m.get("offset",0))))); m["has_subtitles"]=True
        except OSError as e:
            return jsonify(error=f"Could not save subtitle beside movie: {e}"),500
        save_state(s); return jsonify(ok=True)
    candidates=m.get("candidates",[])
    if candidates: candidates.pop(0)
    if not candidates: save_state(s); return jsonify(ok=False,error="No more ranked subtitle candidates are available"),404
    m["candidates"]=candidates; ok,error=download_candidate(p,m,candidates[0])
    if not ok: save_state(s); return jsonify(ok=False,error=error),502
    save_state(s); return jsonify(ok=True,candidate=candidates[0].get("name","next candidate"))

if __name__=="__main__": app.run(host="0.0.0.0",port=8085)

