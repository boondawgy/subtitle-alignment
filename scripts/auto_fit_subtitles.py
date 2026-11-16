#!/usr/bin/env python3
"""Download and score subtitle candidates until a movie meets a target or limit."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from analyze_alignment import analyze, write_report
from transcribe_missing import ROOT, atomic_json, subtitle_tracks, now

EXCLUDED = {'Cool Runnings', 'Wish Upon a Star'}

class BatchError(Exception):
    pass

class SearchIncomplete(BatchError):
    pass

class Api:
    def __init__(self, base):
        self.base = base.rstrip('/')

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data=data, method=method, headers={'Content-Type':'application/json'} if data else {})
        try:
            with urlopen(req, timeout=150) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.loads(exc.read()).get('error', f'HTTP {exc.code}')
            except (ValueError, AttributeError):
                message = f'HTTP {exc.code}'
            raise BatchError(f'{message}') from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise BatchError(f'Could not complete {method} {path}: {exc}') from exc

def downloaded_ids(movie):
    # User-created split/trim tracks are not OpenSubtitles downloads.
    ids = {str(t.get('id')) for t in movie.get('track_history', []) if str(t.get('id', '')).isdigit()}
    ids.update(str(x) for x in movie.get('rejected_ids', []) if str(x).isdigit())
    current = movie.get('current_candidate_id')
    if current is not None and str(current).isdigit(): ids.add(str(current))
    return ids

def load_json(path, default):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: return default
    except (OSError, ValueError) as exc: raise BatchError(f'Cannot read {path}: {exc}') from exc

def completed_transcript(path, directory):
    filename = directory / (hashlib.sha256(path.encode()).hexdigest()[:16]+'.json')
    transcript = load_json(filename, None)
    if not transcript or transcript.get('transcription', {}).get('status') != 'complete': return filename, None
    return filename, transcript

def score_movie(movie, transcript_path, transcript, args):
    """Reuse only analyses whose track identity and content hash still match."""
    report_path = args.report_dir/'report.json'
    report = load_json(report_path, {'movies':[]})
    existing = next((m for m in report.get('movies',[]) if m.get('movie_path')==movie['path']), {})
    transcript_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    cache = {(str(t.get('track_id')), t.get('subtitle_sha256')):t for t in existing.get('tracks',[]) if t.get('subtitle_sha256')} if existing.get('transcript_sha256') == transcript_hash else {}
    tracks = []
    for track in subtitle_tracks(movie, args.data_dir):
        key = (str(track.get('id')), track.get('sha256'))
        tracks.append(cache.get(key) or analyze(transcript, track))
    entry = {'title':movie['title'], 'movie_path':movie['path'], 'transcript_path':str(transcript_path), 'transcript_sha256':transcript_hash, 'tracks':tracks}
    report['movies'] = sorted([m for m in report.get('movies',[]) if m.get('movie_path')!=movie['path']]+[entry], key=lambda x:x['title'].casefold())
    report['generated_at'] = now()
    report.setdefault('score_description','Heuristic 0–100 text/timing fit; not a calibrated probability. Offset is added to subtitle times.')
    write_report(args.report_dir, report)
    rejected = set(map(str, movie.get('rejected_ids', [])))
    scores = [t['fit_score'] for t in tracks if str(t.get('track_id')) not in rejected and t.get('status')=='analyzed' and t.get('fit_score') is not None]
    return max(scores) if scores else None

def get_movie(state_path, path):
    return load_json(state_path, {}).get('movies',{}).get(path)

def error_is_limit(message):
    return any(term in message.lower() for term in ('406','429','quota','rate limit','login','token','sign in'))

def search_candidates(api, movie, tried, metadata, empty_page_limit=2):
    """Yield one page at a time; immediately download before another search replaces it."""
    searches = [None]
    meta = metadata.get(movie['path'], {})
    if meta.get('imdb_id') and (not meta.get('year') or not meta.get('imdb_year') or meta['year']==meta['imdb_year']):
        searches.append(meta['imdb_id'])
    seen = set()
    hit_search_limit = False
    for ident in searches:
        page = 1
        empty_match_pages = 0
        while True:
            payload = {'path':movie['path'], 'page':page}
            if ident: payload['imdb_id'] = ident
            try:
                result = api.call('POST','/api/search',payload)
            except BatchError as exc:
                if 'pagination too deep' not in str(exc).lower():
                    raise
                hit_search_limit = True
                print('  OpenSubtitles search-depth limit reached; trying any available ID-based search.', flush=True)
                break
            for candidate in result.get('candidates',[]):
                cid = str(candidate.get('download_id', ''))
                if cid and cid not in tried and cid not in seen:
                    seen.add(cid)
                    yield candidate
            total = result.get('total_pages')
            if total is None:
                raise BatchError('OpenSubtitles did not report the number of result pages; candidate exhaustion cannot be verified.')
            try: total = int(total)
            except (TypeError, ValueError): raise BatchError('OpenSubtitles returned an invalid page count.')
            if page >= total: break
            matched_count = result.get('matched_count')
            if empty_page_limit and isinstance(matched_count, int):
                empty_match_pages = empty_match_pages + 1 if matched_count == 0 else 0
                if empty_match_pages >= empty_page_limit:
                    hit_search_limit = True
                    print(f'  No matching titles in {empty_match_pages} result pages; moving on from this broad search.', flush=True)
                    break
            page += 1
            time.sleep(.25)
    if hit_search_limit:
        raise SearchIncomplete('Search was limited before every result could be checked; there may be more candidates')

def run_movie(api, item, args, progress, metadata, totals):
    path=item['path'];title=item['title']
    if title in EXCLUDED:
        return 'excluded from transcription/analysis by earlier request'
    movie=get_movie(args.state,path)
    if not movie or movie.get('has_subtitles') or movie.get('passed') or not movie.get('embedded_checked'):
        return 'no longer eligible'
    transcript_path, transcript = completed_transcript(path,args.transcriptions)
    if not transcript: return 'no completed transcription; cannot score safely'
    best=score_movie(movie,transcript_path,transcript,args)
    if best is not None and best>=args.target: return f'already meets target ({best}/100)'
    ids=downloaded_ids(movie)
    if len(ids)>=args.max_tracks: return f'already has {len(ids)} downloaded tracks'
    uncertain = set(progress.get('uncertain',{}).get(path,[]))
    tried=ids | uncertain
    if args.dry_run: return f'would search: best={best if best is not None else "unscored"}, downloaded={len(ids)}'
    search=search_candidates(api,movie,tried,metadata,empty_page_limit=None if getattr(args,'deep_search',False) else 2)
    while len(ids)<args.max_tracks:
        try: candidate=next(search)
        except StopIteration: return f'no more matching untried candidates; downloaded={len(ids)}, best={best if best is not None else "unscored"}'
        except SearchIncomplete as exc: return f'{exc}; downloaded={len(ids)}, best={best if best is not None else "unscored"}'
        if args.max_new_downloads is not None and totals['attempts']>=args.max_new_downloads:
            totals['stopped']='Reached --max-new-downloads';return totals['stopped']
        current=get_movie(args.state,path)
        if not current or current.get('has_subtitles') or current.get('passed') or not current.get('embedded_checked'):return 'no longer eligible'
        ids=downloaded_ids(current)
        if len(ids)>=args.max_tracks:return f'already has {len(ids)} downloaded tracks'
        cid=str(candidate['download_id'])
        if cid in ids or cid in set(map(str,current.get('rejected_ids',[]))):continue
        # Persist before issuing the request: if interrupted, never spend again on an unknown outcome.
        pending=progress.setdefault('uncertain',{}).setdefault(path,[])
        if cid not in pending:pending.append(cid)
        atomic_json(args.progress,progress)
        totals['attempts']+=1
        try:
            api.call('POST','/api/download',{'path':path,'file_id':candidate['download_id']})
        except BatchError as exc:
            current=get_movie(args.state,path) or {}
            rejected = any(marker in str(exc).lower() for marker in ('http 406', 'http 429', 'login was rejected', 'rejected the account token', 'set both opensubtitles_username'))
            if cid in downloaded_ids(current) or rejected:
                pending.remove(cid);atomic_json(args.progress,progress)
                raise BatchError(f'{title}: {exc}') from exc
            raise BatchError(f'{title}: {exc}. The candidate is marked uncertain to prevent an automatic duplicate.') from exc
        movie=get_movie(args.state,path)
        if not movie or cid not in downloaded_ids(movie):
            raise BatchError(f'{title}: download returned success but the track was not recorded; stopping.')
        pending.remove(cid);atomic_json(args.progress,progress)
        ids=downloaded_ids(movie);totals['downloaded']+=1
        best=score_movie(movie,transcript_path,transcript,args)
        print(f'  Downloaded {len(ids)}/{args.max_tracks}; best fit {best if best is not None else "unscored"}',flush=True)
        if best is not None and best>=args.target:return f'target reached ({best}/100)'
        time.sleep(.25)
    return f'{args.max_tracks}-track limit reached; best={best if best is not None else "unscored"}'

def prompt_number(label, default, minimum, maximum=None):
    while True:
        try:
            value=input(f'{label} [{default}]: ').strip()
        except EOFError as exc:
            raise BatchError('Input ended before the batch settings were chosen.') from exc
        if not value:
            return default
        try:
            number=int(value)
        except ValueError:
            number=None
        if number is not None and number>=minimum and (maximum is None or number<=maximum):
            return number
        print(f'Please enter an integer from {minimum} to {maximum}.' if maximum is not None else f'Please enter an integer of at least {minimum}.',file=sys.stderr)

def configure_limits(args, interactive):
    if args.target is None:
        args.target=prompt_number('Target fit score (0–100)',96,0,100) if interactive else 96
    if args.max_tracks is None:
        args.max_tracks=prompt_number('Maximum total downloaded tracks per movie, including existing',10,1) if interactive else 10

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8085')
    parser.add_argument('--state',type=Path,default=ROOT/'data/state.json')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    parser.add_argument('--transcriptions',type=Path,default=ROOT/'transcriptions')
    parser.add_argument('--report-dir',type=Path,default=ROOT/'alignment-analysis')
    parser.add_argument('--progress',type=Path,default=ROOT/'data/auto-fit-progress.json')
    parser.add_argument('--title',action='append',help='Only process this exact title; repeat to select more movies')
    parser.add_argument('--target',type=int,help='Target fit score (default: 96; prompts in a terminal)')
    parser.add_argument('--max-tracks',type=int,help='Maximum total downloaded tracks per movie (default: 10; prompts in a terminal)')
    parser.add_argument('--max-new-downloads',type=int,help='Stop after this many new download attempts in this run')
    parser.add_argument('--allow-anonymous',action='store_true',help='Allow downloads without a configured account')
    parser.add_argument('--deep-search',action='store_true',help='Keep searching broad results until the provider pagination limit')
    parser.add_argument('--dry-run',action='store_true',help='Score existing tracks and show what would be processed; do not search or download')
    args=parser.parse_args()
    if (args.target is not None and not 0<=args.target<=100) or (args.max_tracks is not None and args.max_tracks<1) or (args.max_new_downloads is not None and args.max_new_downloads<1):parser.error('Target must be 0–100 and limits must be positive')
    try:
        configure_limits(args, sys.stdin.isatty())
        print(f'Target: {args.target}/100; maximum downloaded tracks per movie: {args.max_tracks} (including existing).',flush=True)
        args.progress.parent.mkdir(parents=True,exist_ok=True)
        with (args.progress.parent/'auto-fit.lock').open('w') as lockfile:
            try:fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise BatchError('Another auto-fit batch is already running.')
            api=Api(args.base_url);data=api.call('GET','/api/movies')
            if data.get('auth_status') not in ('account configured','account signed in') and not args.allow_anonymous and not args.dry_run:
                raise BatchError('OpenSubtitles account is not configured or was rejected; restart the app with credentials, or use --allow-anonymous.')
            items=[m for m in data.get('movies',[]) if not m.get('has_subtitles') and not m.get('passed') and m.get('scan_status')=='ready']
            if args.title:
                titles={x.casefold() for x in args.title};items=[m for m in items if m['title'].casefold() in titles]
                missing=titles-{m['title'].casefold() for m in items}
                if missing:raise BatchError('Requested movie not found in the eligible library: '+', '.join(sorted(missing)))
            items.sort(key=lambda m:(0 if m.get('best_fit_score') is None else 1,m.get('best_fit_score') if m.get('best_fit_score') is not None else -1,m['title'].casefold()))
            progress=load_json(args.progress,{'uncertain':{}})
            metadata_path=args.data_dir/'prefetch-metadata.json'
            metadata={x['path']:x for x in load_json(metadata_path,[]) if x.get('path')}
            totals={'attempts':0,'downloaded':0,'stopped':None};summary=[]
            for i,m in enumerate(items,1):
                print(f'[{i}/{len(items)}] {m["title"]}',flush=True)
                outcome=run_movie(api,m,args,progress,metadata,totals);summary.append({'title':m['title'],'result':outcome});print(' ',outcome,flush=True)
                if totals['stopped']:break
            output={'completed_at':now(),'target':args.target,'max_tracks':args.max_tracks,'dry_run':args.dry_run,**totals,'movies':summary}
            atomic_json(args.data_dir/'auto-fit-last-run.json',output)
            print(f"Finished: {totals['downloaded']} downloaded; {len(summary)} movies checked.",flush=True)
            return 0
    except (BatchError, OSError, ValueError) as exc:
        print(f'Stopped: {exc}',file=sys.stderr,flush=True);return 1
    except KeyboardInterrupt:
        print('Interrupted. Run again to resume; any uncertain download is not retried automatically.',file=sys.stderr);return 130

if __name__=='__main__':raise SystemExit(main())
