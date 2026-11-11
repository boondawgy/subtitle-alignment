#!/usr/bin/env python3
"""Compare timestamped Whisper samples with cached subtitles; write review recommendations."""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics
import sys

from transcribe_missing import ROOT, atomic_json, subtitle_tracks, now

STOP = set('a an and are as at be but by did do for from had has have he her him his i if in is it me my of on or our she so that the their them then there they this to us was we were what when where who will with you your'.split())
TIME = re.compile(r'(\d{1,3}):(\d\d):(\d\d)[,.](\d{1,3})')

def tokens(text):
    text = re.sub(r'<[^>]*>|\{[^}]*\}', ' ', html.unescape(text.lower()))
    return re.findall(r"[a-z]+(?:'[a-z]+)?|\d+", text)

def secs(match):
    h,m,s,ms=match.groups()
    return int(h)*3600+int(m)*60+int(s)+int(ms.ljust(3,'0'))/1000

def cues(srt):
    result=[]
    for block in re.split(r'\n\s*\n', srt.replace('\r','').strip()):
        lines=block.splitlines(); idx=next((i for i,line in enumerate(lines) if '-->' in line),None)
        if idx is None: continue
        matches=list(TIME.finditer(lines[idx]))
        if len(matches)<2: continue
        text=' '.join(lines[idx+1:]); words=tokens(text)
        if words: result.append({'start':secs(matches[0]),'end':secs(matches[1]),'text':text,'words':words})
    return result

def similarity(a,b):
    # Sequence alignment tolerates insertions, omissions, and some misheard words.
    return SequenceMatcher(None,a,b,autojunk=False).ratio()

def analyze(transcript, track, max_offset=600):
    subs=cues(track.get('srt',''))
    base={'track_id':track.get('id'),'track_name':track.get('name'),'subtitle_sha256':track.get('sha256')}
    if not subs: return {**base,'status':'unavailable','reason':'No readable subtitle cues'}
    index=defaultdict(list)
    for i,c in enumerate(subs):
        for w in set(c['words'])-STOP:
            if len(w)>=3: index[w].append(i)
    candidates=[]; eligible=0
    for sample in transcript.get('samples',[]):
        for segment in sample.get('segments',[]):
            words=tokens(segment.get('text','')); salient=set(words)-STOP
            if len(words)<3 or not salient: continue
            eligible+=1
            likely=Counter()
            for word in salient:
                indices=index.get(word,[])
                if len(indices)<=80:
                    for i in indices: likely[i]+=1
            if not likely: continue
            choices=[]
            for i,count in likely.most_common(80):
                c=subs[i]; diff=segment['start_seconds']-c['start']
                if abs(diff)>max_offset: continue
                score=similarity(words,c['words'])
                if score>=.58 and (len(words)>=4 or count>=2):
                    choices.append((score,abs(diff),i,diff))
            for score,_,i,diff in sorted(choices,reverse=True)[:4]:
                candidates.append({'segment':(sample['start_seconds'],segment['start_seconds']), 'sample':sample['start_seconds'], 'subtitle_index':i,'offset':diff,'similarity':score,'speech':segment['text'],'subtitle':subs[i]['text']})
    if not candidates:
        return {**base,'status':'insufficient evidence','eligible_segments':eligible,'fit_score':None,'recommended_offset_seconds':None,'reason':'No reliable dialogue matches found'}
    # Vote in overlapping 4-second neighborhoods, one vote per transcript segment.
    centers=range(math.floor(min(x['offset'] for x in candidates))-2,math.ceil(max(x['offset'] for x in candidates))+3)
    def best_at(center):
        best={}
        for hit in candidates:
            if abs(hit['offset']-center)<=2:
                prev=best.get(hit['segment'])
                if prev is None or hit['similarity']>prev['similarity']: best[hit['segment']]=hit
        return list(best.values())
    # A segment's evidence must agree on an offset across multiple samples.
    seed=max(centers,key=lambda center:sum(h['similarity']**2 for h in best_at(center)))
    hits=best_at(seed); offset=statistics.median(h['offset'] for h in hits)
    hits=[h for h in best_at(offset) if abs(h['offset']-offset)<=2]
    offset=statistics.median(h['offset'] for h in hits)
    windows=len({h['sample'] for h in hits}); eligible_windows=sum(any(len(tokens(x.get('text','')))>=3 for x in sample.get('segments',[])) for sample in transcript.get('samples',[]))
    coverage=len(hits)/max(eligible,1); window_coverage=windows/max(eligible_windows,1)
    mean=statistics.mean(h['similarity'] for h in hits)
    # Composite heuristic, not a calibrated probability.
    score=round(100 * min(1, .55*min(1,coverage/.35)+.30*window_coverage+.15*mean))
    enough=len(hits)>=4 and windows>=2
    deltas=[h['offset'] for h in hits]; spread=statistics.median(abs(x-offset) for x in deltas)
    ordered=sorted(hits,key=lambda h:h['segment'][1]); drift=None
    if len(ordered)>=8 and windows>=4:
        n=max(2,len(ordered)//3)
        drift=round(statistics.median(x['offset'] for x in ordered[-n:])-statistics.median(x['offset'] for x in ordered[:n]),2)
    evidence=[{'movie_time_seconds':round(h['segment'][1],2),'subtitle_time_seconds':round(subs[h['subtitle_index']]['start'],2),'offset_seconds':round(h['offset'],2),'similarity':round(h['similarity'],2),'transcription':h['speech'],'subtitle':h['subtitle']} for h in sorted(hits,key=lambda h:h['similarity'],reverse=True)[:8]]
    return {**base,'status':'analyzed' if enough else 'insufficient evidence','fit_score':score if enough else None,
            'recommended_offset_seconds':round(offset,2) if enough else None,'offset_convention':'Add this many seconds to subtitle timestamps; positive means later',
            'matching_segments':len(hits),'eligible_segments':eligible,'matching_sample_windows':windows,'eligible_sample_windows':eligible_windows,
            'median_offset_deviation_seconds':round(spread,2),'estimated_end_to_start_drift_seconds':drift,
            'warning': 'Offset may vary over the movie; review timing at beginning and end.' if drift is not None and abs(drift)>3 else None,'example_matches':evidence}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=ROOT/'data/state.json')
    parser.add_argument('--transcriptions',type=Path,default=ROOT/'transcriptions')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    parser.add_argument('--output',type=Path,default=ROOT/'alignment-analysis')
    args=parser.parse_args(); state=json.loads(args.state.read_text());results=[]
    for path,movie in state.get('movies',{}).items():
        if movie.get('has_subtitles') or not (movie.get('working_srt') or movie.get('track_history')): continue
        if movie.get('title') in ('Cool Runnings','Wish Upon a Star'): continue
        transcript_path=args.transcriptions/(hashlib.sha256(path.encode()).hexdigest()[:16]+'.json')
        if not transcript_path.exists():continue
        try: transcript=json.loads(transcript_path.read_text())
        except (OSError,ValueError) as exc:
            print(f"Skipping {movie.get('title')}: {exc}",file=sys.stderr);continue
        if transcript.get('transcription',{}).get('status')!='complete':continue
        tracks=subtitle_tracks(movie,args.data_dir)
        item={'title':movie['title'],'movie_path':path,'transcript_path':str(transcript_path),'tracks':[analyze(transcript,t) for t in tracks]}
        results.append(item);print(f"{len(results)}. {item['title']}",flush=True)
    results.sort(key=lambda x:x['title'].casefold())
    report={'generated_at':now(),'score_description':'Heuristic 0–100 text/timing fit; not a calibrated probability. Offset is added to subtitle times.','movies':results}
    args.output.mkdir(parents=True,exist_ok=True);atomic_json(args.output/'report.json',report)
    lines=['# Subtitle alignment review','',f"Generated: {report['generated_at']}",'','Scores are heuristic, not probabilities. Positive offsets move subtitles later; negative offsets move them earlier. Review before saving.','','| Movie | Track | Fit | Suggested offset | Evidence |','| --- | --- | ---: | ---: | --- |']
    for movie in results:
        for track in movie['tracks']:
            score=str(track['fit_score'])+'/100' if track.get('fit_score') is not None else 'Insufficient evidence'
            offset=f"{track['recommended_offset_seconds']:+.2f} s" if track.get('recommended_offset_seconds') is not None else '—'
            evidence=f"{track.get('matching_segments',0)} matches / {track.get('matching_sample_windows',0)} samples" + ('; timing may drift' if track.get('warning') else '')
            safe=lambda v:str(v).replace('|','\\|').replace('\n',' ')
            lines.append(f"| {safe(movie['title'])} | {safe(track.get('track_name') or 'Unknown')} | {score} | {offset} | {evidence} |")
    (args.output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f"Analyzed {len(results)} movies. Reports: {args.output/'report.md'} and {args.output/'report.json'}")

if __name__=='__main__':main()
