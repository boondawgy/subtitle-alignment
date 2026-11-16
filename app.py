import json, os, re, subprocess, threading, tempfile, hashlib, io, zipfile, shutil
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from datetime import datetime, timezone
import math
from pathlib import Path
from urllib.parse import urlsplit, urljoin
from flask import Flask, jsonify, render_template, request, send_file
import requests

app = Flask(__name__)
MOVIE_DIR = Path(os.getenv("MOVIE_DIR", "/movies"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")); DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
ALIGNMENT_REPORT = Path(os.getenv("ALIGNMENT_REPORT", "/alignment-analysis/report.json"))
_track_hash_cache = {}
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm"}
lock = threading.RLock()
scan_started = False
discovery_started = False
startup_checked = False
scan_progress = {"phase": "idle", "found": 0, "checked": 0, "current": None}

def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError): return {"movies": {}, "api_lookups": 0}

def save_state(s):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=DATA_DIR, delete=False) as f:
        json.dump(s, f, indent=2)
        name = f.name
    os.replace(name, STATE_FILE)

def update_movie(path, updates, **state_updates):
    with lock:
        state = load_state()
        movie = state.get("movies", {}).get(str(path))
        if movie is not None:
            movie.update(updates)
        state.update(state_updates)
        save_state(state)

class OpenSubtitlesError(Exception):
    pass

_auth_lock = threading.RLock()
_auth_token = None
_auth_base = "https://api.opensubtitles.com/api/v1"
_auth_blocked = False

def opensubtitles_credentials():
    username = os.getenv("OPENSUBTITLES_USERNAME", "").strip()
    password = os.getenv("OPENSUBTITLES_PASSWORD", "")
    if bool(username) != bool(password):
        raise OpenSubtitlesError("Set both OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD in .env, then restart the app.")
    return username, password

def opensubtitles_identity():
    username = os.getenv("OPENSUBTITLES_USERNAME", "").strip()
    return "account:" + hashlib.sha256(username.casefold().encode()).hexdigest() if username else "anonymous"

def opensubtitles_base_url(value):
    if not value:
        return "https://api.opensubtitles.com/api/v1"
    parsed = urlsplit(value if "://" in value else "https://" + value)
    if parsed.scheme != "https" or parsed.netloc not in {"api.opensubtitles.com", "vip-api.opensubtitles.com"} or parsed.path not in ("", "/", "/api/v1", "/api/v1/") or parsed.query or parsed.fragment:
        raise OpenSubtitlesError("OpenSubtitles returned an unsupported API host.")
    return "https://" + parsed.netloc + "/api/v1"

def opensubtitles_headers(token=None):
    headers = {"Api-Key": os.getenv("OPENSUBTITLES_CONSUMER_KEY", os.getenv("OPENSUBTITLES_API_KEY", "")),
               "User-Agent": os.getenv("OPENSUBTITLES_USER_AGENT", "subtitle-alignment-local")}
    if token: headers["Authorization"] = "Bearer " + token
    return headers

def opensubtitles_token():
    global _auth_token, _auth_base, _auth_blocked
    username, password = opensubtitles_credentials()
    if not username: return None, "https://api.opensubtitles.com/api/v1"
    with _auth_lock:
        if _auth_blocked:
            raise OpenSubtitlesError("OpenSubtitles login was rejected. Check the username, password and API key, then restart the app.")
        if _auth_token: return _auth_token, _auth_base
        try:
            response = requests.post("https://api.opensubtitles.com/api/v1/login", json={"username": username, "password": password}, headers=opensubtitles_headers(), timeout=30, allow_redirects=False)
        except Exception as exc:
            raise OpenSubtitlesError("Could not reach OpenSubtitles to sign in.") from exc
        if response.status_code == 401:
            _auth_blocked = True
            raise OpenSubtitlesError("OpenSubtitles login was rejected. Check the username, password and API key, then restart the app.")
        if not response.ok:
            raise OpenSubtitlesError(f"OpenSubtitles login failed (HTTP {response.status_code}).")
        try:
            body = response.json(); token = body.get("token")
            if not isinstance(token, str) or not token: raise ValueError("missing token")
            base = opensubtitles_base_url(body.get("base_url"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise OpenSubtitlesError("OpenSubtitles returned an invalid login response.") from exc
        _auth_token, _auth_base = token, base
        return token, base

def opensubtitles_request(method, endpoint, **kwargs):
    global _auth_token, _auth_blocked
    token, base = opensubtitles_token()
    for attempt in range(2):
        url = base + endpoint
        request_kwargs = dict(kwargs)
        for redirects in range(4):
            try:
                response = getattr(requests, method)(url, headers=opensubtitles_headers(token), timeout=30, allow_redirects=False, **request_kwargs)
            except Exception as exc:
                raise OpenSubtitlesError("Could not reach OpenSubtitles. Please try again.") from exc
            status = getattr(response, "status_code", 200)
            if method.lower() == "get" and status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location or redirects == 3:
                    raise OpenSubtitlesError("OpenSubtitles returned an invalid search redirect.")
                target = urljoin(url, location)
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.netloc != urlsplit(base).netloc or not parsed.path.startswith("/api/v1/") or parsed.fragment:
                    raise OpenSubtitlesError("OpenSubtitles returned an untrusted search redirect.")
                url = target
                request_kwargs.pop("params", None)
                continue
            if 300 <= status < 400:
                raise OpenSubtitlesError("OpenSubtitles returned an unexpected redirect.")
            break
        if status != 401 or not token:
            return response
        with _auth_lock:
            if attempt == 1:
                _auth_token = None
                _auth_blocked = True
                raise OpenSubtitlesError("OpenSubtitles rejected the account token. Check your account and API key, then restart the app.")
            if _auth_token == token: _auth_token = None
        token, base = opensubtitles_token()
    return response

def record_api_response(response, download=False):
    """Persist the last reported values, including for failed downloads."""
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        state = load_state()
        headers = response.headers
        remaining = headers.get("RateLimit-Remaining") or headers.get("X-RateLimit-Remaining-Second")
        limit = headers.get("RateLimit-Limit") or headers.get("X-RateLimit-Limit-Second")
        if remaining is not None:
            state.update(api_rate_remaining=remaining, api_rate_limit=limit,
                         api_rate_reset=headers.get("RateLimit-Reset"), api_rate_updated_at=now)
        if download:
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("remaining") is not None:
                    state.update(downloads_remaining=body["remaining"], downloads_updated_at=now, downloads_identity=opensubtitles_identity())
            except (ValueError, TypeError):
                pass
        else:
            state["api_lookups"] = state.get("api_lookups", 0) + 1
        save_state(state)

def load_alignment_results():
    try:
        report = json.loads(ALIGNMENT_REPORT.read_text(encoding="utf-8"))
        return {m["movie_path"]: m.get("tracks", []) for m in report.get("movies", []) if m.get("movie_path")}
    except (OSError, ValueError, TypeError):
        return {}

def track_digest(path):
    try:
        p = Path(path); stat = p.stat(); key = (str(p), stat.st_size, stat.st_mtime_ns)
        if key not in _track_hash_cache:
            _track_hash_cache.clear() if len(_track_hash_cache) > 1024 else None
            _track_hash_cache[key] = hashlib.sha256(p.read_bytes()).hexdigest()
        return _track_hash_cache[key]
    except OSError:
        return None

def available_tracks(movie, analysis):
    history = movie.get("track_history") or []
    if not history and movie.get("working_srt") and movie.get("current_candidate_id") is not None:
        history = [{"id":movie["current_candidate_id"], "name":movie.get("current_candidate_name", "Downloaded subtitle"), "file":movie["working_srt"]}]
    result=[]
    rejected=set(map(str, movie.get("rejected_ids", [])))
    for entry in history:
        if not entry.get("file") or not Path(entry["file"]).is_file(): continue
        digest=track_digest(entry["file"]) if analysis else None
        match=next((t for t in analysis if str(t.get("track_id"))==str(entry.get("id")) and t.get("subtitle_sha256") and t.get("subtitle_sha256")==digest and t.get("status")=="analyzed"), None)
        score=match.get("fit_score") if match else None
        offset=match.get("recommended_offset_seconds") if match else None
        if entry.get("recommended_offset_seconds") is not None:
            offset=entry["recommended_offset_seconds"]
        if not isinstance(score,(int,float)) or isinstance(score,bool) or not math.isfinite(score) or not 0 <= score <= 100: score=None
        if not isinstance(offset,(int,float)) or isinstance(offset,bool) or not math.isfinite(offset): offset=None
        result.append({"id":entry["id"], "name":entry.get("name", "Downloaded subtitle"), "fit_score":score, "recommended_offset_seconds":offset, "rejected":str(entry["id"]) in rejected})
    return result

def has_subtitle_sidecar(p):
    return any(x.is_file() and x.name.lower().startswith(p.stem.lower() + ".") and x.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt", ".sub"} for x in p.parent.iterdir())

def embedded_subtitles(p):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv=p=0", str(p)], text=True, timeout=30)
    return bool(out.strip())

def discover_library():
    global discovery_started
    def check_movie(key):
        with lock:
            scan_progress["current"] = Path(key).name
        try:
            result = embedded_subtitles(Path(key)); error = None
        except (OSError, subprocess.SubprocessError) as exc:
            result = None; error = str(exc)
        with lock:
            state = load_state(); movie = state.get("movies", {}).get(key)
            if movie:
                movie.update(has_embedded=result, embedded_checked=result is not None,
                             has_subtitles=movie.get("has_sidecar", False) or result is True,
                             scan_error=error)
                save_state(state)
            scan_progress["checked"] += 1

    try:
        if not MOVIE_DIR.is_dir():
            raise OSError(f"Movie directory is unavailable: {MOVIE_DIR}")
        found = set()
        # A small bounded pool lets probes run during discovery without overwhelming SMB.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for p in MOVIE_DIR.rglob("*"):
                if any(part.startswith(".") for part in p.relative_to(MOVIE_DIR).parts) or p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
                    continue
                key = str(p.resolve()); found.add(key)
                sidecar = has_subtitle_sidecar(p)
                stat = p.stat()
                with lock:
                    state = load_state(); old = state.setdefault("movies", {}).get(key, {})
                    embedded = old.get("has_embedded")
                    # A cached probe is only valid for the same file. This also
                    # rechecks legacy entries that have no stored fingerprint.
                    if (old.get("movie_size") != stat.st_size or
                            old.get("movie_mtime_ns") != stat.st_mtime_ns):
                        embedded = None
                    checked = embedded is not None
                    old.update(path=key, title=p.stem, has_sidecar=sidecar,
                               movie_size=stat.st_size, movie_mtime_ns=stat.st_mtime_ns,
                               has_embedded=embedded, embedded_checked=checked,
                               has_subtitles=sidecar or embedded is True)
                    state["movies"][key] = old; save_state(state)
                    scan_progress["found"] += 1
                if not checked and not sidecar:
                    pool.submit(check_movie, key)
            with lock:
                state = load_state()
                state["movies"] = {k: v for k, v in state.get("movies", {}).items() if k in found}
                state.pop("scan_error", None); save_state(state)
                scan_progress["phase"] = "checking"
        with lock:
            scan_progress["phase"] = "complete"
            scan_progress["current"] = None
    except Exception as exc:
        app.logger.exception("Library scan failed")
        with lock:
            state = load_state(); state["scan_error"] = str(exc); save_state(state)
            scan_progress["phase"] = "error"
    finally:
        with lock: discovery_started = False

def start_scan():
    global discovery_started
    with lock:
        if discovery_started: return False
        discovery_started = True
        scan_progress.update(phase="discovering", found=0, checked=0, current=None)
        threading.Thread(target=discover_library, daemon=True).start()
        return True

def movie_or_404(path):
    if not path: return None
    p = Path(path).resolve(); root = MOVIE_DIR.resolve()
    if root not in p.parents or not p.is_file(): return None
    return p

def parse_srt(text):
    cues = []
    pattern = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")
    def seconds(match):
        h, m, sec, ms = match.groups()
        return int(h)*3600 + int(m)*60 + int(sec) + int(ms.ljust(3, "0"))/1000
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None: continue
        stamps = pattern.findall(lines[timing])
        matches = list(pattern.finditer(lines[timing]))
        if len(matches) < 2: continue
        start, end = map(seconds, matches[:2])
        if end > start:
            cues.append({"start": start, "end": end, "text": "\n".join(lines[timing+1:]).replace("[br]", "\n")})
    return cues

def shift_srt(text, offset):
    def fmt(v):
        v=max(0,v); ms=round(v*1000); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    out=[]
    for i, cue in enumerate(parse_srt(text), 1):
        start, end = max(0, cue["start"] + offset), max(0, cue["end"] + offset)
        if end <= start: continue
        out.append(f"{i}\n{fmt(start)} --> {fmt(end)}\n{cue['text']}")
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
    global startup_checked
    with lock:
        state = load_state()
        if not startup_checked:
            startup_checked = True
            incomplete = any(not m.get("embedded_checked") and not m.get("has_sidecar") and not m.get("scan_error") for m in state.get("movies", {}).values())
            if not STATE_FILE.exists() or incomplete:
                start_scan()
        result = []
        analyses = load_alignment_results()
        for movie in state.get("movies", {}).values():
            m = dict(movie)
            tracks=available_tracks(m, analyses.get(m.get("path"), []))
            scores=[t["fit_score"] for t in tracks if t["fit_score"] is not None and not t["rejected"]]
            m["track_count"] = len(tracks)
            m["best_fit_score"] = max(scores) if scores else None
            m["scan_status"] = "ready" if m.get("embedded_checked") or m.get("has_sidecar") else ("error" if m.get("scan_error") or not discovery_started else "analyzing")
            if m["scan_status"] == "error" and not m.get("scan_error"):
                m["scan_error"] = "Scan interrupted; refresh the library to retry"
            result.append(m)
        return jsonify(movies=result, scanning=discovery_started, scan_progress=dict(scan_progress), scan_error=state.get("scan_error"),
                       lookups=state.get("api_lookups", 0), rate_remaining=state.get("api_rate_remaining"),
                       rate_limit=state.get("api_rate_limit"), rate_reset=state.get("api_rate_reset"),
                       downloads_remaining=state.get("downloads_remaining") if state.get("downloads_identity", "anonymous") == opensubtitles_identity() else None,
                       rate_updated_at=state.get("api_rate_updated_at"), downloads_updated_at=state.get("downloads_updated_at") if state.get("downloads_identity", "anonymous") == opensubtitles_identity() else None,
                       auth_status="account rejected" if _auth_blocked else ("account signed in" if _auth_token else ("account configured" if os.getenv("OPENSUBTITLES_USERNAME", "").strip() else "anonymous")))

@app.post("/api/refresh")
def refresh_library():
    return jsonify(ok=True, started=start_scan())

@app.post("/api/pass-movie")
def pass_movie():
    data=request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
        return jsonify(error="Expected a movie path and a true or false pass value"), 400
    p=movie_or_404(data.get("path"))
    if not p: return jsonify(error="Invalid movie"), 400
    with lock:
        state=load_state(); movie=state.get("movies", {}).get(str(p))
        if not movie: return jsonify(error="Movie not found in library"), 404
        movie["passed"]=data["passed"]
        save_state(state)
    return jsonify(ok=True, passed=data["passed"])

@app.post("/api/movie-identifier")
def set_movie_identifier():
    data=request.get_json(silent=True)
    if not isinstance(data, dict): return jsonify(error="Invalid request"), 400
    p=movie_or_404(data.get("path")); raw=data.get("imdb_id")
    match=re.fullmatch(r"tt(\d{5,12})", str(raw or ""))
    if not p or not match: return jsonify(error="Invalid movie or IMDb ID"), 400
    with lock:
        state=load_state();movie=state.get("movies",{}).get(str(p))
        if not movie: return jsonify(error="Movie not found in library"), 404
        movie["imdb_id"]='tt'+match.group(1);save_state(state)
    return jsonify(ok=True)

@app.post("/api/analyze")
def api_analyze():
    p=movie_or_404(request.json.get("path"));
    if not p: return jsonify(error="Invalid movie"),400
    s=load_state(); s["movies"][str(p)]["vad"]=analyze(p); save_state(s); return jsonify(ok=True)

def title_tokens(value):
    return re.findall(r"[a-z0-9]+", (value or "").lower())

def candidate_rank(item, title, year):
    """Reject results whose identified movie does not match the requested title."""
    attrs = item.get("attributes") or {}
    details = attrs.get("feature_details") or {}
    kind = (details.get("feature_type") or attrs.get("feature_type") or "").lower()
    if kind and kind != "movie":
        return None
    result_year = details.get("year")
    if year and result_year and str(result_year) != str(year):
        return None
    wanted = title_tokens(title)
    if not wanted:
        return None
    named = details.get("title") or details.get("movie_name")
    files = attrs.get("files") or []
    release = attrs.get("release") or (files[0].get("file_name", "") if files else "")
    actual = title_tokens(named or release)
    if not actual:
        return None
    # Metadata names should match closely. For release names, find the full title
    # as a contiguous sequence because release tags often follow the movie title.
    if named:
        similarity = SequenceMatcher(None, " ".join(wanted), " ".join(actual)).ratio()
        if similarity < 0.85:
            return None
    else:
        if not any(actual[i:i+len(wanted)] == wanted for i in range(len(actual)-len(wanted)+1)):
            return None
        similarity = 1.0
    movie_release = set(title_tokens(title))
    release_overlap = len(movie_release & set(title_tokens(release)))
    return (similarity, release_overlap, attrs.get("download_count", 0) or 0)

@app.post("/api/search")
def api_search():
    path=request.json.get("path"); p=movie_or_404(path); s=load_state(); m=s.get("movies",{}).get(str(p))
    if not p or not m: return jsonify(error="Invalid movie"),400
    try:
        page = int(request.json.get("page", 1))
        if page < 1: raise ValueError()
    except (ValueError, TypeError):
        return jsonify(error="Invalid search page"), 400
    year_match=re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)",m["title"]); params={"query":re.sub(r"[. _-]+", " ", m["title"][:year_match.start()].strip(" ._-(") if year_match else m["title"]),"languages":"en","order_by":"download_count","order_direction":"desc","page":page}
    if year_match: params["year"]=year_match.group(1)
    imdb_id = request.json.get("imdb_id") or m.get("imdb_id")
    if imdb_id:
        imdb_match = re.fullmatch(r"(?:tt)?(\d{1,12})", str(imdb_id))
        if not imdb_match: return jsonify(error="Invalid IMDb ID"), 400
        imdb_id = int(imdb_match.group(1))
        params.pop("query", None)
        params.pop("year", None)
        params.update(imdb_id=imdb_id, type="movie")
    try:
        r=opensubtitles_request("get", "/subtitles", params=params)
    except OpenSubtitlesError as exc:
        return jsonify(error=str(exc)), 502
    record_api_response(r)
    if not r.ok: return jsonify(error=f"OpenSubtitles returned {r.status_code}: {r.text[:200]}"),502
    try:
        body=r.json()
        if not isinstance(body, dict) or not isinstance(body.get("data", []), list): raise ValueError("invalid search response")
        data=body.get("data", [])
    except (ValueError, TypeError):
        return jsonify(error="OpenSubtitles returned an invalid search response."), 502
    ranked=[]
    for item in data:
        if imdb_id:
            details = (item.get("attributes") or {}).get("feature_details") or {}
            try:
                if int(details.get("imdb_id", 0)) != imdb_id: continue
            except (ValueError, TypeError):
                continue
            title = details.get("title") or ""
            # An exact verified IMDb ID is authoritative when regional release years differ.
            score = candidate_rank(item, title, None)
        else:
            score=candidate_rank(item, params["query"], params.get("year"))
        if score is not None: ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    m["candidates"]=[]
    rejected=set(map(str, m.get("rejected_ids", [])))
    for _, x in ranked:
        attrs=x.get("attributes",{}); files=attrs.get("files",[])
        for file in files:
            if file.get("file_id") and str(file["file_id"]) not in rejected:
                m["candidates"].append({"id":x["id"],"name":file.get("file_name") or attrs.get("release") or "candidate","download_id":file["file_id"],"download_count":attrs.get("download_count",0) or 0})
    update_movie(p, {"candidates": m["candidates"]}); return jsonify(candidates=m["candidates"], lookups=load_state().get("api_lookups", 0), page=page, total_pages=body.get("total_pages"), total_count=body.get("total_count"), matched_count=len(ranked))

def preserve_legacy_track(p, movie):
    """Keep the one track retained by older versions, if it still exists."""
    history = movie.setdefault("track_history", [])
    current = movie.get("current_candidate_id")
    working = movie.get("working_srt")
    if current is None or not working or any(str(t["id"]) == str(current) for t in history):
        return
    source = Path(working)
    if not source.is_file():
        return
    key = hashlib.sha256((str(p) + "\0" + str(current)).encode()).hexdigest()
    target = DATA_DIR / ("track-" + key + ".srt")
    if source != target:
        shutil.copyfile(source, target)
    history.append({"id": current, "name": movie.get("current_candidate_name", "Downloaded subtitle"), "file": str(target)})
    movie["working_srt"] = str(target)

def select_track(movie, track_id):
    track = next((t for t in movie.get("track_history", []) if str(t.get("id")) == str(track_id)), None)
    if not track or not Path(track["file"]).is_file():
        return False
    movie.update(working_srt=track["file"], current_candidate_id=track["id"],
                 current_candidate_name=track["name"], offset=0)
    return True

def download_candidate(p, m, candidate):
    try:
        r=opensubtitles_request("post", "/download", json={"file_id":candidate.get("download_id")})
    except OpenSubtitlesError as exc:
        return False, str(exc)
    record_api_response(r, download=True)
    if not r.ok:
        try:
            body = r.json()
            message = body.get("message") or body.get("error") if isinstance(body, dict) else None
        except ValueError:
            message = None
        if r.status_code == 406:
            return False, f"OpenSubtitles declined the download (HTTP 406). {message or 'The download allowance may be exhausted; try again after it resets.'}"
        return False, f"Download request failed: {r.status_code}" + (f" — {message}" if message else "")
    link=r.json().get("link")
    if not link: return False, "OpenSubtitles did not return a download link"
    response=requests.get(link,timeout=60)
    if not response.ok: return False, f"Subtitle download failed: {response.status_code}"
    content=response.content
    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries=[x for x in archive.infolist() if x.filename.lower().endswith(".srt") and x.file_size <= 10_000_000]
                if not entries: return False, "Archive contains no usable SRT subtitle"
                content=archive.read(entries[0])
        if len(content)>10_000_000: return False, "Subtitle file is too large"
        try: subtitle=content.decode("utf-8-sig")
        except UnicodeDecodeError: subtitle=content.decode("cp1252")
        if not parse_srt(subtitle): return False, "Downloaded file is not a valid SRT subtitle"
    except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
        return False, f"Could not read subtitle: {exc}"
    preserve_legacy_track(p, m)
    candidate_id = candidate.get("download_id")
    key = hashlib.sha256((str(p) + "\0" + str(candidate_id)).encode()).hexdigest()
    tmp = DATA_DIR / ("track-" + key + ".srt")
    tmp.write_text(subtitle, encoding="utf-8")
    history = m.setdefault("track_history", [])
    if not any(str(t["id"]) == str(candidate_id) for t in history):
        history.append({"id": candidate_id, "name": candidate.get("name", "Unknown subtitle"), "file": str(tmp)})
    select_track(m, candidate_id)
    return True, None

@app.post("/api/download")
def api_download():
    path=request.json.get("path"); p=movie_or_404(path); s=load_state(); m=s.get("movies",{}).get(str(p)); cand=(m or {}).get("candidates",[])
    if not p or not cand: return jsonify(error="Search for candidates first"),400
    requested_id=request.json.get("file_id")
    candidate=next((x for x in cand if str(x.get("download_id"))==str(requested_id)), None) if requested_id is not None else cand[0]
    if candidate is None: return jsonify(error="Selected candidate is no longer available; search again."), 409
    ok,error=download_candidate(p,m,candidate)
    if not ok: return jsonify(error=error),502
    update_movie(p, {k:m[k] for k in ("working_srt", "current_candidate_id", "current_candidate_name", "offset", "track_history")}); return jsonify(ok=True)

# Keep one converted preview in a separate cache; original movies are never changed.
preview_jobs = {}
preview_lock = threading.Lock()
PREVIEW_DIR = DATA_DIR / "preview-cache"

def preview_key(path):
    stat = path.stat()
    return hashlib.sha256(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()

def audio_needs_conversion(path):
    result = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(path)], text=True, timeout=30).strip().lower()
    return result in {"ac3", "eac3", "dts", "truehd", "mlp"}

def build_preview(path, key):
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PREVIEW_DIR / (key + ".partial.mp4")
    target = PREVIEW_DIR / (key + ".mp4")
    try:
        # Evict only application-generated completed previews; never movie files.
        for old in PREVIEW_DIR.glob("*.mp4"):
            if old != temporary and old != target and not old.name.endswith(".partial.mp4"):
                old.unlink(missing_ok=True)
        required = path.stat().st_size * 2 + 256 * 1024 * 1024
        if shutil.disk_usage(PREVIEW_DIR).free < required:
            raise OSError("Not enough free disk space to prepare a compatible preview")
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-b:a", "160k", "-movflags", "+faststart", str(temporary)], capture_output=True, text=True, timeout=7200)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-500:] or "ffmpeg could not prepare the preview")
        os.replace(temporary, target)
        with preview_lock: preview_jobs[key] = {"status": "ready"}
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        with preview_lock: preview_jobs[key] = {"status": "error", "error": str(exc)}

@app.get("/api/preview-video")
def preview_video_status():
    path = movie_or_404(request.args.get("path"))
    if not path: return jsonify(error="Invalid movie"), 400
    try:
        if not audio_needs_conversion(path): return jsonify(status="ready", converted=False)
        key = preview_key(path)
        with preview_lock:
            if (PREVIEW_DIR / (key + ".mp4")).is_file():
                return jsonify(status="ready", converted=True)
            job = preview_jobs.get(key)
            if job is None:
                if any(x.get("status") == "preparing" for x in preview_jobs.values()):
                    return jsonify(status="error", error="Another compatible preview is being prepared; try again shortly"), 409
                job = preview_jobs[key] = {"status": "preparing"}
                threading.Thread(target=build_preview, args=(path, key), daemon=True).start()
            return jsonify(**job)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify(status="error", error=f"Could not inspect movie audio: {exc}"), 500

@app.get("/api/video")
def video():
    p=movie_or_404(request.args.get("path"))
    if not p: return ("",404)
    if request.args.get("compatible") == "1":
        cached=PREVIEW_DIR / (preview_key(p) + ".mp4")
        if not cached.is_file(): return ("",404)
        return send_file(cached, conditional=True, mimetype="video/mp4")
    return send_file(p, conditional=True)

@app.get("/api/subtitles")
def subtitles():
    p=movie_or_404(request.args.get("path")); s=load_state(); m=s["movies"].get(str(p)) if p else None
    if not m or not m.get("working_srt"): return ("",404)
    offset=float(request.args.get("offset",0)); out=["WEBVTT", ""]
    for cue in parse_srt(Path(m["working_srt"]).read_text(encoding="utf-8")):
        start=max(0, cue["start"]+offset); end=max(0, cue["end"]+offset)
        if end <= start: continue
        def stamp(total):
            ms=round(total*1000); h,ms=divmod(ms,3600000); minute,ms=divmod(ms,60000); second,ms=divmod(ms,1000)
            return f"{h:02}:{minute:02}:{second:02}.{ms:03}"
        out += [stamp(start)+" --> "+stamp(end), cue["text"], ""]
    response=app.response_class("\n".join(out),mimetype="text/vtt"); response.headers["Cache-Control"]="no-store"; return response

@app.get("/api/cues")
def cues():
    p=movie_or_404(request.args.get("path"))
    if not p: return jsonify(cues=[], tracks=[])
    with lock:
        state=load_state(); m=state.get("movies",{}).get(str(p))
        if not m or not m.get("working_srt"): return jsonify(cues=[], tracks=[])
        working = Path(m["working_srt"])
        if not working.is_file(): return jsonify(cues=[], tracks=[])
        # Migrate the single cached track from versions predating track history.
        if m.get("current_candidate_id") is not None and not m.get("track_history"):
            preserve_legacy_track(p, m)
            save_state(state)
        working = Path(m["working_srt"])
        return jsonify(cues=parse_srt(working.read_text(encoding="utf-8", errors="replace")), name=m.get("current_candidate_name", "Downloaded subtitle"),
                       tracks=available_tracks(m, load_alignment_results().get(str(p), [])), current_id=m.get("current_candidate_id"), preferred_id=m.get("preferred_track_id"))

@app.post("/api/select-track")
def api_select_track():
    data=request.get_json() or {}; p=movie_or_404(data.get("path"))
    if not p: return jsonify(error="Invalid movie"), 400
    with lock:
        state=load_state(); movie=state.get("movies", {}).get(str(p))
        if not movie or not select_track(movie, data.get("id")):
            return jsonify(error="Subtitle track is unavailable"), 404
        save_state(state)
    return jsonify(ok=True)

@app.post("/api/reject-track")
def api_reject_track():
    data=request.get_json() or {}; p=movie_or_404(data.get("path")); track_id=data.get("id")
    if not p or track_id is None: return jsonify(error="Invalid movie or track"), 400
    with lock:
        state=load_state(); movie=state.get("movies", {}).get(str(p))
        if not movie: return jsonify(error="Invalid movie"), 400
        history=movie.get("track_history", [])
        track=next((t for t in history if str(t.get("id")) == str(track_id)), None)
        if not track: return jsonify(error="Subtitle track is unavailable"), 404
        rejected=movie.setdefault("rejected_ids", [])
        if str(track["id"]) not in set(map(str, rejected)): rejected.append(track["id"])
        movie["candidates"]=[c for c in movie.get("candidates", []) if str(c.get("download_id")) != str(track_id)]
        if str(movie.get("current_candidate_id")) == str(track_id):
            for alternative in history:
                if str(alternative.get("id")) not in set(map(str, rejected)) and select_track(movie, alternative["id"]): break
        save_state(state)
    return jsonify(ok=True)

@app.post("/api/decision")
def decision():
    data=request.json; p=movie_or_404(data.get("path")); s=load_state(); m=s.get("movies",{}).get(str(p))
    if not p or not m: return jsonify(error="Invalid movie"),400
    if data.get("action")=="approve":
        try:
            final=p.with_suffix(".eng.srt"); final.write_text(shift_srt(Path(m["working_srt"]).read_text(),float(data.get("offset",m.get("offset",0))))); m["has_subtitles"]=True; m["has_sidecar"]=True
        except OSError as e:
            return jsonify(error=f"Could not save subtitle beside movie: {e}"),500
        update_movie(p, {"has_subtitles": True, "has_sidecar": True}); return jsonify(ok=True)
    if data.get("action") != "reject": return jsonify(error="Invalid action"), 400
    rejected = m.setdefault("rejected_ids", [])
    current = m.get("current_candidate_id")
    if current is not None and current not in rejected: rejected.append(current)
    candidates=[c for c in m.get("candidates", []) if str(c.get("download_id")) not in set(map(str, rejected))]
    m["candidates"] = candidates
    if not candidates: update_movie(p, {"candidates": candidates, "rejected_ids": rejected}); return jsonify(ok=False,error="No more ranked subtitle candidates are available"),404
    m["candidates"]=candidates; ok,error=download_candidate(p,m,candidates[0])
    if not ok: update_movie(p, {"candidates": candidates, "rejected_ids": rejected}); return jsonify(ok=False,error=error),502
    update_movie(p, {k:m[k] for k in ("working_srt", "current_candidate_id", "current_candidate_name", "offset", "track_history", "candidates", "rejected_ids")}); return jsonify(ok=True,candidate=candidates[0].get("name","next candidate"))

if __name__=="__main__": app.run(host="0.0.0.0",port=8085)

