import importlib.util
import json
from pathlib import Path
import tempfile
import types
import wave
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('transcribe_missing', Path(__file__).resolve().parents[1] / 'scripts' / 'transcribe_missing.py')
transcribe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transcribe)

class TranscriptionTests(unittest.TestCase):
    def test_priority_and_unknown(self):
        ratings = {'a': 'PG', 'b': 'PG-13'}
        movies = [{'title':'C'}, {'title':'A'}, {'title':'B'}]
        self.assertEqual([m['title'] for m in sorted(movies, key=lambda m: transcribe.movie_priority(m, ratings))], ['B', 'A', 'C'])

    def test_rejects_path_outside_library(self):
        with self.assertRaises(ValueError):
            transcribe.local_movie_path('/movies/Movies/../secret.mkv', Path('/host'), '/movies/Movies')

    def test_samples_resume_and_use_absolute_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); movie = root / 'test.mkv'; movie.write_bytes(b'video')
            args = types.SimpleNamespace(movie_root=root, container_movie_root='/movies/Movies', output=root/'out', data_dir=root/'data', model='tiny.en', language='en', sample_seconds=60, skip_seconds=300, beam_size=1)
            record = {'title':'Test', 'path':'/movies/Movies/test.mkv'}
            word = types.SimpleNamespace(start=1.0, end=2.0, word=' hello', probability=.9)
            segment = types.SimpleNamespace(start=1.0, end=2.0, text=' hello', avg_logprob=-.1, no_speech_prob=.01, words=[word])
            model = types.SimpleNamespace(transcribe=lambda *a, **k: (iter([segment]), types.SimpleNamespace(language='en', language_probability=.99)))
            def fake_extract(movie, start, length, target):
                with wave.open(str(target), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                    wav.writeframes(b'\x00\x00' * 100)
            with patch.object(transcribe, 'duration', return_value=400), patch.object(transcribe, 'extract_audio', side_effect=fake_extract) as extract:
                output=transcribe.run_movie(record, args, {}, model)
                self.assertEqual(extract.call_count, 2)
                saved=json.loads(output.read_text())
                self.assertEqual([x['start_seconds'] for x in saved['samples']], [0, 360])
                self.assertEqual(saved['samples'][1]['segments'][0]['words'][0]['start_seconds'], 361)
                self.assertEqual(saved['samples'][1]['end_seconds'], 400)
                transcribe.run_movie(record, args, {}, model)
                self.assertEqual(extract.call_count, 2)

    def test_snapshots_subtitle_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'one.srt').write_text('subtitle text')
            result=transcribe.subtitle_tracks({'track_history':[{'id':1, 'name':'First', 'file':'/data/one.srt'}]}, root)
            self.assertEqual(result[0]['srt'], 'subtitle text')
            self.assertTrue(result[0]['sha256'])

if __name__ == '__main__': unittest.main()
