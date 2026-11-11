import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Core tests run even on machines without the Docker image's dependencies.
try:
    import flask, requests
except ImportError:
    flask = types.ModuleType('flask')
    class Flask:
        def __init__(self, *args): self.logger = types.SimpleNamespace(exception=lambda *args: None)
        def get(self, *args): return lambda f: f
        post = get
    flask.Flask = Flask
    for name in ('jsonify', 'render_template', 'send_file'):
        setattr(flask, name, lambda *args, **kwargs: None)
    flask.jsonify = lambda *args, **kwargs: kwargs
    flask.request = types.SimpleNamespace()
    sys.modules['flask'] = flask
    sys.modules['requests'] = types.ModuleType('requests')

spec = importlib.util.spec_from_file_location('subtitle_app', Path(__file__).resolve().parents[1] / 'app.py')
app = importlib.util.module_from_spec(spec)
with tempfile.TemporaryDirectory() as initial:
    with patch.dict('os.environ', {'DATA_DIR': initial}):
        spec.loader.exec_module(app)

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        app.MOVIE_DIR = self.root / 'movies'; app.MOVIE_DIR.mkdir()
        app.DATA_DIR = self.root / 'data'; app.DATA_DIR.mkdir()
        app.STATE_FILE = app.DATA_DIR / 'state.json'

    def test_standard_srt_and_shift(self):
        text = '1\n00:00:01,500 --> 00:00:03,000\nHello[br]world\n'
        self.assertEqual(app.parse_srt(text)[0]['start'], 1.5)
        self.assertEqual(app.parse_srt(text)[0]['text'], 'Hello\nworld')
        self.assertIn('00:00:01,000 --> 00:00:02,500', app.shift_srt(text, -.5))

    def test_sidecar_with_metacharacters(self):
        movie = app.MOVIE_DIR / 'Movie [2020].mkv'; movie.touch()
        movie.with_suffix('.eng.srt').touch()
        self.assertTrue(app.has_subtitle_sidecar(movie))

    def test_scan_detects_and_migrates(self):
        movie = (app.MOVIE_DIR / 'Movie.mkv').resolve(); movie.touch()
        hidden = app.MOVIE_DIR / '._hidden.mkv'; hidden.touch()
        app.save_state({'movies': {str(movie): {'has_subtitles': True, 'embedded_checked': True}}, 'api_lookups': 0})
        with patch.object(app, 'embedded_subtitles', return_value=False):
            app.discover_library()
        state = app.load_state()['movies']
        self.assertEqual(list(state), [str(movie)])
        self.assertFalse(state[str(movie)]['has_subtitles'])
        self.assertTrue(state[str(movie)]['embedded_checked'])

    def test_incomplete_scan_resumes_once_after_startup(self):
        app.save_state({'movies': {'movie': {'embedded_checked': False, 'has_sidecar': False}}})
        app.startup_checked = False
        with patch.object(app, 'start_scan') as start:
            app.movies()
            app.movies()
            start.assert_called_once()

    def test_old_negative_result_does_not_need_reprobe(self):
        movie = (app.MOVIE_DIR / 'Movie.mkv').resolve(); movie.touch()
        app.save_state({'movies': {str(movie): {'has_subtitles': False, 'embedded_checked': True}}})
        with patch.object(app, 'embedded_subtitles') as probe:
            app.discover_library()
            probe.assert_not_called()
        self.assertFalse(app.load_state()['movies'][str(movie)]['has_embedded'])

    def test_probe_starts_before_discovery_finishes(self):
        first = app.MOVIE_DIR / 'First.mkv'; first.touch()
        second = app.MOVIE_DIR / 'Second.mkv'; second.touch()
        started = threading.Event()
        def files(*args):
            yield first
            self.assertTrue(started.wait(2), 'probe did not start during discovery')
            yield second
        def probe(path):
            started.set()
            return False
        with patch.object(Path, 'rglob', files), patch.object(app, 'embedded_subtitles', probe):
            app.discover_library()
        self.assertEqual(len(app.load_state()['movies']), 2)
        self.assertTrue(all(m['embedded_checked'] for m in app.load_state()['movies'].values()))

    def test_search_rejects_unrelated_movies_and_episodes(self):
        title = 'How to Lose a Guy in 10 Days'
        def item(name, kind='Movie', year=2003):
            return {'attributes': {'feature_details': {'title': name, 'feature_type': kind, 'year': year}, 'release': name, 'files': [{'file_name': name}]}}
        self.assertIsNotNone(app.candidate_rank(item(title), title, '2003'))
        self.assertIsNone(app.candidate_rank(item('Two and a Half Men', 'Episode'), title, '2003'))
        self.assertIsNone(app.candidate_rank(item('Ghost in the Shell'), title, '2003'))
        self.assertIsNone(app.candidate_rank(item(title, year=2010), title, '2003'))
        self.assertIsNotNone(app.candidate_rank({'attributes': {'release': 'How.to.Lose.a.Guy.in.10.Days.2003.1080p'}}, title, '2003'))
        self.assertIsNone(app.candidate_rank({'attributes': {'release': 'Ghost.in.the.Shell.10'}}, title, '2003'))

    def test_api_quota_recorded_from_download_response(self):
        response = types.SimpleNamespace(headers={'RateLimit-Remaining': '3', 'RateLimit-Limit': '5'}, json=lambda: {'remaining': 82})
        app.record_api_response(response, download=True)
        state = app.load_state()
        self.assertEqual(state['downloads_remaining'], 82)
        self.assertEqual(state['api_rate_remaining'], '3')
        self.assertTrue(state['downloads_updated_at'])
        self.assertTrue(state['api_rate_updated_at'])
        app.record_api_response(types.SimpleNamespace(headers={}, json=lambda: {}), download=True)
        self.assertEqual(app.load_state()['downloads_remaining'], 82)

    def test_downloaded_tracks_are_preserved_and_selectable(self):
        movie = (app.MOVIE_DIR / 'Movie.mkv').resolve(); movie.touch()
        legacy = app.DATA_DIR / 'old.srt'; legacy.write_text('old subtitle')
        state = {'working_srt': str(legacy), 'current_candidate_id': 1, 'current_candidate_name': 'First'}
        app.preserve_legacy_track(movie, state)
        legacy.write_text('overwritten')
        second = app.DATA_DIR / 'second.srt'; second.write_text('new subtitle')
        state['track_history'].append({'id': 2, 'name': 'Second', 'file': str(second)})
        self.assertTrue(app.select_track(state, 2))
        self.assertTrue(app.select_track(state, '1'))
        self.assertEqual(Path(state['working_srt']).read_text(), 'old subtitle')
        self.assertEqual(state['current_candidate_name'], 'First')
        self.assertFalse(app.select_track(state, 3))
        self.assertEqual(state['current_candidate_id'], 1)

    def test_track_scores_require_matching_file_hash(self):
        import hashlib
        track=app.DATA_DIR/'track.srt';track.write_text('subtitle')
        movie={'track_history':[{'id':42,'name':'Track','file':str(track)}]}
        analysis=[{'track_id':42,'status':'analyzed','subtitle_sha256':hashlib.sha256(b'subtitle').hexdigest(),'fit_score':91,'recommended_offset_seconds':-2.5}]
        result=app.available_tracks(movie,analysis)
        self.assertEqual((result[0]['fit_score'],result[0]['recommended_offset_seconds']),(91,-2.5))
        track.write_text('changed subtitle')
        self.assertIsNone(app.available_tracks(movie,analysis)[0]['fit_score'])
        self.assertIsNone(app.available_tracks(movie,analysis)[0]['recommended_offset_seconds'])

    def test_legacy_track_counts_and_unscored_tracks(self):
        track=app.DATA_DIR/'old.srt';track.write_text('subtitle')
        movie={'working_srt':str(track),'current_candidate_id':2,'current_candidate_name':'Old'}
        result=app.available_tracks(movie,[])
        self.assertEqual(len(result),1)
        self.assertIsNone(result[0]['fit_score'])

    def test_scan_failure_keeps_cached_library(self):
        app.save_state({'movies': {'cached': {'title': 'Cached'}}})
        app.MOVIE_DIR = self.root / 'missing'
        app.discover_library()
        self.assertIn('cached', app.load_state()['movies'])
        self.assertIn('unavailable', app.load_state()['scan_error'])

if __name__ == '__main__': unittest.main()
