#!/usr/bin/env python3
"""Sample movies missing subtitles and save absolute-time transcripts for later alignment."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import wave
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
            name = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)

def read_ratings(path):
    if not path:
        return {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not {'title', 'rating'}.issubset(reader.fieldnames or []):
            raise ValueError('Ratings CSV needs title and rating columns')
        return {row['title'].strip().casefold(): row['rating'].strip().upper() for row in reader if row.get('title') and row.get('rating')}

def movie_priority(movie, ratings):
    rating = ratings.get(movie.get('title', '').casefold()) or str(movie.get('rating') or movie.get('mpaa_rating') or '').upper()
    return (0 if rating == 'PG-13' else 1 if rating == 'PG' else 2, movie.get('title', '').casefold())

def local_movie_path(container_path, host_root, container_root):
    relative = PurePosixPath(container_path).relative_to(PurePosixPath(container_root))
    if '..' in relative.parts:
        raise ValueError('Movie path escapes container movie directory')
    return host_root.joinpath(*relative.parts)

def subtitle_tracks(movie, data_dir):
    tracks = list(movie.get('track_history') or [])
    if not tracks and movie.get('working_srt'):
        tracks = [{'id': movie.get('current_candidate_id'), 'name': movie.get('current_candidate_name', 'Downloaded subtitle'), 'file': movie['working_srt']}]
    result = []
    for entry in tracks:
        path = PurePosixPath(entry.get('file', ''))
        try:
            rel = path.relative_to('/data')
            if '..' in rel.parts:
                continue
            local = data_dir.joinpath(*rel.parts)
            content = local.read_bytes()
            result.append({'id': entry.get('id'), 'name': entry.get('name'),
                           'container_path': str(path), 'local_path': str(local),
                           'sha256': hashlib.sha256(content).hexdigest(),
                           'srt': content.decode('utf-8-sig', errors='replace')})
        except (OSError, ValueError):
            result.append({'id': entry.get('id'), 'name': entry.get('name'), 'container_path': str(path), 'error': 'Subtitle file unavailable'})
    return result

def duration(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], text=True, timeout=120)
    value = float(out.strip())
    if not value > 0:
        raise ValueError('Invalid movie duration')
    return value

def extract_audio(movie, start, length, target):
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', str(movie), '-t', str(length), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)], check=True, timeout=300)

def new_record(movie, local, model, language, sample_seconds, skip_seconds, ratings):
    stat = local.stat()
    rating = ratings.get(movie['title'].casefold()) or movie.get('rating') or movie.get('mpaa_rating')
    return {'schema_version': 1, 'movie': {'title': movie['title'], 'container_path': movie['path'],
            'host_path': str(local), 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'rating': rating or None},
            'transcription': {'engine': 'faster-whisper', 'model': model, 'language_requested': language,
                              'sample_seconds': sample_seconds, 'skip_seconds': skip_seconds,
                              'time_reference': 'seconds from start of movie', 'status': 'in_progress',
                              'created_at': now(), 'updated_at': now()},
            'subtitle_tracks': [], 'samples': []}

def same_run(old, new):
    keys = ('model', 'language_requested', 'sample_seconds', 'skip_seconds')
    return (old.get('movie', {}).get('size_bytes') == new['movie']['size_bytes'] and
            old.get('movie', {}).get('mtime_ns') == new['movie']['mtime_ns'] and
            all(old.get('transcription', {}).get(k) == new['transcription'][k] for k in keys))

def run_movie(movie, args, ratings, model):
    path = local_movie_path(movie['path'], args.movie_root, args.container_movie_root)
    if not path.is_file():
        raise FileNotFoundError(f'Movie not found on host: {path}')
    key = hashlib.sha256(movie['path'].encode()).hexdigest()[:16]
    output = args.output / f'{key}.json'
    fresh = new_record(movie, path, args.model, args.language, args.sample_seconds, args.skip_seconds, ratings)
    if output.exists():
        record = json.loads(output.read_text(encoding='utf-8'))
        if not same_run(record, fresh):
            raise ValueError(f'Existing transcript uses a different movie or settings; preserve or move {output} before restarting')
    else:
        record = fresh
    record['subtitle_tracks'] = subtitle_tracks(movie, args.data_dir)
    record['transcription'].update(status='in_progress', updated_at=now())
    record['transcription'].pop('error', None)
    atomic_json(output, record)
    total = duration(path)
    record['movie']['duration_seconds'] = total
    done = {round(sample['start_seconds'], 3) for sample in record['samples'] if sample.get('status') == 'complete'}
    step = args.sample_seconds + args.skip_seconds
    start = 0
    while start < total:
        length = min(args.sample_seconds, total - start)
        if round(start, 3) not in done:
            print(f'  {start/60:.1f} min / {total/60:.1f} min', flush=True)
            try:
                with tempfile.TemporaryDirectory(prefix='subtitle-transcribe-') as tmp:
                    wav = Path(tmp) / 'sample.wav'
                    extract_audio(path, start, length, wav)
                    # Passing decoded PCM avoids a PyAV/faster-whisper version
                    # incompatibility around av.open(metadata_errors=...).
                    import numpy as np
                    with wave.open(str(wav), 'rb') as audio:
                        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                            raise ValueError('Expected mono 16 kHz 16-bit PCM audio')
                        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
                    segments, info = model.transcribe(samples, language=args.language, beam_size=args.beam_size, vad_filter=False, word_timestamps=True)
                    results = []
                    for segment in segments:
                        words = [{'start_seconds': round(start + word.start, 3), 'end_seconds': round(start + word.end, 3), 'text': word.word, 'probability': word.probability} for word in (segment.words or [])]
                        results.append({'start_seconds': round(start + segment.start, 3), 'end_seconds': round(start + segment.end, 3), 'text': segment.text.strip(), 'avg_logprob': segment.avg_logprob, 'no_speech_prob': segment.no_speech_prob, 'words': words})
                record['samples'].append({'start_seconds': start, 'end_seconds': round(start+length, 3), 'status': 'complete', 'detected_language': info.language, 'language_probability': info.language_probability, 'segments': results})
                record['samples'].sort(key=lambda x: x['start_seconds'])
                record['transcription']['updated_at'] = now()
                atomic_json(output, record)
            except Exception as exc:
                record['transcription'].update(status='error', error=str(exc), updated_at=now())
                atomic_json(output, record)
                raise
        start += step
    record['transcription'].update(status='complete', updated_at=now())
    atomic_json(output, record)
    return output

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT/'data/state.json')
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data')
    parser.add_argument('--output', type=Path, default=ROOT/'transcriptions')
    parser.add_argument('--movie-root', type=Path, default=Path('/Volumes/Media/Movies'), help='Host path corresponding to the container movie directory')
    parser.add_argument('--container-movie-root', default='/movies/Movies')
    parser.add_argument('--ratings', type=Path, default=ROOT/'scripts/movie_ratings.csv')
    parser.add_argument('--model', default='tiny.en', help='faster-whisper model or local model path (default: tiny.en)')
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--compute-type', default='int8')
    parser.add_argument('--language', default='en')
    parser.add_argument('--beam-size', type=int, default=1)
    parser.add_argument('--sample-seconds', type=int, default=60)
    parser.add_argument('--skip-seconds', type=int, default=300)
    parser.add_argument('--limit', type=int, help='Process at most this many movies, including failures')
    parser.add_argument('--list', action='store_true', help='List the queue without loading the model')
    args = parser.parse_args()
    if args.sample_seconds <= 0 or args.skip_seconds < 0 or args.beam_size < 1 or (args.limit is not None and args.limit < 1):
        parser.error('Sample length, beam size, and limit must be positive; skip must be nonnegative')
    try:
        ratings = read_ratings(args.ratings)
        state = json.loads(args.state.read_text(encoding='utf-8'))
        movies = [m for m in state.get('movies', {}).values() if m.get('path') and m.get('title') and not m.get('has_subtitles') and (m.get('embedded_checked') or m.get('has_sidecar'))]
        movies.sort(key=lambda m: movie_priority(m, ratings))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'Cannot load movie queue: {exc}', file=sys.stderr); return 1
    if args.limit: movies = movies[:args.limit]
    if args.list:
        for m in movies:
            print(f"{ratings.get(m['title'].casefold()) or m.get('rating') or m.get('mpaa_rating') or 'unknown':7}  {m['title']}")
        print(f'{len(movies)} movies queued'); return 0
    if not args.movie_root.is_dir():
        print(f'Movie share is not mounted at {args.movie_root}; mount it or use --movie-root', file=sys.stderr); return 1
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    except Exception as exc:
        print(f'Could not load faster-whisper model: {exc}', file=sys.stderr); return 1
    failures = 0
    for index, movie in enumerate(movies, 1):
        print(f'[{index}/{len(movies)}] {movie["title"]}', flush=True)
        try:
            out = run_movie(movie, args, ratings, model)
            print(f'  Saved {out}', flush=True)
        except Exception as exc:
            failures += 1; print(f'  Failed: {exc}', file=sys.stderr, flush=True)
    print(f'Finished: {len(movies)-failures} completed, {failures} failed.', flush=True)
    return 1 if failures else 0

if __name__ == '__main__':
    raise SystemExit(main())
