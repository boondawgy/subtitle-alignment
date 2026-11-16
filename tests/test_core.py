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

    def test_old_negative_result_is_reprobed_without_file_fingerprint(self):
        movie = (app.MOVIE_DIR / 'Movie.mkv').resolve(); movie.touch()
        app.save_state({'movies': {str(movie): {'has_subtitles': False, 'embedded_checked': True}}})
        with patch.object(app, 'embedded_subtitles', return_value=False) as probe:
            app.discover_library()
            probe.assert_called_once_with(movie)
        self.assertFalse(app.load_state()['movies'][str(movie)]['has_embedded'])

    def test_unchanged_file_reuses_embedded_subtitle_probe(self):
        movie = (app.MOVIE_DIR / 'Movie.mkv').resolve(); movie.touch()
        stat = movie.stat()
        app.save_state({'movies': {str(movie): {'has_embedded': False, 'embedded_checked': True,
                       'movie_size': stat.st_size, 'movie_mtime_ns': stat.st_mtime_ns}}})
        with patch.object(app, 'embedded_subtitles') as probe:
            app.discover_library()
            probe.assert_not_called()

    def test_preferred_track_offset_can_override_sampled_analysis(self):
        track = app.DATA_DIR / 'track.srt'; track.write_text('subtitle')
        movie = {'track_history': [{'id': 'proposal', 'file': str(track), 'recommended_offset_seconds': 0}]}
        digest = app.track_digest(track)
        analysis = [{'track_id': 'proposal', 'subtitle_sha256': digest, 'status': 'analyzed',
                     'fit_score': 90, 'recommended_offset_seconds': 4.5}]
        self.assertEqual(app.available_tracks(movie, analysis)[0]['recommended_offset_seconds'], 0)

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

    def test_rejected_tracks_are_identified(self):
        track=app.DATA_DIR/'track.srt';track.write_text('subtitle')
        movie={'track_history':[{'id':42,'name':'Track','file':str(track)}],'rejected_ids':['42']}
        self.assertTrue(app.available_tracks(movie,[])[0]['rejected'])

    def test_legacy_track_counts_and_unscored_tracks(self):
        track=app.DATA_DIR/'old.srt';track.write_text('subtitle')
        movie={'working_srt':str(track),'current_candidate_id':2,'current_candidate_name':'Old'}
        result=app.available_tracks(movie,[])
        self.assertEqual(len(result),1)
        self.assertIsNone(result[0]['fit_score'])

    def test_imdb_search_excludes_results_for_other_movies(self):
        movie=(app.MOVIE_DIR/'Up.mp4').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'title':'Up','path':str(movie)}}})
        def item(imdb_id, file_id):
            return {'id':str(file_id),'attributes':{'feature_details':{'imdb_id':imdb_id,'title':'Up','feature_type':'Movie'},'files':[{'file_id':file_id,'file_name':'Up.srt'}],'download_count':10}}
        response=types.SimpleNamespace(ok=True,headers={},json=lambda:{'data':[item(1049413,1),item(9999999,2)]})
        with patch.object(app,'request',types.SimpleNamespace(json={'path':str(movie),'imdb_id':'tt1049413'})), patch.object(app.requests,'get',return_value=response,create=True), patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result=app.api_search()
        self.assertEqual([c['download_id'] for c in result['candidates']],[1])

    def test_account_login_and_token_refresh(self):
        app._auth_token=None;app._auth_base='https://api.opensubtitles.com/api/v1';app._auth_blocked=False
        login1=types.SimpleNamespace(status_code=200,ok=True,json=lambda:{'token':'token-one','base_url':'vip-api.opensubtitles.com'})
        login2=types.SimpleNamespace(status_code=200,ok=True,json=lambda:{'token':'token-two','base_url':'vip-api.opensubtitles.com'})
        expired=types.SimpleNamespace(status_code=401,ok=False)
        success=types.SimpleNamespace(status_code=200,ok=True)
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':'test-password'},clear=False), patch.object(app.requests,'post',side_effect=[login1,login2],create=True) as login, patch.object(app.requests,'get',side_effect=[expired,success],create=True) as api:
            self.assertIs(app.opensubtitles_request('get','/subtitles',params={}),success)
            self.assertEqual(login.call_count,2)
            self.assertEqual(api.call_args_list[0].kwargs['headers']['Authorization'],'Bearer token-one')
            self.assertEqual(api.call_args_list[1].kwargs['headers']['Authorization'],'Bearer token-two')
            self.assertTrue(api.call_args_list[1].args[0].startswith('https://vip-api.opensubtitles.com/api/v1/'))
        app._auth_token=None;app._auth_blocked=False

    def test_rejected_login_is_not_retried_and_never_falls_back(self):
        app._auth_token=None;app._auth_blocked=False
        denied=types.SimpleNamespace(status_code=401,ok=False)
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':'wrong'},clear=False), patch.object(app.requests,'post',return_value=denied,create=True) as login:
            for _ in range(2):
                with self.assertRaises(app.OpenSubtitlesError):app.opensubtitles_token()
            self.assertEqual(login.call_count,1)
        app._auth_token=None;app._auth_blocked=False

    def test_incomplete_credentials_and_untrusted_api_host(self):
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':''},clear=False):
            with self.assertRaises(app.OpenSubtitlesError):app.opensubtitles_credentials()
        with self.assertRaises(app.OpenSubtitlesError):app.opensubtitles_base_url('https://evil.example/api/v1')

    def test_anonymous_download_quota_is_not_shown_for_an_account(self):
        app.save_state({'movies':{},'downloads_remaining':17})
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':'test-password'},clear=False), patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            self.assertIsNone(app.movies()['downloads_remaining'])

    def test_search_follows_only_same_host_redirects(self):
        app._auth_token='test-token';app._auth_blocked=False;app._auth_base='https://api.opensubtitles.com/api/v1'
        redirect=types.SimpleNamespace(status_code=301,headers={'Location':'/api/v1/subtitles?query=test'})
        success=types.SimpleNamespace(status_code=200,headers={})
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':'test-password'},clear=False), patch.object(app.requests,'get',side_effect=[redirect,success],create=True) as get:
            self.assertIs(app.opensubtitles_request('get','/subtitles',params={'query':'TEST'}),success)
            self.assertEqual(get.call_args_list[1].args[0],'https://api.opensubtitles.com/api/v1/subtitles?query=test')
            self.assertNotIn('params',get.call_args_list[1].kwargs)
        redirect.headers['Location']='https://evil.example/api/v1/subtitles'
        with patch.dict('os.environ',{'OPENSUBTITLES_USERNAME':'test-user','OPENSUBTITLES_PASSWORD':'test-password'},clear=False), patch.object(app.requests,'get',return_value=redirect,create=True) as get:
            with self.assertRaises(app.OpenSubtitlesError):app.opensubtitles_request('get','/subtitles')
            self.assertEqual(get.call_count,1)
        app._auth_token=None;app._auth_blocked=False

    def test_search_requests_page_and_reports_total_pages(self):
        movie=(app.MOVIE_DIR/'Movie.mkv').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'title':'Movie','path':str(movie)}}})
        response=types.SimpleNamespace(ok=True,headers={},json=lambda:{'data':[],'total_pages':3,'total_count':123})
        with patch.object(app,'request',types.SimpleNamespace(json={'path':str(movie),'page':2})), patch.object(app,'opensubtitles_request',return_value=response) as search, patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result=app.api_search()
        self.assertEqual(search.call_args.kwargs['params']['page'],2)
        self.assertEqual((result['page'],result['total_pages']),(2,3))

    def test_unknown_candidate_is_not_downloaded(self):
        movie=(app.MOVIE_DIR/'Movie.mkv').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'title':'Movie','path':str(movie),'candidates':[{'download_id':1}]}}})
        with patch.object(app,'request',types.SimpleNamespace(json={'path':str(movie),'file_id':2})), patch.object(app,'download_candidate') as download, patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result,status=app.api_download()
        self.assertEqual(status,409);download.assert_not_called()

    def test_pass_movie_persists_and_can_be_reversed(self):
        movie=(app.MOVIE_DIR/'Movie.mkv').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'path':str(movie),'title':'Movie','embedded_checked':True,'has_embedded':False}}})
        for value in (True, False):
            fake=types.SimpleNamespace(get_json=lambda silent=True:{'path':str(movie),'passed':value})
            with patch.object(app,'request',fake),patch.object(app,'jsonify',side_effect=lambda **kw:kw):
                self.assertTrue(app.pass_movie()['ok'])
            self.assertIs(app.load_state()['movies'][str(movie)]['passed'],value)
            if value:
                with patch.object(app,'embedded_subtitles',return_value=False): app.discover_library()
                self.assertTrue(app.load_state()['movies'][str(movie)]['passed'])

    def test_pass_movie_rejects_non_boolean_value(self):
        fake=types.SimpleNamespace(get_json=lambda silent=True:{'path':'ignored','passed':'false'})
        with patch.object(app,'request',fake),patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result,status=app.pass_movie()
        self.assertEqual(status,400)

    def test_saved_imdb_identifier_used_for_search(self):
        movie=(app.MOVIE_DIR/'Movie.mkv').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'title':'Movie','path':str(movie),'imdb_id':'tt1234567'}}})
        item={'id':'1','attributes':{'feature_details':{'imdb_id':1234567,'title':'Movie','feature_type':'Movie'},'files':[{'file_id':1,'file_name':'Movie.srt'}]}}
        response=types.SimpleNamespace(ok=True,headers={},json=lambda:{'data':[item]})
        with patch.object(app,'request',types.SimpleNamespace(json={'path':str(movie)})),patch.object(app,'opensubtitles_request',return_value=response) as search,patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result=app.api_search()
        self.assertEqual(search.call_args.kwargs['params']['imdb_id'],1234567)
        self.assertEqual([c['download_id'] for c in result['candidates']],[1])

    def test_imdb_match_allows_different_filename_release_year(self):
        movie=(app.MOVIE_DIR/'A Monster in Paris (2013).mp4').resolve();movie.touch()
        app.save_state({'movies':{str(movie):{'title':'A Monster in Paris (2013)','path':str(movie),'imdb_id':'tt0961097'}}})
        item={'id':'1','attributes':{'feature_details':{'imdb_id':961097,'title':'A Monster in Paris','year':2011,'feature_type':'Movie'},'files':[{'file_id':7,'file_name':'A Monster in Paris.srt'}]}}
        response=types.SimpleNamespace(ok=True,headers={},json=lambda:{'data':[item]})
        with patch.object(app,'request',types.SimpleNamespace(json={'path':str(movie)})),patch.object(app,'opensubtitles_request',return_value=response),patch.object(app,'jsonify',side_effect=lambda **kw:kw):
            result=app.api_search()
        self.assertEqual([c['download_id'] for c in result['candidates']],[7])

    def test_scan_failure_keeps_cached_library(self):
        app.save_state({'movies': {'cached': {'title': 'Cached'}}})
        app.MOVIE_DIR = self.root / 'missing'
        app.discover_library()
        self.assertIn('cached', app.load_state()['movies'])
        self.assertIn('unavailable', app.load_state()['scan_error'])

if __name__ == '__main__': unittest.main()
